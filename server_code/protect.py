# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the m2py repo. This file is produced by sync_to_api.py.
# Edit the engine in the m2py repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""protect — statistical disclosure control for tabular data and results.

See docs/specs/ for design, README.md for usage, BACKGROUND.md for the SDC primer.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

import numpy as np
import pandas as pd

# Public API: data-side verbs, meta verbs, audit + risk reporting.
__all__ = [
    "TransformLog",
    "noise",
    "jitter",
    "winsorize",
    "bin",
    "coarsen",
    "year",
    "month",
    "diff",
    "shorten",
    "collapse",
    "pseudonymize",
    "insert",
    "eliminate",
    "swap",
    "suppress",
    "risk",
    "RiskReport",
    "protect",
    "profile",
]


# ============================================================================
# TransformLog
# ============================================================================


@dataclass
class TransformLog:
    """Audit trail for protection operations.

    Returned by `protect()` and optionally by individual verbs with `audit=True`.
    Designed as documentation for HIPAA Expert Determination, GDPR records,
    and microdata.no method reporting.
    """
    entries: list[dict] = field(default_factory=list)

    def add(self, *, function: str, columns: Sequence[str] | None = None,
            params: dict | None = None, rows_affected: int | None = None,
            units_affected: int | None = None, notes: str | None = None) -> None:
        """Append an operation entry with timestamp and audit metadata."""
        self.entries.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "function": function,
            "columns": list(columns) if columns else None,
            "params": params or {},
            "rows_affected": rows_affected,
            "units_affected": units_affected,
            "notes": notes,
        })

    def to_text(self) -> str:
        """Return a human-readable text rendering of all entries."""
        lines = []
        for i, e in enumerate(self.entries, 1):
            cols = ", ".join(e["columns"]) if e["columns"] else "-"
            params = ", ".join(f"{k}={v!r}" for k, v in e["params"].items())
            lines.append(
                f"{i}. {e['function']}({cols}) [{params}] "
                f"rows={e['rows_affected']}, units={e['units_affected']}"
            )
            if e.get("notes"):
                lines.append(f"   note: {e['notes']}")
        return "\n".join(lines) if lines else "(empty log)"

    def to_json(self) -> str:
        """Return all entries as a JSON string."""
        return json.dumps({"entries": self.entries}, default=str, indent=2)

    def summary(self) -> dict:
        """Return aggregate counts: total operations and operations per function."""
        by_function: dict[str, int] = {}
        for e in self.entries:
            by_function[e["function"]] = by_function.get(e["function"], 0) + 1
        return {
            "total_operations": len(self.entries),
            "by_function": by_function,
        }

    def __len__(self) -> int:
        return len(self.entries)


# ============================================================================
# Helpers
# ============================================================================


def _resolve_random_state(random_state: int | np.random.Generator | None) -> np.random.Generator:
    """Convert int seed / Generator / None to a Generator."""
    if isinstance(random_state, np.random.Generator):
        return random_state
    return np.random.default_rng(random_state)


def _validate_columns(data: pd.DataFrame, columns: str | Sequence[str]) -> list[str]:
    """Normalize columns argument to a list and verify each is in `data`."""
    if isinstance(columns, str):
        columns = [columns]
    columns = list(columns)
    missing = [c for c in columns if c not in data.columns]
    if missing:
        raise KeyError(f"Columns {missing} not in DataFrame")
    return columns


def _select_share(
    data: pd.DataFrame,
    share: float,
    unit_id: str | None,
    rng: np.random.Generator,
) -> pd.Series:
    """Boolean mask aligned to `data.index` selecting `share` of units (or rows).

    If `unit_id` is given, selection is at unit granularity: a whole unit's
    rows are all True or all False. Otherwise, rows are selected independently.
    """
    if share <= 0:
        return pd.Series(False, index=data.index)
    if share >= 1:
        return pd.Series(True, index=data.index)

    if unit_id is None:
        n = len(data)
        n_select = int(round(n * share))
        choice = rng.choice(n, size=n_select, replace=False)
        mask = np.zeros(n, dtype=bool)
        mask[choice] = True
        return pd.Series(mask, index=data.index)

    units = data[unit_id].unique()
    n_select = int(round(len(units) * share))
    selected = set(rng.choice(units, size=n_select, replace=False))
    return data[unit_id].isin(selected)


def _apply_per_unit(
    data: pd.DataFrame,
    unit_id: str,
    fn: Callable[[Any], Any],
) -> pd.Series:
    """Apply `fn` once per unit, broadcast to all rows of that unit.

    `fn` is called with the unit's id and returns a scalar; the result is
    indexed back to `data.index`.
    """
    units = data[unit_id].unique()
    draws = {u: fn(u) for u in units}
    return data[unit_id].map(draws)


def _check_unit_invariant(
    data: pd.DataFrame,
    columns: Sequence[str],
    unit_id: str,
) -> None:
    """Warn if any declared invariant column varies within `unit_id`."""
    for col in columns:
        n_distinct = data.groupby(unit_id)[col].nunique()
        violating = n_distinct[n_distinct > 1]
        if len(violating) > 0:
            warnings.warn(
                f"Column {col!r} varies within {len(violating)} units "
                f"(declared invariant); first offender: {violating.index[0]!r}",
                stacklevel=2,
            )


# ============================================================================
# Value-level verbs
# ============================================================================


def noise(
    data: pd.DataFrame,
    columns: str | Sequence[str],
    *,
    scale: float | str = "auto",
    method: str = "gaussian",
    share: float = 1.0,
    direction: str = "both",
    clip: tuple[float, float] | None = None,
    by: str | None = None,
    unit_id: str | None = None,
    random_state: int | np.random.Generator | None = None,
) -> pd.DataFrame:
    """Add noise to numeric columns.

    Parameters
    ----------
    data : DataFrame
    columns : str | list of str
        Numeric column(s) to perturb.
    scale : float | 'auto', default 'auto'
        SD (gaussian/laplace), spread (uniform), max step (discrete), proportion
        (multiplicative), or integer group size (group_mean). With 'auto',
        scale is 0.05 x column_std per column (or 3 for discrete, 0.05 for
        multiplicative, 3 for group_mean group-size).
    method : {'gaussian', 'laplace', 'uniform', 'discrete', 'multiplicative', 'group_mean'}
    share : float in [0, 1], default 1.0
        Fraction of units (or rows when unit_id is None) to perturb.
    direction : {'both', 'up', 'down'}, default 'both'
        Asymmetric noise; clipped to non-negative or non-positive when not 'both'.
    clip : (lo, hi) | None
        Post-noise clipping.
    by : str | None
        Grouping for method='group_mean' (sort within group before grouping
        into k-tuples).
    unit_id : str | None
        When set, noise is drawn once per unit and broadcast.
    random_state : int | Generator | None

    Returns
    -------
    DataFrame
        Copy of `data` with perturbed columns.
    """
    rng = _resolve_random_state(random_state)
    columns = _validate_columns(data, columns)
    out = data.copy()

    if method == "group_mean":
        k = 3 if scale == "auto" else int(scale)
        for col in columns:
            out[col] = _noise_group_mean(out, col, k, by=by)
        return out

    select_mask = _select_share(data, share, unit_id, rng)
    n_total = len(data)

    # When share=0 nothing is selected; return the copy untouched so dtypes
    # are preserved (important for integer columns).
    if not select_mask.any():
        return out

    for col in columns:
        col_scale = _resolve_noise_scale(out[col], scale, method)

        if unit_id is not None:
            unit_noise = _apply_per_unit(
                data, unit_id, lambda _u, _s=col_scale: _draw_noise(rng, method, _s, 1)[0]
            )
            noise_arr = unit_noise.values
        else:
            noise_arr = _draw_noise(rng, method, col_scale, n_total)

        if direction == "up":
            noise_arr = np.abs(noise_arr)
        elif direction == "down":
            noise_arr = -np.abs(noise_arr)

        noise_arr = np.where(select_mask.values, noise_arr, 0)

        if method == "multiplicative":
            new = out[col].values * (1 + noise_arr)
        else:
            new = out[col].values + noise_arr

        if clip is not None:
            new = np.clip(new, clip[0], clip[1])

        out[col] = new

    return out


def _resolve_noise_scale(series: pd.Series, scale, method: str) -> float:
    """Compute the effective scale, handling 'auto'."""
    if scale != "auto":
        return float(scale)
    if method == "multiplicative":
        return 0.05
    if method == "discrete":
        return 3.0
    sd = float(series.std())
    if sd == 0 or np.isnan(sd):
        return 1.0
    return 0.05 * sd


def _draw_noise(rng: np.random.Generator, method: str, scale: float, n: int) -> np.ndarray:
    """Draw an array of noise samples by method."""
    if method == "gaussian":
        return rng.normal(0, scale, size=n)
    if method == "laplace":
        return rng.laplace(0, scale, size=n)
    if method == "uniform":
        return rng.uniform(-scale, scale, size=n)
    if method == "discrete":
        s = int(scale)
        return rng.integers(-s, s + 1, size=n).astype(float)
    if method == "multiplicative":
        return rng.normal(0, scale, size=n)
    raise ValueError(f"Unknown noise method: {method!r}")


def _noise_group_mean(data: pd.DataFrame, col: str, k: int, by: str | None) -> pd.Series:
    """Microaggregation: sort within `by` (or globally), group into k-tuples,
    replace each value with group mean. Returns Series aligned to data.index.
    """
    if k < 2:
        return data[col]

    def _agg(s: pd.Series) -> pd.Series:
        sorted_idx = s.sort_values().index
        result = s.copy()
        for start in range(0, len(sorted_idx), k):
            group = sorted_idx[start:start + k]
            result.loc[group] = s.loc[group].mean()
        return result

    if by is None:
        return _agg(data[col])
    return data.groupby(by, group_keys=False)[col].apply(_agg)


def jitter(
    data: pd.DataFrame,
    columns: str | Sequence[str],
    *,
    scale: float | str | pd.Timedelta = "auto",
    distribution: str = "uniform",
    unit_id: str | None = None,
    share: float = 1.0,
    random_state: int | np.random.Generator | None = None,
) -> pd.DataFrame:
    """Small symmetric noise — for numeric or date columns.

    Use for plot-safe perturbation; use `noise` when distribution and scale
    matter for downstream statistics.

    Default scale='auto' computes 0.01 x column_range for numeric columns
    and '1 day' for date columns.
    """
    rng = _resolve_random_state(random_state)
    columns = _validate_columns(data, columns)
    out = data.copy()
    select_mask = _select_share(data, share, unit_id, rng)
    n_total = len(data)

    if not select_mask.any():
        return out

    for col in columns:
        is_date = pd.api.types.is_datetime64_any_dtype(out[col])
        col_scale = _resolve_jitter_scale(out[col], scale, is_date)

        if unit_id is not None:
            draws = _apply_per_unit(
                data, unit_id,
                lambda _u, _s=col_scale, _d=is_date: _draw_jitter_scalar(rng, distribution, _s, _d),
            )
            noise_arr = draws.values
        else:
            noise_arr = _draw_jitter_array(rng, distribution, col_scale, n_total, is_date)

        if is_date:
            applied = np.where(select_mask.values, noise_arr, pd.Timedelta(0))
            out[col] = out[col] + pd.to_timedelta(applied)
        else:
            applied = np.where(select_mask.values, noise_arr, 0.0)
            out[col] = out[col].values + applied

    return out


def _resolve_jitter_scale(series: pd.Series, scale, is_date: bool):
    """Compute the effective scale for jitter, handling 'auto'."""
    if scale == "auto":
        if is_date:
            return pd.Timedelta("1 day")
        rng_ = float(series.max() - series.min())
        if rng_ == 0 or np.isnan(rng_):
            return 1.0
        return 0.01 * rng_
    return pd.Timedelta(scale) if is_date else float(scale)


def _draw_jitter_scalar(rng, distribution, scale, is_date):
    """Draw a single jitter sample (numeric or Timedelta)."""
    if is_date:
        rng_value = rng.uniform(-1, 1) if distribution == "uniform" else rng.normal(0, 1)
        return rng_value * scale
    if distribution == "uniform":
        return rng.uniform(-scale, scale)
    return rng.normal(0, scale)


def _draw_jitter_array(rng, distribution, scale, n, is_date):
    """Draw an array of jitter samples."""
    if is_date:
        u = rng.uniform(-1, 1, size=n) if distribution == "uniform" else rng.normal(0, 1, size=n)
        return np.array([x * scale for x in u])
    if distribution == "uniform":
        return rng.uniform(-scale, scale, size=n)
    return rng.normal(0, scale, size=n)


def winsorize(
    data: pd.DataFrame,
    columns: str | Sequence[str],
    *,
    limits: tuple[float | None, float | None] = (0.01, 0.99),
    method: str = "percentile",
    by: str | None = None,
    unit_id: str | None = None,
    share: float = 1.0,
) -> pd.DataFrame:
    """Cap extremes.

    Methods
    -------
    percentile : limits are quantiles, e.g. (0.01, 0.99)
    value      : limits are exact bounds, e.g. (None, 90) for top-code at 90
    gaussian   : limits are SD multipliers; cap at mean ± k·SD
    iqr        : limits are IQR multipliers; cap at Q1 - k·IQR and Q3 + k·IQR
    mad        : limits are MAD multipliers; cap at median ± k·MAD
    """
    columns = _validate_columns(data, columns)
    out = data.copy()
    lo_arg, hi_arg = limits

    def _bounds(s: pd.Series) -> tuple[float | None, float | None]:
        if method == "percentile":
            lo = s.quantile(lo_arg) if lo_arg is not None else None
            hi = s.quantile(hi_arg) if hi_arg is not None else None
            return lo, hi
        if method == "value":
            return lo_arg, hi_arg
        if method == "gaussian":
            m, sd = s.mean(), s.std()
            return (m - lo_arg * sd if lo_arg is not None else None,
                    m + hi_arg * sd if hi_arg is not None else None)
        if method == "iqr":
            q1, q3 = s.quantile([0.25, 0.75])
            iqr = q3 - q1
            return (q1 - lo_arg * iqr if lo_arg is not None else None,
                    q3 + hi_arg * iqr if hi_arg is not None else None)
        if method == "mad":
            med = s.median()
            mad = (s - med).abs().median()
            return (med - lo_arg * mad if lo_arg is not None else None,
                    med + hi_arg * mad if hi_arg is not None else None)
        raise ValueError(f"Unknown winsorize method: {method!r}")

    for col in columns:
        if by is None:
            lo, hi = _bounds(out[col])
            out[col] = out[col].clip(lower=lo, upper=hi)
        else:
            def _grp(s):
                lo, hi = _bounds(s)
                return s.clip(lower=lo, upper=hi)
            out[col] = out.groupby(by, group_keys=False)[col].apply(_grp)

    return out


def bin(
    data: pd.DataFrame,
    columns: str | Sequence[str],
    *,
    bins: int | Sequence[float] = 10,
    method: str = "quantile",
    labels: str | Sequence[str] = "range",
    min_count: int | None = None,
    unit_id: str | None = None,
    share: float = 1.0,
) -> pd.DataFrame:
    """Numeric → discrete intervals.

    Methods
    -------
    quantile     : equal-frequency bins (n bins)
    equal_width  : equal-width bins (n bins between min and max)
    manual       : `bins` is interpreted as explicit edge list

    Labels
    ------
    range    : "10-20" string
    midpoint : numeric midpoint of each interval
    index    : integer index (0, 1, 2, ...)
    list[str]: custom list of labels (length = #bins)

    min_count
    ---------
    If set, sparse bins (count < min_count) are merged into the smaller of
    their adjacent neighbors until all bins meet the threshold.
    """
    columns = _validate_columns(data, columns)
    out = data.copy()

    for col in columns:
        s = out[col]
        if method == "quantile":
            edges = np.unique(s.quantile(np.linspace(0, 1, bins + 1)).values)
        elif method == "equal_width":
            edges = np.linspace(s.min(), s.max(), bins + 1)
        elif method == "manual":
            edges = np.asarray(bins, dtype=float)
        else:
            raise ValueError(f"Unknown bin method: {method!r}")

        cat = pd.cut(s, edges, include_lowest=True, duplicates="drop")

        if min_count is not None:
            cat = _merge_sparse_bins(cat, min_count)

        if labels == "range":
            out[col] = cat.astype(str)
        elif labels == "midpoint":
            mids = {iv: (iv.left + iv.right) / 2 for iv in cat.cat.categories}
            out[col] = cat.map(mids).astype(float)
        elif labels == "index":
            out[col] = cat.cat.codes
        else:
            mapping = dict(zip(cat.cat.categories, labels))
            out[col] = cat.map(mapping)

    return out


def _merge_sparse_bins(cat, min_count: int):
    """Merge bins below min_count into adjacent bins until all bins meet
    the threshold. Greedy: merge each sparse bin into its smaller neighbor first.
    Returns a Series with merged categories.
    """
    s = pd.Series(cat).copy()
    counts = s.value_counts()
    cats = sorted(counts.index, key=lambda iv: iv.left)
    while True:
        sparse = [c for c in cats if counts.get(c, 0) < min_count]
        if not sparse:
            break
        target = sparse[0]
        i = cats.index(target)
        left = cats[i - 1] if i > 0 else None
        right = cats[i + 1] if i < len(cats) - 1 else None
        if left is None and right is None:
            break  # only one bin left
        if left is None:
            neighbor = right
        elif right is None:
            neighbor = left
        else:
            neighbor = left if counts.get(left, 0) <= counts.get(right, 0) else right
        new_iv = pd.Interval(min(target.left, neighbor.left),
                             max(target.right, neighbor.right),
                             closed=target.closed)
        s = s.map(lambda x, t=target, n=neighbor, nv=new_iv: nv if x in (t, n) else x)
        cats = sorted(set(s.dropna().unique()), key=lambda iv: iv.left)
        counts = s.value_counts()
    return pd.Categorical(s, categories=cats, ordered=True)


def coarsen(
    data: pd.DataFrame,
    columns: str | Sequence[str],
    *,
    to,
    mode: str = "nearest",
    unit_id: str | None = None,
    share: float = 1.0,
    random_state: int | np.random.Generator | None = None,
) -> pd.DataFrame:
    """Snap values to a coarser resolution.

    Numeric columns snap to a multiple of `to`; date columns snap to a
    period boundary. String/object columns raise — use `shorten` instead.

    Parameters
    ----------
    data : DataFrame
    columns : str | list of str
        Numeric or datetime column(s) to coarsen.
    to : number | str | pd.Timedelta
        Resolution to snap to. For numeric columns, a positive number — values
        are snapped to multiples of this. For date columns, one of:
          - period name (case-insensitive, plural OK): ``'day'``, ``'hour'``,
            ``'minute'``, ``'week'``, ``'month'``, ``'quarter'``, ``'year'``
          - multi-period string: ``'5 years'``, ``'10 days'``, ``'3 months'``
          - pandas offset alias: ``'W'``, ``'D'``, ``'5Y'``, ``'10min'``
          - a ``pd.Timedelta``
    mode : {'nearest', 'floor', 'ceil'}, default 'nearest'
        Direction of snapping.
    unit_id, share, random_state
        Accepted for signature consistency but inert — `coarsen` is
        deterministic per-value, so unit-aware draws and partial selection
        do not apply.

    Returns
    -------
    DataFrame
        Copy of `data` with the specified columns coarsened. Numeric inputs
        return float columns; date inputs return datetime columns.

    Notes
    -----
    For coarsening string/categorical codes (ICD chapters, ZIP prefixes), use
    `shorten`. For binning numeric values into labeled categories, use `bin`.
    """
    if mode not in ("nearest", "floor", "ceil"):
        raise ValueError(
            f"mode must be 'nearest', 'floor', or 'ceil'; got {mode!r}"
        )
    columns = _validate_columns(data, columns)
    out = data.copy()
    for col in columns:
        s = out[col]
        if pd.api.types.is_datetime64_any_dtype(s):
            out[col] = _coarsen_date(s, to, mode)
        elif pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s):
            out[col] = _coarsen_numeric(s, to, mode)
        else:
            raise TypeError(
                f"Column {col!r} has string dtype; coarsen handles only "
                f"numeric and datetime. For string/code coarsening, use shorten."
            )
    return out


# Mapping period name -> (pandas offset alias, is_calendar_period)
# A "calendar period" is variable-length (month, quarter, year) — these need
# the period-conversion path rather than `dt.floor`. Week is also handled via
# the period path because pandas treats it as a non-fixed frequency for floor.
_PERIOD_NAMES = {
    "day": ("D", False),
    "hour": ("h", False),
    "minute": ("min", False),
    "week": ("W", True),
    "month": ("M", True),
    "quarter": ("Q", True),
    "year": ("Y", True),
}


def _parse_date_resolution(to):
    """Normalize `to` into a structured form for date coarsening.

    Returns a tuple `(kind, payload)` where `kind` is one of:
      - 'timedelta'      : payload is a pd.Timedelta — use dt.floor/round/ceil
      - 'period'         : payload is (base_name, n) — variable-length calendar
                           period (year/month/quarter/week), n=multiplier
      - 'offset'         : payload is a pandas offset alias string usable
                           directly with dt.floor (e.g. 'D', '5min')
    """
    if isinstance(to, pd.Timedelta):
        return ("timedelta", to)
    if not isinstance(to, str):
        raise ValueError(
            f"For date columns, `to` must be a string or Timedelta; got {to!r}"
        )

    text = to.strip()
    # multi-period like "5 years", "10 days", "3 months"
    parts = text.split()
    if len(parts) == 2 and parts[0].isdigit():
        n = int(parts[0])
        unit = parts[1].lower().rstrip("s")
        if unit in _PERIOD_NAMES:
            base, is_period = _PERIOD_NAMES[unit]
            if is_period:
                return ("period", (base, n))
            return ("timedelta", pd.Timedelta(f"{n}{base}"))

    # single name
    name = text.lower().rstrip("s")
    if name in _PERIOD_NAMES:
        base, is_period = _PERIOD_NAMES[name]
        if is_period:
            return ("period", (base, 1))
        return ("offset", base)

    # raw pandas alias — attempt to detect calendar-period suffixes
    upper = text.upper()
    # Y, A (year), Q (quarter), M alone (month), W (week) are calendar-period
    # but 'min' ends with 'N' so it's safe; we need to be careful not to
    # match 'ME', 'YE' (the modern aliases) either.
    if upper.endswith(("Y", "A", "Q")) or upper == "M" or upper == "W":
        # extract leading digits if present (e.g. "5Y", "3M", "2Q")
        digits = ""
        i = 0
        while i < len(text) and text[i].isdigit():
            digits += text[i]
            i += 1
        suffix = text[i:].upper()
        n = int(digits) if digits else 1
        if suffix in ("Y", "A", "YE"):
            return ("period", ("Y", n))
        if suffix == "Q":
            return ("period", ("Q", n))
        if suffix in ("M", "ME"):
            return ("period", ("M", n))
        if suffix == "W":
            return ("period", ("W", n))
    # fall back to using it as a pandas offset alias directly
    return ("offset", text)


def _coarsen_numeric(s, to, mode):
    if isinstance(to, bool) or not isinstance(to, (int, float)) or to <= 0:
        raise ValueError(
            f"For numeric columns, `to` must be a positive number; got {to!r}"
        )
    if mode == "nearest":
        return (s / to).round() * to
    if mode == "floor":
        return np.floor(s / to) * to
    return np.ceil(s / to) * to


def _coarsen_date(s, to, mode):
    s = pd.to_datetime(s)
    kind, payload = _parse_date_resolution(to)
    if kind == "timedelta":
        freq = payload
        if mode == "nearest":
            return s.dt.round(freq)
        if mode == "floor":
            return s.dt.floor(freq)
        return s.dt.ceil(freq)
    if kind == "offset":
        if mode == "nearest":
            return s.dt.round(payload)
        if mode == "floor":
            return s.dt.floor(payload)
        return s.dt.ceil(payload)
    # kind == "period": variable-length calendar period(s)
    base, n = payload
    return _coarsen_date_period(s, base, n, mode)


def _coarsen_date_period(s, base, n, mode):
    """Snap to a calendar period boundary (year/quarter/month/week), possibly
    a multiple of the base period.

    For multi-year/multi-month/multi-quarter, we use arithmetic on year/month
    rather than pandas' anchored multi-period (which doesn't align to round
    multiples like year % 5 == 0).
    """
    if base == "Y" and n > 1:
        years = s.dt.year
        floor_year = (years // n) * n
        floor_dt = pd.to_datetime({"year": floor_year, "month": 1, "day": 1})
        floor_dt.index = s.index
        ceil_year = floor_year + n
        ceil_dt = pd.to_datetime({"year": ceil_year, "month": 1, "day": 1})
        ceil_dt.index = s.index
    elif base == "M" and n > 1:
        years = s.dt.year
        months = s.dt.month  # 1..12
        # zero-based month index across years
        total = (years * 12 + (months - 1))
        floor_total = (total // n) * n
        floor_year = floor_total // 12
        floor_month = (floor_total % 12) + 1
        floor_dt = pd.to_datetime({"year": floor_year, "month": floor_month, "day": 1})
        floor_dt.index = s.index
        ceil_total = floor_total + n
        ceil_year = ceil_total // 12
        ceil_month = (ceil_total % 12) + 1
        ceil_dt = pd.to_datetime({"year": ceil_year, "month": ceil_month, "day": 1})
        ceil_dt.index = s.index
    elif base == "Q" and n > 1:
        # treat as multi-month with n*3 months
        return _coarsen_date_period(s, "M", n * 3, mode)
    elif base == "W" and n > 1:
        # treat as multi-day with n*7 days (fixed-frequency)
        freq = pd.Timedelta(days=7 * n)
        if mode == "nearest":
            return s.dt.round(freq)
        if mode == "floor":
            return s.dt.floor(freq)
        return s.dt.ceil(freq)
    else:
        # single calendar period — use pandas period conversion
        freq = base
        if base == "Y":
            # newer pandas wants 'Y' or 'YE'; to_period accepts 'Y'
            freq = "Y"
        period = s.dt.to_period(freq)
        floor_dt = period.dt.start_time
        ceil_dt = (period + 1).dt.start_time

    if mode == "floor":
        return floor_dt
    if mode == "ceil":
        # values already on a boundary should stay
        return ceil_dt.where(s != floor_dt, floor_dt)
    # nearest: pick whichever boundary is closer
    to_floor = (s - floor_dt).abs()
    to_ceil = (ceil_dt - s).abs()
    return floor_dt.where(to_floor <= to_ceil, ceil_dt)


# ============================================================================
# Date verbs
# ============================================================================


def year(
    data: pd.DataFrame,
    columns: str | Sequence[str],
    *,
    bin: int | None = None,
    as_date: bool = False,
    unit_id: str | None = None,
    share: float = 1.0,
) -> pd.DataFrame:
    """Truncate dates to year resolution.

    Default returns integer year. `as_date=True` returns a date floored to
    January 1 of that year. `bin=N` produces N-year period labels like
    "1990-1994".
    """
    columns = _validate_columns(data, columns)
    out = data.copy()
    for col in columns:
        y = pd.to_datetime(out[col]).dt.year
        if bin is None:
            if as_date:
                out[col] = pd.to_datetime(y.astype(str) + "-01-01")
            else:
                out[col] = y.astype(int)
        else:
            floor = (y // bin) * bin
            ceil = floor + bin - 1
            if as_date:
                out[col] = pd.to_datetime(floor.astype(str) + "-01-01")
            else:
                out[col] = floor.astype(str) + "-" + ceil.astype(str)
    return out


def month(
    data: pd.DataFrame,
    columns: str | Sequence[str],
    *,
    bin: int | None = None,
    as_date: bool = False,
    unit_id: str | None = None,
    share: float = 1.0,
) -> pd.DataFrame:
    """Truncate dates to month resolution. `bin=3` groups into quarters."""
    columns = _validate_columns(data, columns)
    out = data.copy()
    for col in columns:
        dt = pd.to_datetime(out[col])
        y = dt.dt.year
        m = dt.dt.month
        if bin is not None:
            m = ((m - 1) // bin) * bin + 1
        if as_date:
            out[col] = pd.to_datetime(
                y.astype(str) + "-" + m.astype(str).str.zfill(2) + "-01"
            )
        else:
            out[col] = y.astype(str) + "-" + m.astype(str).str.zfill(2)
    return out


def diff(
    data: pd.DataFrame,
    columns: str | Sequence[str],
    *,
    ref="first_per_unit",
    unit: str = "days",
    keep_order: bool = True,
    unit_id: str | None = None,
    share: float = 1.0,
    random_state: int | np.random.Generator | None = None,
) -> pd.DataFrame:
    """Convert dates to numeric diff from a reference.

    ref options
    -----------
    'first_per_unit' (default) : earliest date per unit (requires unit_id)
    'min'                      : minimum date in the column
    'random_per_unit'          : random anchor per unit (requires unit_id)
    column name (str)          : pairwise anchor from another date column
    pd.Timestamp or date string: fixed scalar anchor

    unit : 'days' | 'months' | 'years'

    keep_order=True raises if the result would reorder events within a unit
    (critical for survival-analysis correctness).
    """
    columns = _validate_columns(data, columns)
    out = data.copy()

    if ref in ("first_per_unit", "random_per_unit") and unit_id is None:
        raise ValueError(f"ref={ref!r} requires unit_id to be set")

    rng = _resolve_random_state(random_state)

    for col in columns:
        dt = pd.to_datetime(out[col])
        if ref == "first_per_unit":
            anchor = data.groupby(unit_id)[col].transform("min")
        elif ref == "min":
            anchor = pd.Timestamp(dt.min())
        elif ref == "random_per_unit":
            units = data[unit_id].unique()
            min_date = dt.min()
            max_date = dt.max()
            span_days = max((max_date - min_date).days, 1)
            unit_anchors = {
                u: min_date + pd.Timedelta(days=int(rng.integers(0, span_days + 1)))
                for u in units
            }
            anchor = data[unit_id].map(unit_anchors)
        elif isinstance(ref, str) and ref in data.columns:
            anchor = pd.to_datetime(data[ref])
        elif isinstance(ref, (pd.Timestamp,)):
            anchor = ref
        elif isinstance(ref, str):
            anchor = pd.Timestamp(ref)
        else:
            raise ValueError(f"Unsupported ref: {ref!r}")

        delta = (dt - anchor)
        if isinstance(delta, pd.Series):
            days = delta.dt.days
        else:
            days = pd.Series([delta.days] * len(out), index=out.index)

        if unit == "days":
            result = days.astype(int)
        elif unit == "months":
            result = (days / 30.44).astype(int)
        elif unit == "years":
            result = (days / 365.25).astype(int)
        else:
            raise ValueError(f"Unknown unit: {unit!r}")

        if keep_order and unit_id is not None:
            for pid, grp in data.groupby(unit_id):
                orig_order = dt.loc[grp.index].rank(method="first")
                new_order = result.loc[grp.index].rank(method="first")
                if not (orig_order.values == new_order.values).all():
                    raise ValueError(
                        f"diff would reorder events within unit {pid!r}; "
                        f"keep_order=True"
                    )

        out[col] = result

    return out


# ============================================================================
# Code & category verbs
# ============================================================================


def shorten(
    data: pd.DataFrame,
    columns: str | Sequence[str],
    *,
    keep: int = 3,
    sep: str | None = None,
    side: str = "left",
    min_count: int | None = None,
    fallback: str = "*",
    per_value: dict[str, str] | None = None,
    unit_id: str | None = None,
    share: float = 1.0,
) -> pd.DataFrame:
    """Truncate codes (ICD, ZIP, NACE).

    keep      : number of characters to keep
    sep       : if set, truncate at first occurrence of this character
    side      : 'left' = keep prefix; 'right' = keep suffix
    min_count : cascade — if a truncated value appears < min_count times,
                truncate further until it meets the threshold, or replace
                with `fallback` if no further truncation is possible
    per_value : dict mapping a value or "PREFIX*" pattern to an action:
                'keep_full' or 'keep_N' (e.g., 'keep_1' = keep 1 character)
    """
    columns = _validate_columns(data, columns)
    out = data.copy()

    def _truncate(value, keep_n):
        if pd.isna(value):
            return value
        s = str(value)
        if sep is not None and sep in s:
            return s.split(sep)[0] if side == "left" else s.split(sep)[-1]
        return s[:keep_n] if side == "left" else s[-keep_n:]

    for col in columns:
        s = out[col].astype(str)

        if per_value:
            def _apply_rule(v):
                for pattern, action in per_value.items():
                    matches = (v == pattern or
                               (pattern.endswith("*") and v.startswith(pattern[:-1])))
                    if matches:
                        if action == "keep_full":
                            return v
                        if action.startswith("keep_"):
                            n = int(action.split("_")[1])
                            return _truncate(v, n)
                return _truncate(v, keep)
            s = s.map(_apply_rule)
        else:
            s = s.map(lambda v: _truncate(v, keep))

        if min_count is not None:
            current_keep = keep
            while current_keep >= 1:
                counts = s.value_counts()
                rare = counts[counts < min_count].index
                if len(rare) == 0:
                    break
                current_keep -= 1
                if current_keep < 1:
                    s = s.where(~s.isin(rare), fallback)
                    break
                s = s.map(lambda v, _r=rare, _k=current_keep:
                          _truncate(v, _k) if v in _r else v)

        out[col] = s

    return out


def collapse(
    data: pd.DataFrame,
    columns: str | Sequence[str],
    *,
    mapping: dict | None = None,
    rare_below: int | None = None,
    keep_top: int | None = None,
    keep_prop: float | None = None,
    other_label: str = "Other",
    by: str | None = None,
    unit_id: str | None = None,
    random_state: int | np.random.Generator | None = None,
) -> pd.DataFrame:
    """Merge categorical levels. Exactly one mode per call.

    Modes
    -----
    mapping={old: new, ...}      : explicit hierarchy
    rare_below=N                 : values appearing < N times → other_label
    keep_top=N                   : keep N most common; rest → other_label
    keep_prop=p                  : keep values with proportion ≥ p; rest → other_label
    """
    columns = _validate_columns(data, columns)
    modes = [mapping is not None, rare_below is not None,
             keep_top is not None, keep_prop is not None]
    if sum(modes) != 1:
        raise ValueError(
            "collapse requires exactly one mode: "
            "mapping, rare_below, keep_top, or keep_prop"
        )

    out = data.copy()

    for col in columns:
        s = out[col]

        if mapping is not None:
            out[col] = s.map(lambda v: mapping.get(v, v))
            continue

        def _apply_threshold(series: pd.Series) -> pd.Series:
            if rare_below is not None:
                counts = series.value_counts()
                keep_set = set(counts[counts >= rare_below].index)
            elif keep_top is not None:
                counts = series.value_counts()
                keep_set = set(counts.head(keep_top).index)
            elif keep_prop is not None:
                props = series.value_counts(normalize=True)
                keep_set = set(props[props >= keep_prop].index)
            else:
                return series
            return series.where(series.isin(keep_set), other_label)

        if by is not None:
            out[col] = out.groupby(by, group_keys=False)[col].apply(_apply_threshold)
        else:
            out[col] = _apply_threshold(s)

    return out


# ============================================================================
# ID verb
# ============================================================================


def pseudonymize(
    data: pd.DataFrame,
    columns: str | Sequence[str],
    *,
    method: str = "random",
    salt: str | None = None,
    return_key: bool = True,
    key_path: str | None = None,
    prefix: str = "P",
    random_state: int | np.random.Generator | None = None,
):
    """Replace IDs (random or deterministic hash).

    method='random' : random new IDs, key dict returned (different per run unless seeded)
    method='hash'   : deterministic hash with `salt` (stable across runs that share salt)

    Returns (df, key) when return_key=True (default), otherwise just df.

    key_path persists the key to a JSON file (warning logged about co-location).
    """
    columns = _validate_columns(data, columns)
    out = data.copy()
    keys: dict[str, dict] = {}

    if method == "random":
        rng = _resolve_random_state(random_state)
        for col in columns:
            uniques = list(out[col].unique())
            order = rng.permutation(len(uniques))
            mapping = {
                uniques[i]: f"{prefix}{order[i] + 1:06d}"
                for i in range(len(uniques))
            }
            out[col] = out[col].map(mapping)
            keys[col] = mapping
    elif method == "hash":
        if salt is None:
            warnings.warn(
                "pseudonymize(method='hash') without salt is weak; "
                "provide a salt for production use",
                stacklevel=2,
            )
        salt_bytes = (salt or "").encode("utf-8")
        for col in columns:
            def _h(v, _salt=salt_bytes):
                if pd.isna(v):
                    return v
                h = hashlib.blake2b(str(v).encode("utf-8") + _salt, digest_size=8)
                return prefix + h.hexdigest()
            out[col] = out[col].map(_h)
            keys[col] = {"method": "hash", "salt_provided": salt is not None}
    else:
        raise ValueError(f"Unknown pseudonymize method: {method!r}")

    if key_path is not None:
        warnings.warn(
            f"Persisting pseudonymization key to {key_path}; "
            "store it separately from the data",
            stacklevel=2,
        )
        with open(key_path, "w") as f:
            json.dump(keys, f, indent=2, default=str)

    if return_key:
        return out, keys
    return out


# ============================================================================
# Record-level verbs
# ============================================================================


def insert(
    data: pd.DataFrame,
    *,
    n: int | None = None,
    share: float = 0.01,
    level: str = "row",
    source: str = "resample",
    modify: dict | None = None,
    new_unit_ids: bool = True,
    unit_id: str | None = None,
    random_state: int | np.random.Generator | None = None,
) -> pd.DataFrame:
    """Inject decoy rows or units.

    level='row'  : add N decoy rows
    level='unit' : add N decoy units, each with realistic row-count history
                   (drawn from real units' row-count distribution)

    source='resample'           : draw real rows then optionally modify
    source='sample_per_column'  : draw each column independently (breaks correlations)
    """
    if level not in ("row", "unit"):
        raise ValueError(f"level must be 'row' or 'unit', got {level!r}")
    if level == "unit" and unit_id is None:
        raise ValueError("level='unit' requires unit_id to be set")

    rng = _resolve_random_state(random_state)

    if share > 0.05:
        warnings.warn(f"insert share={share} > 0.05 may distort statistics", stacklevel=2)

    if level == "row":
        n_decoys = n if n is not None else int(round(len(data) * share))
        sample = _generate_decoys(data, n_decoys, source, rng, modify)
        if new_unit_ids and unit_id is not None and unit_id in sample.columns:
            sample[unit_id] = [f"DECOY{i:06d}" for i in range(n_decoys)]
        return pd.concat([data, sample], ignore_index=True)

    # level == "unit"
    n_units = data[unit_id].nunique()
    n_decoy_units = n if n is not None else int(round(n_units * share))
    row_counts = data.groupby(unit_id).size().values
    decoys = []
    for i in range(n_decoy_units):
        rc = int(rng.choice(row_counts))
        sample = _generate_decoys(data, rc, source, rng, modify)
        new_id = f"DECOY{i:06d}"
        sample[unit_id] = new_id
        decoys.append(sample)
    if decoys:
        return pd.concat([data] + decoys, ignore_index=True)
    return data.copy()


def _generate_decoys(
    data: pd.DataFrame,
    n: int,
    source: str,
    rng: np.random.Generator,
    modify: dict | None,
) -> pd.DataFrame:
    """Generate n decoy rows."""
    if source == "resample":
        idx = rng.choice(len(data), size=n, replace=True)
        sample = data.iloc[idx].reset_index(drop=True)
    elif source == "sample_per_column":
        sample = pd.DataFrame({
            c: data[c].sample(n=n, replace=True, random_state=int(rng.integers(0, 2**31))).values
            for c in data.columns
        })
    else:
        raise ValueError(f"Unknown source: {source!r}")

    if modify:
        for col, (op, mag) in modify.items():
            if col not in sample.columns:
                continue
            if op == "noise":
                sample[col] = sample[col] + rng.normal(0, mag, size=n)
            elif op == "shift" and pd.api.types.is_datetime64_any_dtype(sample[col]):
                offsets = rng.integers(-mag, mag + 1, size=n)
                sample[col] = sample[col] + pd.to_timedelta(offsets, unit="D")

    return sample


def eliminate(
    data: pd.DataFrame,
    *,
    where: pd.Series | None = None,
    rare_below: int | None = None,
    share: float | None = None,
    level: str = "row",
    columns: Sequence[str] | None = None,
    replace_with=None,
    unit_id: str | None = None,
    random_state: int | np.random.Generator | None = None,
) -> pd.DataFrame:
    """Drop rows/units or mask cells.

    Modes (exactly one of where, rare_below, share, OR columns-only):
    - where=<bool Series>   : drop matching rows (or units with level='unit')
    - rare_below=N          : mask cells whose value count < N in given columns
    - share=p               : drop random p% of rows (or units with level='unit')
    - columns=[...] only    : mask all cells in those columns to NaN (or replace_with)

    Raises if no mode is given (no silent no-op).
    """
    modes = [where is not None, rare_below is not None, share is not None]
    only_columns = (sum(modes) == 0 and columns is not None)
    if sum(modes) == 0 and not only_columns:
        raise ValueError(
            "eliminate requires a mode arg: where, rare_below, share, or columns"
        )
    if sum(modes) > 1:
        raise ValueError("eliminate accepts exactly one of where, rare_below, or share")
    if level == "unit" and unit_id is None:
        raise ValueError("level='unit' requires unit_id to be set")

    rng = _resolve_random_state(random_state)
    out = data.copy()

    if where is not None:
        if level == "unit":
            units_to_drop = data.loc[where, unit_id].unique()
            return out[~out[unit_id].isin(units_to_drop)].reset_index(drop=True)
        return out[~where].reset_index(drop=True)

    if share is not None:
        if share > 0.05:
            warnings.warn(f"eliminate share={share} > 0.05 may distort statistics", stacklevel=2)
        mask = _select_share(data, share, unit_id if level == "unit" else None, rng)
        return out[~mask].reset_index(drop=True)

    if rare_below is not None:
        cols = _validate_columns(out, columns) if columns else list(out.columns)
        for col in cols:
            counts = out[col].value_counts()
            rare = counts[counts < rare_below].index
            if level == "unit":
                units_with_rare = data.loc[data[col].isin(rare), unit_id].unique()
                mask_rows = out[unit_id].isin(units_with_rare)
                out.loc[mask_rows, col] = replace_with if replace_with is not None else np.nan
            else:
                out.loc[out[col].isin(rare), col] = replace_with if replace_with is not None else np.nan
        return out

    # only_columns mode
    if columns:
        cols = _validate_columns(out, columns)
        for col in cols:
            out[col] = replace_with if replace_with is not None else np.nan
        return out

    return out


def swap(
    data: pd.DataFrame,
    columns: str | Sequence[str],
    *,
    method: str = "rank",
    level: str = "row",
    by: str | None = None,
    share: float = 0.05,
    swap_range_pct: float = 0.05,
    transition: dict | None = None,
    unit_id: str | None = None,
    random_state: int | np.random.Generator | None = None,
) -> pd.DataFrame:
    """Exchange values between rows or whole records between units.

    method describes HOW to match: rank | random | shuffle | pram
    level  describes WHAT is swapped: row | unit
    """
    columns = _validate_columns(data, columns)
    if level == "unit" and unit_id is None:
        raise ValueError("level='unit' requires unit_id to be set")

    rng = _resolve_random_state(random_state)
    out = data.copy()

    if method == "shuffle":
        for col in columns:
            if by is None:
                perm = rng.permutation(len(out))
                out[col] = out[col].values[perm]
            else:
                def _shuf(s, _rng=rng):
                    return pd.Series(_rng.permutation(s.values), index=s.index)
                out[col] = out.groupby(by, group_keys=False)[col].apply(_shuf)
        return out

    if method == "pram":
        if transition is None:
            raise ValueError("method='pram' requires transition matrix dict")
        for col in columns:
            out[col] = out[col].map(lambda v, _t=transition, _r=rng: _pram_recode(v, _t, _r))
        return out

    if level == "row":
        # rank or random row-pair swap
        for col in columns:
            n = len(out)
            n_swap = int(round(n * share))
            col_idx = out.columns.get_loc(col)
            if method == "rank":
                order = out[col].rank(method="first").values.argsort()
                pairs_done = 0
                attempts = 0
                while pairs_done < n_swap // 2 and attempts < n_swap * 10:
                    i = int(rng.integers(0, n))
                    window = max(1, int(n * swap_range_pct))
                    j_candidates = order[max(0, i - window):min(n, i + window + 1)]
                    j = int(rng.choice(j_candidates))
                    if j != i:
                        a = out.iloc[i, col_idx]
                        b = out.iloc[j, col_idx]
                        out.iloc[i, col_idx] = b
                        out.iloc[j, col_idx] = a
                        pairs_done += 1
                    attempts += 1
            elif method == "random":
                idx_to_swap = rng.choice(n, size=(n_swap // 2) * 2, replace=False)
                pairs = idx_to_swap.reshape(-1, 2)
                for i, j in pairs:
                    a = out.iloc[i, col_idx]
                    b = out.iloc[j, col_idx]
                    out.iloc[i, col_idx] = b
                    out.iloc[j, col_idx] = a
            else:
                raise ValueError(f"Unknown method: {method!r}")
        return out

    # level == "unit": swap whole records between matched units
    units = data[unit_id].unique()
    n_units = len(units)
    n_swap_units = int(round(n_units * share))

    if method == "random":
        chosen = rng.choice(units, size=(n_swap_units // 2) * 2, replace=False)
        pairs = chosen.reshape(-1, 2)
    elif method == "rank":
        # rank units by the first column's per-unit mean, swap within window
        first_col = columns[0]
        unit_vals = data.groupby(unit_id)[first_col].mean().sort_values()
        ordered = list(unit_vals.index)
        window = max(1, int(n_units * swap_range_pct))
        used: set = set()
        pairs_list: list = []
        for _ in range(n_swap_units // 2):
            available_positions = [k for k in range(n_units) if ordered[k] not in used]
            if not available_positions:
                break
            i_pos = int(rng.choice(available_positions))
            i = ordered[i_pos]
            j_candidates = [ordered[k]
                            for k in range(max(0, i_pos - window), min(n_units, i_pos + window + 1))
                            if ordered[k] != i and ordered[k] not in used]
            if not j_candidates:
                continue
            j = j_candidates[int(rng.integers(0, len(j_candidates)))]
            pairs_list.append((i, j))
            used.add(i)
            used.add(j)
        pairs = np.array(pairs_list) if pairs_list else np.empty((0, 2))
    else:
        raise ValueError(f"Unknown method for level='unit': {method!r}")

    for u1, u2 in pairs:
        mask1 = out[unit_id] == u1
        mask2 = out[unit_id] == u2
        for col in columns:
            v1 = out.loc[mask1, col].iloc[0] if mask1.any() else None
            v2 = out.loc[mask2, col].iloc[0] if mask2.any() else None
            out.loc[mask1, col] = v2
            out.loc[mask2, col] = v1

    return out


def _pram_recode(value, transition: dict, rng: np.random.Generator):
    """PRAM: probabilistic categorical recoding given a transition matrix."""
    if pd.isna(value):
        return value
    row = transition.get(value)
    if row is None:
        return value
    items = list(row.items())
    targets = [k for k, _ in items]
    probs = np.array([v for _, v in items], dtype=float)
    probs = probs / probs.sum()
    return rng.choice(targets, p=probs)


# ============================================================================
# Output verb
# ============================================================================


def suppress(target, **kwargs):
    """Polymorphic output protection. Dispatches on target type.

    For pandas Series/DataFrame: min_n, counts, dominance, p_percent, round,
        ranges, contributions, secondary
    For statsmodels result: redact_intercept, widen_alpha, group_counts
    For plot data ((x, y) tuple): hexbin, bin_histogram, jitter, gridsize,
        bins, min_count
    """
    if isinstance(target, (pd.Series, pd.DataFrame)):
        return _suppress_table(target, **kwargs)
    if hasattr(target, "params") and hasattr(target, "conf_int"):
        return _suppress_regression(target, **kwargs)
    if isinstance(target, tuple) and len(target) == 2:
        return _suppress_plot(target, **kwargs)
    raise NotImplementedError(
        f"suppress does not handle target of type {type(target).__name__}"
    )


def _suppress_table(
    target,
    *,
    min_n: int | None = None,
    counts=None,
    dominance: tuple[int, float] | None = None,
    p_percent: float | None = None,
    round: int | None = None,
    ranges: Sequence[tuple[int, int]] | None = None,
    contributions: dict | None = None,
    secondary: bool = False,
):
    out = target.copy()

    # primary suppression by frequency
    if min_n is not None:
        if counts is None:
            counts = out
        mask = counts < min_n
        out = out.where(~mask, other=np.nan)

    # dominance rule
    if dominance is not None and contributions is not None:
        n, k = dominance
        idx_iter = list(out.index)
        for idx in idx_iter:
            contribs = sorted(contributions.get(idx, []), reverse=True)
            total = sum(contribs) if contribs else 0
            top_n_sum = sum(contribs[:n])
            if total > 0 and top_n_sum / total > k:
                if isinstance(out, pd.Series):
                    out[idx] = np.nan
                else:
                    out.loc[idx] = np.nan

    # p%-rule
    if p_percent is not None and contributions is not None:
        idx_iter = list(out.index)
        for idx in idx_iter:
            contribs = sorted(contributions.get(idx, []), reverse=True)
            if len(contribs) < 3:
                continue
            x1, x2 = contribs[0], contribs[1]
            sum_rest = sum(contribs[2:])
            if sum_rest == 0 or x1 == 0:
                continue
            if sum_rest / x1 < p_percent:
                if isinstance(out, pd.Series):
                    out[idx] = np.nan
                else:
                    out.loc[idx] = np.nan

    # rounding
    if round is not None:
        out = (out / round).round() * round

    # fuzzy ranges
    if ranges is not None:
        def _range_label(v):
            if pd.isna(v):
                return v
            for lo, hi in ranges:
                if lo <= v <= hi:
                    return f"{lo}-{hi}"
            return f">{ranges[-1][1]}"
        if isinstance(out, pd.Series):
            out = out.map(_range_label)
        else:
            out = out.map(_range_label)

    if secondary:
        out = _secondary_suppression(out)

    return out


def _secondary_suppression(table):
    """Greedy secondary suppression. For DataFrames: if a row or column has
    exactly one NaN, suppress the smallest remaining value so marginals can't
    recover the suppressed value. For Series: no-op (no marginal structure).
    """
    if isinstance(table, pd.Series):
        return table
    changed = True
    while changed:
        changed = False
        for axis_idx in range(2):
            n = table.shape[axis_idx]
            for i in range(n):
                row = table.iloc[i, :] if axis_idx == 0 else table.iloc[:, i]
                nan_count = row.isna().sum()
                if nan_count == 1:
                    remaining = row.dropna()
                    if len(remaining) == 0:
                        continue
                    smallest = remaining.idxmin()
                    if axis_idx == 0:
                        table.iloc[i, table.columns.get_loc(smallest)] = np.nan
                    else:
                        table.iloc[table.index.get_loc(smallest), i] = np.nan
                    changed = True
    return table


def _suppress_regression(
    result,
    *,
    redact_intercept: int | None = None,
    widen_alpha: float | None = None,
    group_counts: dict | None = None,
):
    """Return a lightweight namespace mimicking the statsmodels API surface
    we need (params, conf_int).
    """
    import types
    raw_params = result.params
    params = raw_params.copy()
    if widen_alpha is not None:
        ci = result.conf_int(alpha=widen_alpha)
    else:
        ci = result.conf_int()
    # statsmodels returns plain ndarrays when fit on raw numpy arrays; convert
    # to pandas so we have a uniform name-based API. Use "const" for the
    # intercept (statsmodels add_constant convention) and x1, x2, ... otherwise.
    if not hasattr(params, "index"):
        names = ["const"] + [f"x{i}" for i in range(1, len(params))]
        params = pd.Series(params, index=names)
    if not hasattr(ci, "loc"):
        ci = pd.DataFrame(ci, index=params.index, columns=[0, 1])

    if redact_intercept is not None and group_counts is not None:
        smallest = min(group_counts.values())
        if smallest < redact_intercept:
            intercept_name = "const" if "const" in params.index else params.index[0]
            params[intercept_name] = np.nan
            ci.loc[intercept_name] = np.nan

    ns = types.SimpleNamespace()
    ns.params = params
    ns.conf_int = lambda: ci
    ns.summary_text = (
        f"Suppressed regression result:\n{params.to_string()}\n\nCI:\n{ci.to_string()}"
    )
    return ns


def _suppress_plot(
    xy,
    *,
    hexbin: bool = False,
    bin_histogram: bool = False,
    gridsize: int = 30,
    bins: int = 20,
    min_count: int = 5,
    jitter: tuple[float, float] | None = None,
):
    x, y = xy
    x = np.asarray(x)
    y = np.asarray(y)
    if hexbin:
        h, xedges, yedges = np.histogram2d(x, y, bins=gridsize)
        h_safe = np.where(h >= min_count, h, 0)
        return {
            "x_centers": (xedges[:-1] + xedges[1:]) / 2,
            "y_centers": (yedges[:-1] + yedges[1:]) / 2,
            "counts": h_safe,
        }
    if bin_histogram:
        h, edges = np.histogram(x, bins=bins)
        h_safe = np.where(h >= min_count, h, 0)
        return {"edges": edges, "counts": h_safe}
    if jitter is not None:
        rng = np.random.default_rng()
        sd_x, sd_y = jitter
        return (x + rng.normal(0, sd_x, size=len(x)),
                y + rng.normal(0, sd_y, size=len(y)))
    raise ValueError("Plot suppress requires one of: hexbin, bin_histogram, jitter")


# ============================================================================
# Risk
# ============================================================================


@dataclass
class RiskReport:
    """Disclosure-risk metrics for a set of quasi-identifiers."""
    k_min: int
    k_median: float
    k_below_5: int
    units_at_risk: int
    l_min: float | None
    l_median: float | None
    t_max: float | None
    distinct_combos: int
    suggestions: list[str]

    def describe(self) -> str:
        """Return a plain-English summary."""
        lines = [
            f"k-anonymity: min={self.k_min}, median={self.k_median:.1f}",
            f"  records with k<5: {self.k_below_5}",
            f"  unique units on quasi-IDs: {self.units_at_risk}",
            f"  distinct QI combinations: {self.distinct_combos}",
        ]
        if self.l_min is not None:
            lines.append(f"l-diversity: min={self.l_min:.2f}, median={self.l_median:.2f}")
        if self.t_max is not None:
            lines.append(f"t-closeness: max={self.t_max:.3f}")
        if self.suggestions:
            lines.append("Suggestions:")
            for s in self.suggestions:
                lines.append(f"  - {s}")
        return "\n".join(lines)

    def diff(self, other: "RiskReport") -> dict:
        """Return before/after pairs for key metrics."""
        return {
            "k_min": (self.k_min, other.k_min),
            "k_median": (self.k_median, other.k_median),
            "k_below_5": (self.k_below_5, other.k_below_5),
            "units_at_risk": (self.units_at_risk, other.units_at_risk),
            "distinct_combos": (self.distinct_combos, other.distinct_combos),
        }


def risk(
    data: pd.DataFrame,
    *,
    quasi_ids: Sequence[str],
    sensitive: Sequence[str] | None = None,
    unit_id: str | None = None,
) -> RiskReport:
    """Compute disclosure-risk metrics for a set of quasi-identifiers.

    Returns a RiskReport with k-anonymity, l-diversity (if `sensitive` given),
    uniqueness counts, and heuristic suggestions.
    """
    quasi_ids = list(quasi_ids)
    sensitive = list(sensitive) if sensitive else None

    # Per-unit projection: each unit counted once on its (assumed-invariant) quasi_ids
    if unit_id is not None:
        proj = data.groupby(unit_id)[quasi_ids].first().reset_index()
        eq_classes = proj.groupby(quasi_ids).size()
    else:
        eq_classes = data.groupby(quasi_ids).size()

    k_min = int(eq_classes.min())
    k_median = float(eq_classes.median())
    k_below_5 = int((eq_classes < 5).sum())
    units_at_risk = int((eq_classes == 1).sum())
    distinct_combos = int(len(eq_classes))

    l_min = l_median = None
    t_max = None
    if sensitive:
        sens_col = sensitive[0]
        l_vals = []
        # iterate over equivalence classes; build mask from quasi_id tuple
        for keys, _ in eq_classes.items():
            if not isinstance(keys, tuple):
                keys = (keys,)
            mask = np.ones(len(data), dtype=bool)
            for c, v in zip(quasi_ids, keys):
                mask &= (data[c] == v).values
            sub = data.loc[mask, sens_col]
            if len(sub) == 0:
                continue
            probs = sub.value_counts(normalize=True).values
            entropy = -np.sum(probs * np.log(np.clip(probs, 1e-12, 1)))
            l_vals.append(np.exp(entropy))
        if l_vals:
            l_min = float(min(l_vals))
            l_median = float(np.median(l_vals))

    suggestions = []
    if k_min < 5:
        suggestions.append(
            f"k_min={k_min} < 5: consider widening quasi-ID bins (bin, shorten, collapse) "
            f"or suppressing rare combinations."
        )
    if units_at_risk > 0:
        suggestions.append(
            f"{units_at_risk} units are uniquely identifiable on these quasi-IDs."
        )

    return RiskReport(
        k_min=k_min,
        k_median=k_median,
        k_below_5=k_below_5,
        units_at_risk=units_at_risk,
        l_min=l_min,
        l_median=l_median,
        t_max=t_max,
        distinct_combos=distinct_combos,
        suggestions=suggestions,
    )


# ============================================================================
# Meta verbs
# ============================================================================


def protect(
    data: pd.DataFrame,
    *,
    recipe: dict,
    unit_id: str | None = None,
    audit: bool = True,
) -> tuple[pd.DataFrame, "TransformLog"]:
    """Apply many verbs in declared order via a recipe dict.

    A recipe maps column → step (or list of steps). Each step is a single-key
    dict mapping verb-name → params:

        recipe = {
            "income":    {"winsorize": {"limits": (0.01, 0.99)}},
            "icd":       {"shorten": {"sep": "."}},
            "cost":      [{"winsorize": {...}}, {"noise": {...}}],
        }

    Returns (df, TransformLog).
    """
    out = data.copy()
    log = TransformLog()

    # column-targeted verbs (take columns as first positional arg)
    _verb_registry = {
        "noise": noise,
        "jitter": jitter,
        "winsorize": winsorize,
        "bin": bin,
        "coarsen": coarsen,
        "year": year,
        "month": month,
        "diff": diff,
        "shorten": shorten,
        "collapse": collapse,
        "pseudonymize": pseudonymize,
        "swap": swap,
    }
    # whole-frame verbs (no column arg)
    _frame_verbs = {
        "insert": insert,
        "eliminate": eliminate,
    }

    for col, ops in recipe.items():
        steps = ops if isinstance(ops, list) else [ops]
        for step in steps:
            if len(step) != 1:
                raise ValueError(f"Each step must have one verb, got {step}")
            verb_name, params = next(iter(step.items()))
            params = dict(params)

            # auto-inject unit_id when the call doesn't specify one and the
            # target verb accepts it (some verbs like pseudonymize do not)
            if unit_id is not None and "unit_id" not in params:
                target_fn = _verb_registry.get(verb_name) or _frame_verbs.get(verb_name)
                if target_fn is not None:
                    sig = inspect.signature(target_fn)
                    if "unit_id" in sig.parameters:
                        params["unit_id"] = unit_id

            if verb_name in _verb_registry:
                fn = _verb_registry[verb_name]
                if verb_name == "pseudonymize":
                    result = fn(out, col, **params)
                    if isinstance(result, tuple):
                        out, _key = result
                    else:
                        out = result
                else:
                    out = fn(out, col, **params)
            elif verb_name in _frame_verbs:
                out = _frame_verbs[verb_name](out, **params)
            else:
                raise ValueError(f"Unknown verb in recipe: {verb_name!r}")

            log.add(
                function=verb_name,
                columns=[col],
                params={k: v for k, v in params.items() if k != "unit_id"},
                rows_affected=len(out),
                units_affected=out[unit_id].nunique() if unit_id and unit_id in out.columns else None,
            )

    if audit:
        return out, log
    return out


def profile(
    data: pd.DataFrame,
    name: str,
    **kwargs,
) -> tuple[pd.DataFrame, "TransformLog"]:
    """Apply a named composition.

    Available profiles:
      safe_harbor          : HIPAA Safe Harbor identifier removal
      microdata_no         : microdata.no Tiltak 1/6/7 input-side rules
      gdpr_pseudonymize    : pseudonymize IDs, audit-log residual GDPR status
      health_research      : composed defaults for health-research release
      k_anonymize          : iterative generalization to target k
    """
    profiles = {
        "safe_harbor": _profile_safe_harbor,
        "microdata_no": _profile_microdata_no,
        "gdpr_pseudonymize": _profile_gdpr_pseudonymize,
        "health_research": _profile_health_research,
        "k_anonymize": _profile_k_anonymize,
    }
    if name not in profiles:
        raise ValueError(f"Unknown profile: {name!r}. Available: {list(profiles)}")
    return profiles[name](data, **kwargs)


# ============================================================================
# Profile implementations
# ============================================================================


def _profile_safe_harbor(
    data: pd.DataFrame,
    *,
    date_cols: Sequence[str] = (),
    zip_col: str | None = None,
    id_cols: Sequence[str] = (),
    age_col: str | None = None,
    zip_population_threshold: int = 20_000,
    random_state: int | None = None,
):
    """HIPAA Safe Harbor (§164.514(b)(2))."""
    out = data.copy()
    log = TransformLog()

    for col in id_cols:
        if col in out.columns:
            out, _key = pseudonymize(out, col, method="random", random_state=random_state)
            log.add(function="pseudonymize", columns=[col],
                    params={"method": "random"}, rows_affected=len(out),
                    notes="HIPAA Safe Harbor identifier removal")

    for col in date_cols:
        if col in out.columns:
            out = year(out, col)
            log.add(function="year", columns=[col], params={},
                    rows_affected=len(out),
                    notes="HIPAA: year-only resolution")

    if zip_col and zip_col in out.columns:
        out = shorten(out, zip_col, keep=3)
        zip3_counts = out[zip_col].value_counts()
        below = zip3_counts[zip3_counts < zip_population_threshold].index
        out.loc[out[zip_col].isin(below), zip_col] = "***"
        log.add(function="shorten", columns=[zip_col],
                params={"keep": 3, "pop_threshold": zip_population_threshold},
                rows_affected=len(out),
                notes=f"HIPAA: ZIP3 with pop >= {zip_population_threshold}")

    if age_col and age_col in out.columns:
        out = winsorize(out, age_col, limits=(None, 90), method="value")
        log.add(function="winsorize", columns=[age_col],
                params={"limits": (None, 90), "method": "value"},
                rows_affected=len(out), notes="HIPAA: top-code at 90")

    return out, log


def _profile_microdata_no(
    data: pd.DataFrame,
    *,
    unit_id: str,
    min_population: int = 1000,
    winsorize_cols: Sequence[str] = (),
):
    """microdata.no input-side rules: Tiltak 1, 6, 7."""
    out = data.copy()
    log = TransformLog()

    n_units = out[unit_id].nunique()
    if n_units < min_population:
        raise ValueError(
            f"microdata_no profile requires population >= {min_population}; "
            f"got {n_units} units"
        )
    log.add(function="_assert_min_population", columns=[unit_id],
            params={"min_population": min_population},
            rows_affected=len(out), units_affected=n_units,
            notes=f"Tiltak 1: population check passed ({n_units} >= {min_population})")

    for col in winsorize_cols:
        if col in out.columns:
            out = winsorize(out, col, limits=(0.01, 0.99), method="percentile")
            log.add(function="winsorize", columns=[col],
                    params={"limits": (0.01, 0.99)}, rows_affected=len(out),
                    notes="Tiltak 2: winsorize at 1st/99th percentile")

    return out, log


def _profile_gdpr_pseudonymize(
    data: pd.DataFrame,
    *,
    id_cols: Sequence[str],
    salt: str | None = None,
    random_state: int | None = None,
):
    """GDPR pseudonymization: hash declared IDs, document residual status."""
    out = data.copy()
    log = TransformLog()
    method = "hash" if salt is not None else "random"
    for col in id_cols:
        if col in out.columns:
            out, _key = pseudonymize(out, col, method=method, salt=salt,
                                      random_state=random_state)
            log.add(function="pseudonymize", columns=[col],
                    params={"method": method},
                    rows_affected=len(out),
                    notes="GDPR Art.4(5): output is pseudonymized data, "
                          "still personal data under GDPR")
    return out, log


def _profile_health_research(
    data: pd.DataFrame,
    *,
    unit_id: str,
    quasi_ids: Sequence[str] = (),
    sensitive_cols: Sequence[str] = (),
    k: int = 5,
):
    """Composed defaults for typical health-research release."""
    out = data.copy()
    log = TransformLog()

    for col in sensitive_cols:
        if col in out.columns:
            out = collapse(out, col, rare_below=k)
            log.add(function="collapse", columns=[col],
                    params={"rare_below": k}, rows_affected=len(out))
    return out, log


def _profile_k_anonymize(
    data: pd.DataFrame,
    *,
    quasi_ids: Sequence[str],
    k: int = 5,
    unit_id: str | None = None,
    max_iterations: int = 20,
):
    """Greedy iterative k-anonymization."""
    out = data.copy()
    log = TransformLog()
    for iteration in range(max_iterations):
        report = risk(out, quasi_ids=list(quasi_ids), unit_id=unit_id)
        if report.k_min >= k:
            log.add(function="_k_anonymize_converged",
                    params={"k": k, "iterations": iteration},
                    rows_affected=len(out),
                    notes=f"k_min={report.k_min} >= target k={k}")
            return out, log
        worst_col = None
        worst_count = float("inf")
        for col in quasi_ids:
            if col in out.columns:
                min_count = out[col].value_counts().min()
                if min_count < worst_count:
                    worst_count = min_count
                    worst_col = col
        if worst_col is None:
            break
        out = collapse(out, worst_col, rare_below=k)
        log.add(function="collapse", columns=[worst_col],
                params={"rare_below": k}, rows_affected=len(out),
                notes=f"iteration {iteration}, worst k_min={report.k_min}")
    return out, log


# Expose private helpers as attributes on `protect` so that, after the package
# does `from .protect import *`, callers (and tests) can still reach the
# internal building blocks via `protect.protect._resolve_random_state`. The
# star-import shadows the submodule, so attaching helpers to the function
# preserves both the callable surface and the helper-introspection surface.
protect._resolve_random_state = _resolve_random_state
protect._validate_columns = _validate_columns
protect._select_share = _select_share
protect._apply_per_unit = _apply_per_unit
protect._check_unit_invariant = _check_unit_invariant
