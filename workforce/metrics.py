"""Deterministic metric engine. The single calculator of record for business numbers:
the AI reads these results, it never produces them."""
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy.orm import Session

from . import data, expr
from .tenancy import TenantConfig


class MetricEngine:
    """Computes metrics for one evaluation pass, memoising results so a rule pass reads each dataset once."""

    def __init__(self, db: Session, cfg: TenantConfig):
        self.db, self.cfg = db, cfg
        self._rows: dict[tuple, list[dict]] = {}
        self._exact: dict[str, dict[tuple, float | None]] = {}
        self._memo: dict[str, dict[tuple, float | None]] = {}

    def _dataset_rows(self, dataset: str, window: int | None, offset: int = 0) -> list[dict]:
        key = (dataset, window, offset)
        if key not in self._rows:
            until = data.today(self.cfg) - timedelta(days=offset)
            since = until - timedelta(days=window - 1) if window else None
            # bounded by today even with no offset: a row dated in the future is never part of "the last N days"
            self._rows[key] = data.rows(self.db, self.cfg, dataset, since, until if window or offset else None, with_ids=True)
        return self._rows[key]

    def values(self, name: str, exact: bool = False) -> dict[tuple, float | None]:
        """{group_key_tuple: value} for every group of the metric, rounded to 4 decimals for people to read.
        exact=True gives full precision, for comparisons: rounding first would make 0.70 of a ratio read as below 0.70."""
        if not exact:
            if name not in self._memo:
                self._memo[name] = {g: (round(v, 4) if isinstance(v, float) else v) for g, v in self.values(name, True).items()}
            return self._memo[name]
        if name in self._exact:
            return self._exact[name]
        m = self.cfg.metrics[name]
        out: dict[tuple, float | None] = {}
        if m.dataset:
            groups: dict[tuple, list] = defaultdict(list)
            distinct: dict[tuple, set] = defaultdict(set)
            unknown: set[tuple] = set()  # groups with a row whose value could not be worked out (a blank input)
            for _, r in self._dataset_rows(m.dataset, m.window_days, m.window_offset_days):
                if not _matches(r, m.where):
                    continue
                g = tuple(r.get(b) for b in m.by)
                if m.agg == "count":
                    groups[g].append(1)
                    continue
                v = expr.evaluate(m.value, self._numeric(m.dataset, r)) if m.agg != "count_distinct" else r.get(m.value)
                if v is None:
                    unknown.add(g)
                    continue
                if m.agg == "count_distinct":
                    distinct[g].add(v)
                    groups[g]
                else:
                    groups[g].append(v)
            for g, vals in groups.items():
                if m.agg in ("sum", "count"):
                    v = float(sum(vals))
                elif m.agg == "avg":
                    v = sum(vals) / len(vals) if vals else None
                elif m.agg == "min":
                    v = min(vals) if vals else None
                elif m.agg == "max":
                    v = max(vals) if vals else None
                else:
                    v = float(len(distinct[g]))
                if v is not None and m.per_day and m.window_days:
                    v = v / m.window_days
                out[g] = v
            for g in unknown - set(groups):  # every row blank: unknown, not missing (and never a zero)
                out[g] = None
            if not m.by and not out:
                out[()] = 0.0 if self._absent_is_zero(m) else None
        else:
            parts = {p: self.values(p, True) for p in expr.names(m.formula)}
            keys = set()
            for p, vals in parts.items():
                if list(self.cfg.metrics[p].by) == list(m.by):
                    keys |= set(vals)
            for g in keys:
                env = {}
                for p, vals in parts.items():
                    pby = self.cfg.metrics[p].by
                    sub = tuple(g[m.by.index(b)] for b in pby if b in m.by)
                    env[p] = vals.get(sub, 0.0 if self._absent_is_zero(self.cfg.metrics[p]) else None)
                out[g] = expr.evaluate(m.formula, env)
        self._exact[name] = out
        return out

    def _numeric(self, dataset: str, row: dict) -> dict:
        """Numeric view of a row for value expressions; each date field also exposes days_until_<field>."""
        env = {k: v for k, v in row.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        today = data.today(self.cfg)
        for f, spec in self.cfg.datasets[dataset].fields.items():
            if spec.type == "date" and row.get(f):
                env[f"days_until_{f}"] = (date.fromisoformat(row[f]) - today).days
        return env

    def record_ids(self, name: str, filters: dict) -> list[int]:
        """The source records behind a metric value, for drill-down. Derived metrics combine their parts."""
        m = self.cfg.metrics[name]
        if m.formula:
            out: list[int] = []
            for part in expr.names(m.formula):
                pf = {k: v for k, v in filters.items() if k in self.cfg.metrics[part].by}
                out += self.record_ids(part, pf)
            return sorted(set(out))
        dims = {k: v for k, v in filters.items() if k in m.by}
        return [rid for rid, r in self._dataset_rows(m.dataset, m.window_days, m.window_offset_days)
                if _matches(r, m.where) and all(str(r.get(k)) == str(v) for k, v in dims.items())]

    def lookup(self, name: str, filters: dict) -> list[dict]:
        """Rows of {dimension: value, ..., value} matching the given dimension filters."""
        m = self.cfg.metrics[name]
        rows = []
        for g, v in self.values(name).items():
            dims = dict(zip(m.by, g))
            if all(str(dims.get(k)) == str(fv) for k, fv in filters.items() if k in dims):
                rows.append({**dims, "value": v})
        return rows

    def for_group(self, name: str, dim: str | None, group, exact: bool = False) -> float | None:
        m = self.cfg.metrics[name]
        vals = self.values(name, exact)
        if not m.by:
            return vals.get(())
        if dim is None or m.by != [dim]:
            return None
        if (group,) not in vals:
            if m.dataset:
                return 0.0 if self._absent_is_zero(m) else None
            return self._derived_zero(name, group, exact)
        return vals.get((group,))

    def _absent_is_zero(self, m) -> bool:
        """A group with no rows in an additive metric over events is a real zero (no sales that week). In a snapshot
        (a stock count, a scorecard) a missing group was simply not reported, so it is unknown, never zero."""
        return bool(m.dataset) and m.agg in ("sum", "count", "count_distinct") and self.cfg.datasets[m.dataset].mode != "snapshot"

    def _derived_zero(self, name: str, group, exact: bool = False):
        m = self.cfg.metrics[name]
        if not m.formula:
            return None
        env = {p: self.for_group(p, m.by[0] if m.by else None, group, True) for p in expr.names(m.formula)}
        v = expr.evaluate(m.formula, env)
        return v if exact or not isinstance(v, float) else round(v, 4)


def same_value(a, b) -> bool:
    """Category values match whatever their case: 'Delivered' is 'delivered'."""
    if isinstance(a, str) and isinstance(b, str):
        return a.casefold() == b.casefold()
    return a == b


def _matches(row: dict, where: dict) -> bool:
    for k, want in where.items():
        v = row.get(k)
        if not any(same_value(v, w) for w in (want if isinstance(want, list) else [want])):
            return False
    return True
