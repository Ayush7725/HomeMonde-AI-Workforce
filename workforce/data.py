"""Data foundation: source connectors, validation/normalisation, ingestion and freshness.

Connectors only read. Rows are validated against the dataset schema before anything is written;
a batch with too many invalid rows is rejected whole rather than half-loaded."""
import csv
import itertools
import json
import math
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from . import events
from .db import is_postgres, utcnow
from .models import Record, SourceState
from .tenancy import DatasetConfig, FieldSpec, SourceConfig, TenantConfig, resolve_env

BATCH = 500


class SourceError(Exception):
    pass


class RowError(ValueError):
    """Why one row could not be read. `cause` is the same for every row with the same problem, whatever its value."""

    def __init__(self, field: str, problem: str, value=None):
        self.cause = f"{field}: {problem}"
        super().__init__(self.cause if value is None else f"{self.cause} '{value}'")


def today(cfg: TenantConfig) -> date:
    return datetime.now(ZoneInfo(cfg.timezone)).date()


# ---------------------------------------------------------------- connectors

def _csv_rows(src: SourceConfig, ds: DatasetConfig):
    if not re.fullmatch(r"[\w.\- ]+\.csv", ds.extract):
        raise SourceError("csv extract must be a plain .csv file name")
    path = Path(resolve_env(src.location)) / ds.extract
    if not path.is_file():
        raise SourceError(f"file not found: {ds.extract}")
    with path.open(newline="", encoding="utf-8-sig") as f:
        yield from csv.DictReader(f, delimiter=src.delimiter)


def _file_changed_at(src: SourceConfig, ds: DatasetConfig) -> datetime | None:
    """When a CSV export was last written (UTC), or None when the source is not a file."""
    if src.type != "csv":
        return None
    path = Path(resolve_env(src.location)) / ds.extract
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(tzinfo=None) if path.is_file() else None


@lru_cache(maxsize=32)
def _sql_engine(url: str):
    return create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=0)


def _sql_rows(src: SourceConfig, ds: DatasetConfig):
    q = ds.extract.strip().rstrip(";")
    if not re.match(r"(?is)^\s*(select|with)\b", q) or ";" in q:
        raise SourceError("sql extract must be a single SELECT statement")
    with _sql_engine(resolve_env(src.location)).connect() as conn:
        for row in conn.execution_options(stream_results=True).execute(text(q)).mappings():
            yield dict(row)


CONNECTORS = {"csv": _csv_rows, "sql": _sql_rows}


# ---------------------------------------------------------------- normalisation

_NUM_NOISE = re.compile(r"[\s,₹$€£%]")
_PLAIN_NUMBER = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)")
CURRENCY_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}


class OtherCurrency(ValueError):
    """An amount written in a currency that is not the company's own: it is refused, never counted as if it were."""


def _number(raw, currency: str | None = None) -> float:
    """Accept '1,200', '₹ 4,500', '88%' and negatives in parentheses, as exports often write them. A currency symbol is
    accepted only when it is the company's own currency: '$50' in a rupee company is refused, not read as ₹50.
    Anything else is refused rather than guessed: '1e3' (a spreadsheet mangling a cell) would otherwise read as 1000,
    and 'nan' or 'inf' would poison every sum it reaches."""
    if isinstance(raw, str) and currency:
        own = CURRENCY_SYMBOLS.get(currency.upper())
        if any(sym in raw for sym in CURRENCY_SYMBOLS.values() if sym != own):
            raise OtherCurrency("amount in another currency")
    if not isinstance(raw, str):
        v = float(raw)
        if not math.isfinite(v):
            raise ValueError("not a finite number")
        return v
    s = _NUM_NOISE.sub("", raw)
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    if not _PLAIN_NUMBER.fullmatch(s):
        raise ValueError("not a plain number")
    return float(s)


_MONTH_NAME_FORMATS = ("%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y")
_NUMERIC_DATE = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?:[ T].*)?")


def _other_date(s: str, order: str | None) -> date:
    """Dates exports write other than ISO. A month name is never ambiguous. 05/10/2026 is, so it is read only when
    the company has said how its exports write dates (date_order), or when one part cannot be a month."""
    for fmt in _MONTH_NAME_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    m = _NUMERIC_DATE.fullmatch(s)
    if not m:
        raise ValueError("not a date")
    a, b, year = (int(x) for x in m.groups())
    if order is None:
        if a > 12 >= b:
            order = "DMY"
        elif b > 12 >= a:
            order = "MDY"
        else:
            raise ValueError("ambiguous day and month")
    day, month = (a, b) if order == "DMY" else (b, a)
    return date(year, month, day)


def _local_date(dt: datetime, tz: ZoneInfo | None) -> date:
    """A timestamp that carries an offset counts on the company's own calendar day, not the UTC one."""
    return (dt.astimezone(tz) if tz and dt.tzinfo else dt).date()


def _convert(spec: FieldSpec, raw, tz: ZoneInfo | None = None, date_order: str | None = None, currency: str | None = None):
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    if spec.type == "string":
        return str(raw).strip()
    if spec.type == "integer":
        v = _number(raw, currency)
        if v != int(v):
            raise ValueError("not an integer")
        return int(v)
    if spec.type == "number":
        return _number(raw, currency)
    if spec.type == "boolean":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("true", "yes", "1", "y"):
            return True
        if s in ("false", "no", "0", "n"):
            return False
        raise ValueError("not a boolean")
    if spec.type == "date":
        if isinstance(raw, datetime):
            return _local_date(raw, tz).isoformat()
        if isinstance(raw, date):
            return raw.isoformat()
        s = str(raw).strip()
        if spec.format:
            return _local_date(datetime.strptime(s, spec.format), tz).isoformat()
        try:
            return _local_date(datetime.fromisoformat(s), tz).isoformat()
        except ValueError:
            pass
        try:
            return date.fromisoformat(s[:10]).isoformat()
        except ValueError:
            return _other_date(s, date_order).isoformat()
    raise ValueError("unknown type")


def normalise(ds: DatasetConfig, raw: dict, tz: ZoneInfo | None = None, date_order: str | None = None,
              latest: date | None = None, currency: str | None = None) -> dict:
    """One validated row. `latest` is today: an event dated after it (a typo such as 2027 for 2026) is refused, so it can
    never count in this week's figures. Plan dates (due dates, ETAs) are not event dates and may be in the future."""
    out = {}
    for name, spec in ds.fields.items():
        col = spec.column or name
        try:
            v = _convert(spec, raw.get(col), tz, date_order, currency)
        except OtherCurrency:
            raise RowError(name, f"amount in another currency than {currency}", _shown(spec, raw.get(col))) from None
        except (ValueError, TypeError):
            raise RowError(name, f"invalid {spec.type}", _shown(spec, raw.get(col))) from None
        if v is None and spec.default is not None:
            v = _convert(spec, spec.default, tz, date_order)
        if v is None and (spec.required or name in ds.key):
            raise RowError(name, "required")
        if latest is not None and name == ds.date_field and v is not None and v > latest.isoformat():
            raise RowError(name, "date after today", _shown(spec, raw.get(col)))
        out[name] = v
    return out


def _shown(spec: FieldSpec, value):
    """A value as it may appear in a message people read: personal data (sensitive fields) never does."""
    return "•••" if spec.sensitive and value not in (None, "") else value


def _canonical_spellings(ds: DatasetConfig, rows: list[dict]) -> None:
    """'Delivered', ' SHIPPED ' and 'delivered' are one value written differently. Within one file every text value
    takes its most common spelling, so a filter on 'delivered' or a group by channel does not split or miss rows.
    Changes the rows in place."""
    for name, spec in ds.fields.items():
        if spec.type != "string":
            continue
        spellings: dict[str, Counter] = {}
        for r in rows:
            v = r.get(name)
            if isinstance(v, str):
                spellings.setdefault(v.casefold(), Counter())[v] += 1
        canon = {k: c.most_common(1)[0][0] for k, c in spellings.items() if len(c) > 1}
        if canon:
            for r in rows:
                v = r.get(name)
                if isinstance(v, str) and v.casefold() in canon:
                    r[name] = canon[v.casefold()]


# ---------------------------------------------------------------- ingestion

def _jsonable(raw: dict) -> dict:
    """The source row as received, kept for drill-down and audit (dates and decimals become text)."""
    return {str(k): (v if v is None or isinstance(v, (str, int, float, bool)) else str(v)) for k, v in raw.items()}


def _upsert(db: Session, rows: list[dict]):
    if not rows:
        return
    ins = (postgresql.insert if is_postgres() else sqlite.insert)(Record).values(rows)
    db.execute(ins.on_conflict_do_update(index_elements=["tenant_id", "dataset", "key"],
                                         set_={"data": ins.excluded.data, "raw": ins.excluded.raw,
                                               "occurred_on": ins.excluded.occurred_on, "synced_at": ins.excluded.synced_at}))


def _prune_snapshots(db: Session, tenant_id: str, dataset: str, before: date) -> None:
    db.execute(delete(Record).where(Record.tenant_id == tenant_id, Record.dataset == dataset, Record.occurred_on < before))


def _safe_error(e: Exception) -> str:
    msg = str(e).splitlines()[0][:300]
    return re.sub(r"\w+://[^\s'\"]+", "<connection>", msg)  # never leak connection strings


def _missing_columns(ds: DatasetConfig, first: dict) -> list[str]:
    """Columns the dataset cannot do without that are absent from the file altogether (checked on the first row)."""
    return [spec.column or name for name, spec in ds.fields.items()
            if (spec.required or name in ds.key) and spec.default is None and (spec.column or name) not in first]


def _causes(errors: list[tuple[int, RowError]], n: int = 3) -> list[str]:
    """The most common reasons rows failed, biggest first, each with one example row."""
    counts = Counter(e.cause for _, e in errors)
    first = {}
    for row, e in errors:
        first.setdefault(e.cause, f"row {row}: {e}")
    return [f"{first[c]} ({k:,} row{'s' if k != 1 else ''} like this)" for c, k in counts.most_common(n)]


def _drop_conflicting_keys(ds: DatasetConfig, good: list[tuple[dict, dict, int]]):
    """Rows of one file that share a key. Exact copies are kept once; rows that disagree are all left out, since there is
    no way to tell which one is right, and the figures for that key show as unknown instead of picking one at random.

    Returns (kept rows, row errors, {conflicting key: (the key as people may see it, its row numbers)})."""
    by_key: dict[str, list[tuple[dict, dict, int]]] = {}
    for item in good:
        by_key.setdefault("|".join(str(item[0][k]) for k in ds.key), []).append(item)
    kept, errors, conflicts = [], [], {}
    for natural, items in by_key.items():
        if len({json.dumps(r, sort_keys=True, default=str) for r, _, _ in items}) == 1:
            kept.append(items[0])
            continue
        shown = {k: _shown(ds.fields[k], items[0][0][k]) if k in ds.fields else items[0][0][k] for k in ds.key}
        conflicts[natural] = ("|".join(str(v) for v in shown.values()), [row for _, _, row in items])  # (as shown, rows)
        key_desc = ", ".join(f"{k}={v}" for k, v in shown.items())
        errors += [(row, RowError(" + ".join(ds.key), f"{len(items)} rows with different values for the same key", key_desc))
                   for _, _, row in items]
    return kept, errors, conflicts


def _report_stale(db: Session, cfg: TenantConfig, source_id: str, name: str, ds: DatasetConfig) -> str | None:
    """A file can arrive on time and still be old inside: the export keeps sending the same days. Someone owns that.
    Returns why the data is out of date, or None."""
    if ds.max_data_age_days is None:
        return None
    newest = latest_snapshot(db, cfg.id, name)
    if newest is not None and (today(cfg) - newest).days <= ds.max_data_age_days:
        clear_issues(db, cfg, source_id, name, "stale")
        return None
    raise_issue(db, cfg, source_id, name, [
        f"The newest row in {ds.label} is dated {newest.strftime('%d %b %Y') if newest else 'nowhere'}, more than "
        f"{ds.max_data_age_days} days ago, although the file itself arrived on time.",
        "The export has probably stopped picking up new days. Figures from it are marked out of date and no alerts are "
        "raised from them until new rows arrive."], topic="stale", title="Data has stopped moving")
    return f"newest row {newest.strftime('%d %b %Y')}" if newest else "no dated rows"


def _report_signs(db: Session, cfg: TenantConfig, source_id: str, name: str, ds: DatasetConfig, good: list) -> None:
    rule = ds.signs
    if rule is None:
        return
    pos, neg = {c.casefold() for c in rule.positive}, {c.casefold() for c in rule.negative}
    wrong: dict[str, list] = {}
    for r, _, row in good:
        cat, v = str(r.get(rule.by) or "").casefold(), r.get(rule.field)
        if isinstance(v, (int, float)) and ((cat in pos and v < 0) or (cat in neg and v > 0)):
            wrong.setdefault(r.get(rule.by), []).append((row, v))
    if not wrong:
        clear_issues(db, cfg, source_id, name, "signs")
        return
    n = sum(map(len, wrong.values()))
    shown = "; ".join(f"{cat}: {len(rows)} (for example row {rows[0][0]}, {rows[0][1]:,.2f})" for cat, rows in sorted(wrong.items(), key=str))
    raise_issue(db, cfg, source_id, name, [
        f"{n} of {len(good)} rows have a {rule.field} with the opposite sign to the company's convention: {shown}.",
        f"Expected positive: {', '.join(rule.positive) or 'none'}; negative: {', '.join(rule.negative) or 'none'}. "
        "The rows are kept as sent, so totals such as profit may be wrong until the export is corrected."],
        topic="signs", title="Amounts with the wrong sign")


def _report_blanks(db: Session, cfg: TenantConfig, source_id: str, name: str, ds: DatasetConfig, rows_: list[dict]) -> None:
    """Rows whose amount is blank are kept but cannot be added up, so totals built on that field are short by them.
    Say how many, instead of letting a total quietly look complete. Fields with a default (blank means 0) are fine."""
    summed = {m.value for m in cfg.metrics.values() if m.dataset == name and m.agg in ("sum", "avg") and m.value in ds.fields}
    facts = []
    for f in sorted(summed):
        spec = ds.fields[f]
        if spec.type not in ("number", "integer") or spec.default is not None or spec.required:
            continue
        blank = sum(1 for r in rows_ if r.get(f) is None)
        if blank:
            facts.append(f"{blank:,} of {len(rows_):,} rows have no {f}, so every total of {f} leaves them out.")
    if not facts:
        clear_issues(db, cfg, source_id, name, "blank_values")
        return
    raise_issue(db, cfg, source_id, name, facts + [
        "If blank means zero, say so in the company settings (default: 0); otherwise the export needs the values."],
        topic="blank_values", title="Values missing")


def _reject(db: Session, cfg: TenantConfig, source_id: str, name: str, ds: DatasetConfig, summary: str, facts: list[str], **payload):
    events.emit(db, cfg.id, "DATA_REJECTED", f"{ds.label}: {summary}. Previous data kept.", dataset=name, source=source_id, **payload)
    raise_issue(db, cfg, source_id, name, facts + ["The previous data is still being used, and is shown as out of date."])


def _sync_dataset(db: Session, cfg: TenantConfig, source_id: str, name: str, ds: DatasetConfig, snap_day: date) -> dict:
    """Read one dataset. Returns {written, rejected, new, problem}; problem is set when the file was refused."""
    src, tz = cfg.sources[source_id], ZoneInfo(cfg.timezone)
    changed = _file_changed_at(src, ds)
    if changed is not None and utcnow() - changed > timedelta(minutes=ds.max_age_minutes):
        # read on time but never rewritten: the export behind it has stopped, so today's figures cannot come from it
        local = changed.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).strftime("%d %b %Y %H:%M")
        _reject(db, cfg, source_id, name, ds, f"file not updated since {local}",
                [f"The {ds.label} file has not changed since {local}, longer than the {ds.max_age_minutes} minutes allowed.",
                 "The export that writes it has probably stopped, so its figures are not used as today's."], stale_since=local)
        return {"written": 0, "rejected": 0, "new": None, "problem": f"file not updated since {local}"}
    rows_in = iter(CONNECTORS[src.type](src, ds))
    first = next(rows_in, None)
    if first is None and (src.type == "csv" or ds.mode == "snapshot"):
        # a header with no rows is an export that broke, not a day when nothing happened
        _reject(db, cfg, source_id, name, ds, "batch rejected, the file has no rows",
                [f"The latest {ds.label} file has no rows, only a header (or nothing at all)."], empty=True)
        return {"written": 0, "rejected": 0, "new": None, "problem": "no rows"}
    if first is not None and (missing := _missing_columns(ds, first)):
        cols = ", ".join(f"'{c}'" for c in missing)
        facts = [f"The latest {ds.label} file has no column named {cols}, so none of its rows could be read.",
                 f"Columns in the file: {', '.join(repr(str(c)) for c in list(first)[:30])}."]
        if len(first) == 1 and (sep := next((c for c in ";\t|" if c in str(next(iter(first)))), None)):
            facts.append(f"The columns look separated by {'a tab' if sep == chr(9) else repr(sep)} instead of "
                         f"{repr(src.delimiter)}: the export's separator setting has changed.")
        _reject(db, cfg, source_id, name, ds, f"batch rejected, the file has no column {cols}", facts, missing_columns=missing)
        return {"written": 0, "rejected": 0, "new": None, "problem": f"missing column {cols}"}
    good, errors = [], []
    for i, raw in enumerate(itertools.chain([first] if first is not None else [], rows_in)):
        try:
            good.append((normalise(ds, raw, tz, cfg.date_order, snap_day, cfg.currency), _jsonable(raw), i + 2))
        except RowError as e:
            errors.append((i + 2, e))
    _canonical_spellings(ds, [r for r, _, _ in good])
    total = len(good) + len(errors)
    good, dupes, conflicts = _drop_conflicting_keys(ds, good)
    errors = sorted(errors + dupes, key=lambda x: x[0])
    causes = _causes(errors)
    if total and len(errors) / total > ds.max_reject_ratio:
        _reject(db, cfg, source_id, name, ds, f"batch rejected, {len(errors)} of {total} rows invalid (limit {ds.max_reject_ratio:.0%})",
                [f"{len(errors)} of {total} rows in the latest {ds.label} file could not be read, so the whole file was refused "
                 f"(more than {ds.max_reject_ratio:.0%} were invalid)."] + [f"Most common problem: {c}" for c in causes],
                invalid=len(errors), total=total, examples=causes)
        return {"written": 0, "rejected": len(errors), "new": None, "problem": f"{len(errors)} of {total} rows invalid"}
    now = utcnow()
    before = _count(db, cfg.id, name)
    if ds.mode == "snapshot":  # a second export the same day replaces the first: a row it no longer has is gone, not kept
        db.execute(delete(Record).where(Record.tenant_id == cfg.id, Record.dataset == name, Record.occurred_on == snap_day))
    batch = []
    for r, raw_row, _ in good:
        natural = "|".join(str(r[k]) for k in ds.key)
        if ds.mode == "snapshot":
            key, occurred = f"{natural}|{snap_day.isoformat()}", snap_day
        else:
            key = natural
            occurred = date.fromisoformat(r[ds.date_field]) if ds.date_field and r.get(ds.date_field) else None
        batch.append({"tenant_id": cfg.id, "dataset": name, "key": key, "occurred_on": occurred, "data": r, "raw": raw_row,
                      "synced_at": now})
        if len(batch) >= BATCH:
            _upsert(db, batch)
            batch = []
    _upsert(db, batch)
    if ds.mode == "snapshot":
        _prune_snapshots(db, cfg.id, name, snap_day - timedelta(days=ds.keep_snapshot_days))
    clear_issues(db, cfg, source_id, name)
    if conflicts:
        shown = list(conflicts.values())[:5]
        raise_issue(db, cfg, source_id, name, [
            f"{len(conflicts):,} {' + '.join(ds.key)} values appear on more than one row of the latest {ds.label} file, with different "
            f"values, so the platform cannot tell which row is right. All {sum(len(rows) for _, rows in conflicts.values()):,} of those rows were left out.",
            "For example: " + "; ".join(f"{k} (rows {', '.join(map(str, rows[:4]))})" for k, rows in shown) + ".",
            "Their figures are shown as unknown until the file has one row per key. If several rows per key are expected "
            "(for example one per location), the key in the company settings needs that column too."],
            topic="duplicate_keys", title="Rows that contradict each other")
    else:
        clear_issues(db, cfg, source_id, name, "duplicate_keys")
    old = _report_stale(db, cfg, source_id, name, ds)
    _report_signs(db, cfg, source_id, name, ds, good)
    _report_blanks(db, cfg, source_id, name, ds, [r for r, _, _ in good])
    if errors:
        events.emit(db, cfg.id, "DATA_REJECTED", f"{ds.label}: {len(errors)} invalid rows skipped of {total}",
                    dataset=name, source=source_id, invalid=len(errors), total=total, examples=causes)
    # snapshots replace the whole picture daily; only upserts have "new" rows
    return {"written": len(good), "rejected": len(errors), "new": _count(db, cfg.id, name) - before if ds.mode == "upsert" else None,
            "problem": None, "old": old}


def sync_source(db: Session, cfg: TenantConfig, source_id: str) -> dict:
    """Pull every dataset bound to a source. Returns {dataset: rows_written}.

    Each dataset is read on its own: one missing or refused file makes only that dataset stale. The source as a whole
    fails only when none of its datasets could be read."""
    src = cfg.sources[source_id]
    state = db.get(SourceState, (cfg.id, source_id)) or SourceState(tenant_id=cfg.id, source_id=source_id, status="NEVER",
                                                                      error="", rows=0, rejected=0, consecutive_failures=0)
    db.add(state)
    state.last_attempt = utcnow()
    written, rejected_total, results, new_rows = 0, 0, {}, {}
    failed: dict[str, str] = {}  # dataset → why it could not be used this time
    old: dict[str, str] = {}  # dataset → read fine, but its newest row is too old to count as current
    unreadable: dict[str, Exception] = {}
    snap_day = today(cfg)
    names = [n for n, ds in cfg.datasets.items() if ds.source == source_id]
    try:
        for name in names:
            try:
                with db.begin_nested():  # all of a file or none of it: a failure halfway leaves the previous data intact
                    r = _sync_dataset(db, cfg, source_id, name, cfg.datasets[name], snap_day)
            except Exception as e:  # noqa: BLE001 — one unreadable file must not take its neighbours down
                unreadable[name] = e
                failed[name], results[name] = _safe_error(e), 0
                continue
            results[name] = r["written"]
            written += r["written"]
            rejected_total += r["rejected"]
            if r["new"] is not None:
                new_rows[name] = r["new"]
            if r["problem"]:
                failed[name] = r["problem"]
            elif r.get("old"):
                old[name] = r["old"]
        if names and len(unreadable) == len(names):
            raise next(iter(unreadable.values()))
        for name, e in unreadable.items():
            ds = cfg.datasets[name]
            events.emit(db, cfg.id, "DATA_SOURCE_FAILURE", f"{ds.label} could not be read: {failed[name]}. Its data is now treated as stale.",
                        source=source_id, dataset=name, error=failed[name])
            raise_issue(db, cfg, source_id, name, [f"The {ds.label} file could not be read: {failed[name]}.",
                                                    "Figures that depend on it are marked out of date until it works again."])
        state.status, state.last_success = "OK", utcnow()
        state.error = "; ".join(f"{cfg.datasets[n].label}: {why}" for n, why in failed.items())[:2000]
        state.failed_datasets = sorted(failed)
        state.rows, state.rejected, state.consecutive_failures = written, rejected_total, 0
        clear_issues(db, cfg, source_id, None)
        parts = []
        if failed:
            parts.append(f"{len(failed)} of {len(names)} files could not be used ({', '.join(cfg.datasets[n].label for n in failed)})")
        if old:
            parts.append(f"{len(old)} out of date ({', '.join(f'{cfg.datasets[n].label}: {why}' for n, why in old.items())})")
        tail = " · ".join(parts) or "CURRENT"
        events.emit(db, cfg.id, "DATA_SYNC", f"{src.label or source_id} synced · {written:,} rows · {tail}",
                    source=source_id, datasets=results, new_rows=new_rows, failed=failed, area=_area_for_source(cfg, source_id))
    except Exception as e:  # connector failures must never crash the worker
        state.status, state.error = "FAILED", _safe_error(e)
        state.consecutive_failures += 1
        events.emit(db, cfg.id, "DATA_SOURCE_FAILURE",
                    f"{src.label or source_id} sync failed ({state.consecutive_failures}×): {state.error}. Its data is now treated as stale.",
                    source=source_id, error=state.error, failures=state.consecutive_failures)
        if state.consecutive_failures == 2:  # once is noise; twice in a row is worth interrupting someone
            from . import notify
            notify.send(db, cfg, kind="source_failure", severity="HIGH", title=f"Data source failing · {src.label or source_id}",
                        verified=f"{src.label or source_id} failed to sync twice in a row; its data is now treated as stale.")
        if state.consecutive_failures >= 2:
            raise_issue(db, cfg, source_id, None,
                        [f"{src.label or source_id} could not be read {state.consecutive_failures} times in a row.", f"Last error: {state.error}",
                         "Figures that depend on it are marked out of date until it works again."])
    return results


# ---------------------------------------------------------------- data problems become owned tasks (Lock 15)

def _issue_key(source_id: str, dataset: str | None, topic: str | None = None) -> str:
    return f"data:{source_id}:{dataset or '*'}" + (f":{topic}" if topic else "")


def _open_issue(db: Session, cfg: TenantConfig, key: str):
    from .models import WorkItem
    return db.execute(select(WorkItem).where(WorkItem.tenant_id == cfg.id, WorkItem.type == "DATA_ISSUE", WorkItem.dedupe_key == key,
                                             WorkItem.status == "NEEDS_HUMAN_REVIEW")).scalars().first()


def steward(cfg: TenantConfig, source_id: str) -> str:
    """The AI employee whose area relies on this source keeps its data problems on its board."""
    area = _area_for_source(cfg, source_id)
    return next((k for k, e in cfg.employees.items() if e.area == area and not e.answers_questions), cfg.question_employee())


def raise_issue(db: Session, cfg: TenantConfig, source_id: str, dataset: str | None, facts: list[str], topic: str | None = None,
                title: str = "Fix the data feed") -> None:
    """One open task per broken feed (or refused file, or gap a rule found); repeats update it rather than piling up."""
    from . import work
    src = cfg.sources[source_id]
    label = cfg.datasets[dataset].label if dataset else (src.label or source_id)
    who = src.owner or "the person responsible for this data (not yet named in the company settings)"
    finding = {"outcome": "DATA_STALE", "headline": f"{label} needs fixing", "evidence_quality": "DATA_STALE", "ai": False,
               "statements": [{"kind": "FACT", "text": f, "refs": []} for f in facts],
               "quality_basis": ["Reported by the data connection itself"], "impact": "", "recommendation": None,
               "unknowns": [], "owner": who}
    key = _issue_key(source_id, dataset, topic)
    item = _open_issue(db, cfg, key)
    if item is None:
        item = work.create(db, cfg, employee=steward(cfg, source_id), type_="DATA_ISSUE", title=f"{title} · {label}",
                           origin="data", priority="HIGH", subject=label, dedupe_key=key, status="NEEDS_HUMAN_REVIEW",
                           context={"source": source_id, "dataset": dataset}, next_step=f"Waiting for {who} to fix it",
                           msg=f"Data problem logged · {label} · owner: {who}")
    item.finding = finding
    item.updated_at = utcnow()


def clear_issues(db: Session, cfg: TenantConfig, source_id: str, dataset: str | None, topic: str | None = None) -> None:
    """Close the matching data-problem task once the data comes through cleanly again."""
    from . import work
    item = _open_issue(db, cfg, _issue_key(source_id, dataset, topic))
    if item is not None:
        work.transition(db, cfg, item, "COMPLETED", "Fixed · the data came through cleanly", outcome="RESOLVED")


def _count(db: Session, tenant_id: str, dataset: str) -> int:
    return db.execute(select(func.count()).select_from(Record).where(Record.tenant_id == tenant_id, Record.dataset == dataset)).scalar()


def _area_for_source(cfg: TenantConfig, source_id: str) -> str | None:
    """The world pulses the area whose tools read this source's datasets."""
    dsets = {n for n, d in cfg.datasets.items() if d.source == source_id}
    for t in cfg.tools.values():
        if t.dataset in dsets or any(cfg.metrics[m].dataset in dsets for m in t.metrics if m in cfg.metrics):
            return t.area
    return None


def sources_due(db: Session, cfg: TenantConfig) -> list[str]:
    from .health import paused_set
    now = utcnow()
    due = []
    paused = paused_set(db, cfg.id, "source")
    for sid, src in cfg.sources.items():
        if sid in paused:  # a paused feed is not read; its data ages into "out of date" as it should
            continue
        st = db.get(SourceState, (cfg.id, sid))
        if st is None or st.last_attempt is None:
            due.append(sid)
            continue
        wait = src.schedule_minutes * (2 ** min(st.consecutive_failures, 4) if st.status == "FAILED" else 1)
        if now - st.last_attempt >= timedelta(minutes=min(wait, 24 * 60)):
            due.append(sid)
    return due


def freshness(db: Session, cfg: TenantConfig, dataset: str) -> tuple[str, str]:
    """(CURRENT | STALE | NEVER_SYNCED, human-readable last sync). Stale data may be shown but never treated as current."""
    ds = cfg.datasets[dataset]
    st = db.get(SourceState, (cfg.id, ds.source))
    if st is None or st.last_success is None:
        return "NEVER_SYNCED", "never"
    age = utcnow() - st.last_success
    local = st.last_success.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(cfg.timezone))
    label = local.strftime("%d %b %H:%M")
    if st.status == "FAILED" or dataset in (st.failed_datasets or []) or age > timedelta(minutes=ds.max_age_minutes):
        return "STALE", label
    if ds.max_data_age_days is not None:  # read on time, but is the data inside it still moving?
        newest = latest_snapshot(db, cfg.id, dataset)
        if newest is None or (today(cfg) - newest).days > ds.max_data_age_days:
            return "STALE", f"{label} · newest record {newest.strftime('%d %b %Y') if newest else 'none'}"
    return "CURRENT", label


def latest_snapshot(db: Session, tenant_id: str, dataset: str):
    return db.execute(select(func.max(Record.occurred_on)).where(Record.tenant_id == tenant_id, Record.dataset == dataset)).scalar()


def rows(db: Session, cfg: TenantConfig, dataset: str, since: date | None = None, until: date | None = None,
         with_ids: bool = False) -> list:
    """Current rows of a dataset (latest snapshot for snapshot datasets). with_ids gives (record id, row) pairs."""
    ds = cfg.datasets[dataset]
    q = select(Record.id, Record.data).where(Record.tenant_id == cfg.id, Record.dataset == dataset)
    if ds.mode == "snapshot":
        snap = latest_snapshot(db, cfg.id, dataset)
        if snap is None:
            return []
        q = q.where(Record.occurred_on == snap)
    else:
        if since is not None:
            q = q.where(Record.occurred_on >= since)
        if until is not None:
            q = q.where(Record.occurred_on <= until)
    out = db.execute(q).all()
    return [(i, d) for i, d in out] if with_ids else [d for _, d in out]
