"""Rules engine. Work is created by versioned business rules over deterministic metrics, never by
free-form AI reasoning. Rules over stale data do not fire."""
import logging
from collections import defaultdict, deque
import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import data, events, expr, work
from .models import Approval, WorkItem
from .metrics import MetricEngine, same_value
from .tenancy import RuleConfig, TenantConfig

log = logging.getLogger(__name__)
MAX_NEW_PER_RULE = 20
MAX_BATCH = 25  # groups in one batched investigation, so its context stays small


def rule_datasets(cfg: TenantConfig, rule: RuleConfig) -> set[str]:
    if rule.kind == "conflict":
        return {rule.conflict.dataset, rule.conflict.other}
    if rule.kind == "invoice_match":
        return {rule.invoice_match.invoices, rule.invoice_match.orders, rule.invoice_match.receipts}
    if rule.kind == "duplicate":
        return {rule.duplicate.dataset}
    seen, out, todo = set(), set(), list(expr.names(rule.when) | set(rule.metrics))
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        mc = cfg.metrics[m]
        if mc.dataset:
            out.add(mc.dataset)
        else:
            todo.extend(expr.names(mc.formula))
    return out


def evaluate(db: Session, cfg: TenantConfig) -> int:
    """Run every rule once. Returns the number of work items created."""
    engine = MetricEngine(db, cfg)
    created = 0
    for rule in cfg.rules:
        stale = [d for d in rule_datasets(cfg, rule) if data.freshness(db, cfg, d)[0] != "CURRENT"]
        if stale:
            log.info("tenant %s rule %s skipped: stale datasets %s", cfg.id, rule.id, stale)
            continue
        fire = {"threshold": _threshold, "conflict": _conflict, "invoice_match": _invoice_match, "duplicate": _duplicates}[rule.kind]
        created += fire(db, cfg, rule, engine)
    return created


def _fmt(cfg: TenantConfig, text: str, values: dict) -> str:
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError):
        return text


def _threshold(db: Session, cfg: TenantConfig, rule: RuleConfig, engine: MetricEngine) -> int:
    dim = rule.for_each
    groups: set = set()
    for m in expr.names(rule.when):
        if dim and cfg.metrics[m].by == [dim]:
            groups |= {g[0] for g in engine.values(m)}
    if not dim:
        groups = {None}
    covered = work.groups_covered(db, cfg.id, rule.id, rule.cooldown_hours)
    hits, unreported, cleared = [], defaultdict(list), set()
    for group in sorted(groups, key=str):
        env = {m: engine.for_group(m, dim, group, exact=True) for m in expr.names(rule.when)}
        result = expr.evaluate(rule.when, env)
        if result is False:
            cleared.add(str(group))
        if result is True and str(group) not in covered:
            hits.append(group)
        elif result is None and dim:
            for d in set().union(*(_unreported(cfg, engine, m, dim, group) for m in env)):
                unreported[d].append(group)
    _report_gaps(db, cfg, rule, dim, unreported)
    snapshot = any(cfg.datasets[d].mode == "snapshot" for d in rule_datasets(cfg, rule))
    _close_cleared(db, cfg, rule, cleared, "the figures no longer meet the rule",
                   evaluated=None if snapshot or not dim else {str(g) for g in groups})
    limit = MAX_BATCH if rule.batch else MAX_NEW_PER_RULE
    if len(hits) > limit:
        events.emit(db, cfg.id, "RULE_TRIGGERED", f"Rule {rule.id} matched more groups than the per-pass limit ({limit}); the rest wait for the next pass",
                    rule=rule.id, limited=True)
        hits = hits[:limit]
    if not hits:
        return 0
    packs = {g: _pack(cfg, rule, engine, dim, g) for g in hits}
    area = cfg.employees[rule.employee].area
    if rule.batch and len(hits) > 1:
        first = _values(dim, hits[0], packs[hits[0]])
        title = f"{_fmt(cfg, rule.title, first)} and {len(hits) - 1} more"
        events.emit(db, cfg.id, "RULE_TRIGGERED", f"Rule {rule.id} v{rule.version} fired for {len(hits)} {dim} values: "
                    f"{', '.join(map(str, hits))} → {rule.work_type}", rule=rule.id, area=area, groups=[str(g) for g in hits])
        work.create(db, cfg, employee=rule.employee, type_=rule.work_type, priority=rule.priority, title=title,
                    subject=title, origin="rule", rule_id=rule.id, dedupe_key=f"{rule.id}:batch",
                    context={"rule": {"id": rule.id, "version": rule.version, "condition": rule.when}, "dimension": dim,
                             "groups": hits, "evidence_packs": {str(g): packs[g] for g in hits}, "sentiment": rule.sentiment},
                    next_step="Start investigation")
        return 1
    for group in hits:
        values = _values(dim, group, packs[group])
        title = _fmt(cfg, rule.title, values)
        events.emit(db, cfg.id, "RULE_TRIGGERED", f"Rule {rule.id} v{rule.version} fired: {title} → {rule.work_type}",
                    rule=rule.id, area=area, group=str(group) if group is not None else None)
        work.create(db, cfg, employee=rule.employee, type_=rule.work_type, priority=rule.priority, title=title,
                    subject=_fmt(cfg, rule.subject, values) or title, origin="rule", rule_id=rule.id, dedupe_key=f"{rule.id}:{group}",
                    context={"rule": {"id": rule.id, "version": rule.version, "condition": rule.when},
                             "dimension": dim, "group": group, "evidence_pack": packs[group], "sentiment": rule.sentiment},
                    next_step="Start investigation")
    return len(hits)


def _unreported(cfg: TenantConfig, engine: MetricEngine, metric: str, dim, group) -> set[str]:
    """Snapshot datasets (stock counts, scorecards) that have no row for this group, so the rule cannot judge it."""
    m = cfg.metrics[metric]
    if m.dataset:
        unknown = cfg.datasets[m.dataset].mode == "snapshot" and m.by == [dim] and engine.for_group(metric, dim, group) is None
        return {m.dataset} if unknown else set()
    return set().union(*(_unreported(cfg, engine, p, dim, group) for p in expr.names(m.formula)))


def _report_gaps(db: Session, cfg: TenantConfig, rule: RuleConfig, dim, unreported: dict[str, list]) -> None:
    """Groups a rule could not check because a snapshot has no row for them become one owned data task per dataset,
    instead of silently passing (or, worse, counting as zero)."""
    for name in {rule_ds for rule_ds in rule_datasets(cfg, rule) if cfg.datasets[rule_ds].mode == "snapshot"}:
        ds, topic = cfg.datasets[name], f"gaps:{rule.id}"
        missing = unreported.get(name)
        if not missing:
            data.clear_issues(db, cfg, ds.source, name, topic)
            continue
        shown = ", ".join(map(str, missing[:10])) + (f" and {len(missing) - 10} more" if len(missing) > 10 else "")
        data.raise_issue(db, cfg, ds.source, name, [
            f"{len(missing)} {dim} values have recent activity but no row in the latest {ds.label}, so the rule "
            f"\"{rule.description or rule.id}\" cannot check them.",
            f"Missing: {shown}.",
            "They are treated as unknown, never as zero. Add them to the export (with 0 where there truly is none)."],
            topic=topic, title="Records missing")


def _pack(cfg: TenantConfig, rule: RuleConfig, engine: MetricEngine, dim, group) -> dict:
    """The evidence pack: the rule's metrics for one group, computed before any AI runs."""
    pack = {}
    for m in dict.fromkeys([*expr.names(rule.when), *rule.metrics]):
        mc = cfg.metrics[m]
        pack[m] = {"label": mc.label, "value": engine.for_group(m, dim, group), "unit": mc.unit, "version": mc.version}
    return pack


def _values(dim, group, pack: dict) -> dict:
    values = {dim: group} if dim else {}
    values.update({m: v["value"] for m, v in pack.items()})
    return values


def _conflict(db: Session, cfg: TenantConfig, rule: RuleConfig, engine: MetricEngine) -> int:
    c = rule.conflict
    a = {r.get(c.match_on): r.get(c.field) for r in data.rows(db, cfg, c.dataset)}
    b = {r.get(c.match_on): r.get(c.other_field or c.field) for r in data.rows(db, cfg, c.other)}
    made = 0
    for key in sorted(set(a) & set(b), key=str):
        va, vb = a[key], b[key]
        if va is None or vb is None or abs(float(va) - float(vb)) <= c.tolerance:
            continue
        dedupe = f"{rule.id}:{key}"
        if work.open_or_recent(db, cfg.id, dedupe, rule.cooldown_hours) or made >= MAX_NEW_PER_RULE:
            continue
        la, lb = cfg.datasets[c.dataset].label, cfg.datasets[c.other].label
        title = _fmt(cfg, rule.title, {c.match_on: key})
        events.emit(db, cfg.id, "RULE_TRIGGERED", f"Rule {rule.id}: DATA CONFLICT for {key}: {la} = {va}, {lb} = {vb}",
                    rule=rule.id, area=cfg.employees[rule.employee].area, group=str(key))
        work.create(db, cfg, employee=rule.employee, type_=rule.work_type, priority=rule.priority, title=title,
                    subject=_fmt(cfg, rule.subject, {c.match_on: key}) or title, origin="check", rule_id=rule.id,
                    dedupe_key=dedupe, next_step="Start investigation",
                    context={"rule": {"id": rule.id, "version": rule.version}, "dimension": c.match_on, "group": key,
                             "conflict": {c.dataset: va, c.other: vb, "field": c.field, "tolerance": c.tolerance}})
        made += 1
    return made


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _check_finding(db: Session, cfg: TenantConfig, rule: RuleConfig, group: str, check: dict, values: dict, msg: str) -> None:
    """Create one task for a deterministic check (three-way match, duplicate invoice). The check's figures and the
    source records behind them become the task's evidence before any AI runs."""
    title = _fmt(cfg, rule.title, values)
    events.emit(db, cfg.id, "RULE_TRIGGERED", f"Rule {rule.id}: {msg}", rule=rule.id, area=cfg.employees[rule.employee].area, group=group)
    work.create(db, cfg, employee=rule.employee, type_=rule.work_type, priority=rule.priority, title=title,
                subject=_fmt(cfg, rule.subject, values) or title, origin="check", rule_id=rule.id, dedupe_key=f"{rule.id}:{group}",
                next_step="Start investigation", context={"rule": {"id": rule.id, "version": rule.version}, "group": group,
                                                          "dimension": check["dimension"], "check": check})


def _invoice_match(db: Session, cfg: TenantConfig, rule: RuleConfig, engine: MetricEngine) -> int:
    """Three-way match of each open invoice: billed quantity against goods received and ordered (this invoice alone and
    all of the order's invoices together), and billed price against the order price. What cannot be checked (no price,
    no quantity, different units) is raised as such rather than passed. Every difference is computed here, never by the model."""
    m = rule.invoice_match
    orders = {str(r.get(m.po_field)): (rid, r) for rid, r in data.rows(db, cfg, m.orders, with_ids=True)}
    received: dict[str, list] = {}
    for rid, r in data.rows(db, cfg, m.receipts, with_ids=True):
        received.setdefault(str(r.get(m.po_field)), []).append((rid, r))
    invoices = data.rows(db, cfg, m.invoices, with_ids=True)
    billed_total: dict[str, float] = defaultdict(float)  # every invoice on the order that is not cancelled, paid ones included
    counted = set()  # the same supplier invoice entered twice counts once here; the duplicate check raises the copy
    for _, r in invoices:
        once = (str(r.get(m.po_field)), r.get(m.supplier_field), _norm_number(r.get(m.invoice_field)))
        if not _is_open(r.get(m.status_field), m.cancelled_statuses) and once not in counted:
            counted.add(once)
            billed_total[once[0]] += _num(r.get(m.quantity_field)) or 0
    covered = work.groups_covered(db, cfg.id, rule.id, rule.cooldown_hours)
    made, firing, present = 0, set(), set()
    for rid, inv in sorted(invoices, key=lambda p: str(p[1].get(m.invoice_field))):
        number, po = str(inv.get(m.invoice_field)), str(inv.get(m.po_field))
        present.add(number)
        if not _is_open(inv.get(m.status_field), m.open_statuses):
            continue
        billed_qty, billed_price = _num(inv.get(m.quantity_field)), _num(inv.get(m.price_field))
        order = orders.get(po)
        got = received.get(po, [])
        recv_qty = sum(_num(r.get(m.quantity_field)) or 0 for _, r in got)
        ord_qty, ord_price = (_num(order[1].get(m.quantity_field)), _num(order[1].get(m.price_field))) if order else (None, None)
        units = _unit_problem(m, order[1] if order else None, [r for _, r in got], inv)
        tol = 1 + m.quantity_tolerance
        issues, over_qty = [], 0.0
        if order is None:
            issues.append("no purchase order")
        elif _is_open(order[1].get(m.order_status_field), m.cancelled_statuses):
            issues.append("the order was cancelled")
        if not got:
            issues.append("no goods received")
            over_qty = billed_qty or 0
        if billed_qty is None:
            issues.append("invoice has no quantity, so it cannot be checked")
        if billed_price is None:
            issues.append("invoice has no unit price, so it cannot be checked")
        if units:
            issues.append(units)
            over_qty = 0.0  # quantities in different units say nothing about how much is overbilled
        elif billed_qty is not None:
            together = billed_total[po]
            if got and billed_qty > recv_qty * tol:
                issues.append("billed more than received")
                over_qty = billed_qty - recv_qty
            elif got and together > recv_qty * tol:
                issues.append("billed more than received in total (with this order's other invoices)")
                over_qty = min(billed_qty, together - recv_qty)
            if ord_qty is not None and billed_qty > ord_qty * tol:
                issues.append("billed more than ordered")
            elif ord_qty is not None and together > ord_qty * tol and not any(i.startswith("billed more than received") for i in issues):
                issues.append("billed more than ordered in total (with this order's other invoices)")
        if order is not None and ord_price is None and billed_price is not None:
            issues.append("order has no price, so the price cannot be checked")
        elif billed_price is not None and ord_price is not None and abs(billed_price - ord_price) > ord_price * m.price_tolerance:
            issues.append("price differs from the order")
        if not issues:
            continue
        firing.add(number)
        if number in covered or made >= MAX_NEW_PER_RULE:
            continue
        over_price = (billed_price - ord_price) * (billed_qty or 0) if billed_price is not None and ord_price is not None else 0.0
        check = {"kind": "three_way_match", "dimension": m.invoice_field, "invoice": number, "po_number": po,
                 "supplier": inv.get(m.supplier_field), "issues": issues, "billed_quantity": billed_qty, "billed_unit_price": billed_price,
                 "ordered_quantity": ord_qty, "order_unit_price": ord_price, "received_quantity": recv_qty, "receipts": len(got),
                 "billed_on_order_total": round(billed_total[po], 4),
                 "excess_quantity": round(over_qty, 4), "amount_at_risk": round(over_qty * (billed_price or 0) + max(0.0, over_price), 2),
                 "record_ids": [rid] + ([order[0]] if order else []) + [g for g, _ in got]}
        _check_finding(db, cfg, rule, number, check, {m.invoice_field: number, "supplier": inv.get(m.supplier_field), "po_number": po},
                       f"invoice {number} on {po}: {', '.join(issues)}")
        made += 1
    _close_cleared(db, cfg, rule, present - firing, "the invoice now matches its order and receipts, or is no longer open")
    return made


def _unit_problem(m, order: dict | None, receipts: list[dict], invoice: dict) -> str | None:
    """Quantities compare only when they are in the order's unit. A unit that is not given is taken to be the order's."""
    def unit(r):
        v = (r or {}).get(m.unit_field)
        return str(v).strip() if v not in (None, "") else None
    base = unit(order)
    if base is None:
        return None
    off = []
    got = sorted({u for u in map(unit, receipts) if u and u.casefold() != base.casefold()})
    if got:
        off.append(f"receipts: {', '.join(got)}")
    if unit(invoice) and unit(invoice).casefold() != base.casefold():
        off.append(f"invoice: {unit(invoice)}")
    return f"units differ from the order (order: {base}; {'; '.join(off)}), so quantities cannot be compared" if off else None


def _norm_number(v) -> str:
    """INV-00457, inv 457 and INV/457 are the same supplier invoice number: case, separators and zero padding
    inside the digits are ignored."""
    return re.sub(r"(?<!\d)0+(?=\d)", "", re.sub(r"[^A-Z0-9]", "", str(v or "").upper())) or "0"


def _duplicates(db: Session, cfg: TenantConfig, rule: RuleConfig, engine: MetricEngine) -> int:
    """Duplicate invoices: the same supplier invoice number entered more than once, or the same supplier billing
    the same amount again within the window under another number. The later entry is the suspect."""
    d = rule.duplicate
    rows = sorted(data.rows(db, cfg, d.dataset, with_ids=True), key=lambda p: (str(p[1].get(d.date_field)), p[0]))
    covered = work.groups_covered(db, cfg.id, rule.id, rule.cooldown_hours)
    key = cfg.datasets[d.dataset].key
    made, firing = 0, set()
    # Earlier entries are looked up, not scanned: by supplier and normalised number (any date), and the same supplier's
    # entries inside the date window (rows are in date order, so older ones drop off the front).
    by_number: dict[tuple, list] = defaultdict(list)
    recent: dict = defaultdict(deque)
    for idx, (rid, r) in enumerate(rows):
        number, supplier, amount = str(r.get(d.number_field)), r.get(d.supplier_field), _num(r.get(d.amount_field))
        day = r.get(d.date_field)
        nn = _norm_number(number)
        found = {i: (prid, p, "same invoice number") for i, prid, p in by_number[(supplier, nn)]}
        on = date.fromisoformat(day) if day else None
        if on is not None:
            window = recent[supplier]
            while window and (on - window[0][1]).days > d.window_days:
                window.popleft()
            if amount is not None:
                for i, _, prid, p in window:
                    pa = _num(p.get(d.amount_field))
                    if i not in found and pa is not None and abs(amount - pa) <= d.amount_tolerance:
                        found[i] = (prid, p, "same amount within the window")
            window.append((idx, on, rid, r))
        by_number[(supplier, nn)].append((idx, rid, r))
        matches = [found[i] for i in sorted(found)]
        if not matches:
            continue
        group = "|".join(str(r.get(k)) for k in key)  # the suspect entry, by its accounting-system key
        if not _is_open(r.get(d.status_field), d.open_statuses) and not any(_is_open(p.get(d.status_field), d.open_statuses) for _, p, _ in matches):
            continue  # both already paid: a recovery question for accounts, not a payment to stop
        firing.add(group)
        if group in covered or made >= MAX_NEW_PER_RULE:
            continue
        check = {"kind": "duplicate_invoice", "dimension": "+".join(key), "invoice": number, "supplier": supplier, "amount": amount,
                 "date": day, "status": r.get(d.status_field), "reason": matches[0][2],
                 "earlier": [{"invoice": p.get(d.number_field), "amount": _num(p.get(d.amount_field)),
                              "date": p.get(d.date_field), "status": p.get(d.status_field)} for _, p, _ in matches],
                 "record_ids": [rid] + [prid for prid, _, _ in matches]}
        _check_finding(db, cfg, rule, group, check, {d.number_field: number, "supplier": supplier},
                       f"invoice {number} from {supplier} looks like a duplicate ({matches[0][2]})")
        covered.add(group)
        made += 1
    present = {"|".join(str(r.get(k)) for k in key) for _, r in rows}
    _close_cleared(db, cfg, rule, present - firing, "the entries no longer look like a duplicate, or are no longer open")
    return made


def _is_open(status, open_statuses: list[str]) -> bool:
    return any(same_value(status, s) for s in open_statuses)


def _close_cleared(db: Session, cfg: TenantConfig, rule: RuleConfig, cleared: set[str], why: str,
                   evaluated: set[str] | None = None) -> int:
    """An alert closes by itself once every group it was raised for is checked again and no longer meets the rule (the
    stock arrived, the order was received). A batched alert records which of its groups have cleared until all have.
    `evaluated` (event data only): a group with no qualifying rows at all, e.g. a purchase order that is now received
    and so no longer open, has cleared too. In a snapshot a missing group is unknown, and unknown never clears.
    Work that is running or waiting for a person's decision is left for the person."""
    items = db.execute(select(WorkItem).where(WorkItem.tenant_id == cfg.id, WorkItem.rule_id == rule.id, WorkItem.parent_id.is_(None),
                                              WorkItem.status.notin_(list(work.TERMINAL | work.ACTIVE)))).scalars().all()
    closed = 0
    for item in items:
        groups = [str(g) for g in work.item_groups(item.context or {})]
        if not groups:
            continue
        done = [g for g in groups if g in cleared or (evaluated is not None and g not in evaluated)]
        if len(done) < len(groups):
            if len(groups) > 1 and (item.context or {}).get("cleared", []) != done:
                item.context = {**item.context, "cleared": done}
            continue
        if db.execute(select(Approval.id).where(Approval.tenant_id == cfg.id, Approval.work_id == item.id, Approval.status == "PENDING")).first():
            continue
        events.emit(db, cfg.id, "RULE_CLEARED", f"Rule {rule.id} no longer applies to {', '.join(groups[:10])}"
                    + (f" and {len(groups) - 10} more" if len(groups) > 10 else "") + f": {why}. {item.id} closed.",
                    rule=rule.id, work_id=item.id, groups=groups)
        item.context = {**item.context, "cleared": groups}
        work.transition(db, cfg, item, "COMPLETED", f"Closed by itself · {why}", outcome="CONDITION_CLEARED")
        closed += 1
    return closed


WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def schedules_due(db: Session, cfg: TenantConfig, now_local=None) -> int:
    """Create the regular reviews that are due now (company time). Each runs once per day, week or month."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = now_local or datetime.now(ZoneInfo(cfg.timezone))
    made = 0
    for sc in cfg.schedules:
        if now.hour < sc.hour:
            continue
        if sc.every == "weekly" and WEEKDAYS[now.weekday()] != sc.weekday:
            continue
        if sc.every == "monthly" and now.day != sc.day:
            continue
        period = {"daily": now.strftime("%Y-%m-%d"), "weekly": now.strftime("%G-W%V"), "monthly": now.strftime("%Y-%m")}[sc.every]
        dedupe = f"schedule:{sc.id}:{period}"
        if db.execute(select(WorkItem.id).where(WorkItem.tenant_id == cfg.id, WorkItem.dedupe_key == dedupe)).first():
            continue
        work.create(db, cfg, employee=sc.employee, type_="SCHEDULED_REVIEW", priority=sc.priority, title=f"{sc.title} · {period}",
                    subject=sc.brief, origin="schedule", dedupe_key=dedupe, next_step="Start the review",
                    context={"question": sc.brief, "schedule": sc.id, "period": period},
                    msg=f"Scheduled {sc.every} review {sc.id} for {period} → {cfg.employees[sc.employee].name}")
        made += 1
    return made
