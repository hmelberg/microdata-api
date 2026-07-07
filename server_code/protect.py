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
import re
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

import numpy as np
import pandas as pd


def _t(s, **kw):
    # Meldingsspråk følger m2py.M2PY_LANG ('no' er nøkkelspråket; mangler
    # oversettelse vises norsk). Lokal katalog per modul.
    try:
        import sys
        _lang = getattr(sys.modules.get('m2py'), 'M2PY_LANG', 'no')
    except Exception:
        _lang = 'no'
    if _lang == 'en':
        s = _MESSAGES_EN.get(s, s)
    return s.format(**kw) if kw else s


_MESSAGES_EN = {
    # norsk nøkkel -> engelsk
    "{verb} er deterministisk per verdi; delvis anvendelse "
    "(share={share}) støttes ikke fordi det ville gitt inkonsistente "
    "data. Bruk share={default} (standard).":
        "{verb} is deterministic per value; partial application "
        "(share={share}) is not supported because it would produce "
        "inconsistent data. Use share={default} (default).",
    "k_min={k_min} < mål k={k}":
        "k_min={k_min} < target k={k}",
    "k-anonymisering nådde ikke mål k={k}: minste gruppe har "
    "k_min={k_min} etter {max_iterations} iterasjoner. Øk "
    "max_iterations, reduser k, eller generaliser/fjern quasi-"
    "identifikatorer.":
        "k-anonymization did not reach target k={k}: smallest group has "
        "k_min={k_min} after {max_iterations} iterations. Increase "
        "max_iterations, reduce k, or generalize/remove quasi-"
        "identifiers.",
}

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


def _reject_inert_share(share, verb: str, default: float = 1.0) -> None:
    """Deterministiske verb (eller verb-metoder) anvender seg på hver verdi
    likt uansett `share`. En `share` som avviker fra verbets (eller
    metodens) EGET nøytrale utgangspunkt (`default`) ble tidligere godtatt
    men stille ignorert; delvis anvendelse ville gitt inkonsistente data
    (noen rader grovkornet/byttet/omkodet, andre ikke) eller — for verb der
    `share` rett og slett ikke leses — en illusjon av kontroll brukeren ikke
    har. Avvis tydelig i stedet.

    `default` lar denne ene hjelpefunksjonen dekke verb med ulik nøytral
    `share` (winsorize/bin/coarsen/year/month: 1.0; swap sine
    shuffle/pram-metoder: swap sin egen signatur-default 0.05 — se
    `swap`-dokstrengen for hvorfor 0.05 er default der).
    """
    if share is not None and share != default:
        raise ValueError(_t(
            "{verb} er deterministisk per verdi; delvis anvendelse "
            "(share={share}) støttes ikke fordi det ville gitt inkonsistente "
            "data. Bruk share={default} (standard).",
            verb=verb, share=share, default=default,
        ))


def _warn_inert_unit_id(unit_id, verb: str) -> None:
    """`unit_id` only matters where a verb draws something ONCE per unit and
    broadcasts it to that unit's rows (noise, jitter, ...). Verbs that are a
    pure deterministic function of each value (winsorize, bin, ...) have
    nothing to draw, so `unit_id` has zero effect there — it is accepted
    only so `protect(recipe=..., unit_id=...)` can auto-inject it into every
    verb's call uniformly without erroring. Warn (not raise) so a caller who
    passed `unit_id` expecting per-unit consistency here learns it did
    nothing, without breaking the `protect()` auto-injection path.
    """
    if unit_id is not None:
        warnings.warn(
            f"{verb}: unit_id={unit_id!r} has no effect — {verb} is a "
            f"deterministic function of each value, with nothing to draw "
            f"per unit. Accepted only for compatibility with "
            f"protect(recipe=..., unit_id=...).",
            stacklevel=2,
        )


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


def _assert_perturbed(
    before: pd.DataFrame,
    after: pd.DataFrame,
    columns: Sequence[str],
    verb: str,
) -> None:
    """Raise if a perturbation verb changed nothing on `columns`.

    Several verbs (swap, noise, ...) can silently degenerate into a no-op on
    small datasets or unlucky parameter choices (e.g. `share` rounding to 0
    pairs) while still logging success. That overstates protection: the
    caller believes the data was perturbed when it is bit-for-bit identical.
    Call this after the verb has done its work; it treats NaN==NaN as
    "unchanged" so it doesn't flag columns that were already missing.
    """
    for col in columns:
        b = before[col].values
        a = after[col].values
        same = (b == a) | (pd.isna(b) & pd.isna(a))
        if not np.all(same):
            return
    raise ValueError(
        f"{verb}: no values were changed in {list(columns)} (0 of {len(before)} "
        f"rows differ). The requested perturbation had no effect — check "
        f"share/scale/k parameters; data was NOT protected."
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
        Fraction of units (or rows when unit_id is None) to perturb. Defaults
        to 1.0 (perturb everyone) because a row left untouched leaks its own
        exact value; contrast with `swap` (default 0.05) and `insert`
        (default 0.01), where touching every row/adding many decoys would
        itself distort the released statistics — see each verb's docstring.
    direction : {'both', 'up', 'down'}, default 'both'
        Asymmetric noise; clipped to non-negative or non-positive when not 'both'.
    clip : (lo, hi) | None
        Post-noise clipping.
    by : str | None
        Grouping for method='group_mean' (sort within group before grouping
        into k-tuples). Rows whose `by` value is NaN are left unperturbed
        (treated as outside any group) rather than turned into NaN.
    unit_id : str | None
        When set, noise is drawn once per unit and broadcast.
    random_state : int | Generator | None

    Returns
    -------
    DataFrame
        Copy of `data` with perturbed columns.

    Raises
    ------
    ValueError
        If method='group_mean' and scale is not an integer >= 2 (scale means
        group size k for this method, not a standard deviation), or if the
        requested perturbation changed nothing (share > 0 but 0 values
        differ from the input — see `_assert_perturbed`).
    """
    rng = _resolve_random_state(random_state)
    columns = _validate_columns(data, columns)
    out = data.copy()

    if method == "group_mean":
        if scale == "auto":
            k = 3
        else:
            k_float = float(scale)
            if k_float < 2 or not k_float.is_integer():
                raise ValueError(
                    f"noise(method='group_mean'): scale means the group size "
                    f"k (number of records averaged together per group), and "
                    f"must be an integer >= 2; got scale={scale!r}. For "
                    f"SD-scaled noise use method='gaussian' (or another "
                    f"non-group method) instead."
                )
            k = int(k_float)
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
            # Only rows actually selected by `share` were perturbed (the
            # rest have new == original, via the np.where(select_mask, ...)
            # above); clipping the whole column would also alter values
            # that were never touched by noise, silently expanding the
            # verb's effect beyond `share`.
            clipped = np.clip(new, clip[0], clip[1])
            new = np.where(select_mask.values, clipped, new)

        out[col] = new

    _assert_perturbed(data, out, columns, "noise")
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

    Rows whose `by` value is NaN are left unperturbed (treated as outside any
    group) instead of coming back as NaN in `col`.
    """
    if k < 2:
        return data[col]

    def _agg(s: pd.Series) -> pd.Series:
        sorted_idx = s.sort_values().index
        n = len(sorted_idx)
        result = s.copy()
        starts = list(range(0, n, k))
        # Ascending sort means a trailing remainder group (< k members) holds
        # the LARGEST values in the series. Replacing it with its own mean
        # (itself, if it's a singleton) would leave the most disclosive
        # value unperturbed. Standard microaggregation practice: merge any
        # such remainder into the last full group instead of giving it its
        # own (too-small) group.
        if len(starts) >= 2 and (n - starts[-1]) < k:
            starts = starts[:-1]
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else n
            group = sorted_idx[start:end]
            result.loc[group] = s.loc[group].mean()
        return result

    if by is None:
        return _agg(data[col])

    by_notna = data[by].notna()
    result = data[col].copy()
    result.loc[by_notna] = data.loc[by_notna].groupby(by, group_keys=False)[col].apply(_agg)
    return result


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

    share : float in [0, 1], default 1.0
        Fraction of units (or rows when unit_id is None) to perturb. Same
        rationale as `noise`'s default (1.0): an unperturbed row leaks its
        own exact value, unlike `swap` (default 0.05) or `insert` (default
        0.01) — see each verb's docstring.

    Default scale='auto' computes 0.05 x column standard deviation for
    numeric columns (uniform jitter drawn in [-scale, scale]) and '1 day'
    for date columns. This mirrors `noise`'s own 'auto' convention
    (0.05 x SD) so the two verbs are comparably protective; it deliberately
    replaced an earlier 0.01 x column_range default, which was cosmetic —
    recoverable to within 1% of the range regardless of how spread out (or
    outlier-dominated) the column actually was. If a column has zero/NaN
    SD, falls back to 0.01 x column_range (or 1.0 if that is also
    degenerate).
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
        if scale == "auto":
            warnings.warn(
                f"jitter(scale='auto') on column {col!r}: using "
                f"distribution={distribution!r}, magnitude={col_scale!r} "
                f"(uniform half-width or gaussian SD; see docstring for how "
                f"'auto' is derived) — inspect before relying on this for "
                f"disclosure control.",
                stacklevel=2,
            )

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
    """Compute the effective scale for jitter, handling 'auto'.

    Numeric 'auto' is 0.05 x column SD (same fraction `noise` uses for its
    own 'auto'), not a fraction of the raw range — SD reflects the typical
    spread of the data, whereas range is dominated by a single outlier pair
    and can make 'auto' either negligibly small or needlessly huge. Falls
    back to 0.01 x range (then 1.0) only when SD is unusable (0 or NaN,
    e.g. a constant or all-NaN column).
    """
    if scale == "auto":
        if is_date:
            return pd.Timedelta("1 day")
        sd = float(series.std())
        if sd and not np.isnan(sd):
            return 0.05 * sd
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

    Rows whose `by` value is NaN are left unperturbed (treated as outside
    any group) rather than turned into NaN.

    unit_id, share
        Accepted for signature consistency with `protect(unit_id=...)`.
        `winsorize` is a deterministic function of each value (there is
        nothing to draw per unit), so `unit_id` is inert (warns rather than
        raising, since it's routinely auto-injected by `protect()`); a
        non-default `share` (!= 1.0) is rejected outright, like `coarsen`/
        `year`/`month`, rather than silently having no effect.
    """
    _reject_inert_share(share, "winsorize")
    _warn_inert_unit_id(unit_id, "winsorize")
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
            by_notna = out[by].notna()
            out.loc[by_notna, col] = out.loc[by_notna].groupby(
                by, group_keys=False
            )[col].apply(_grp)

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

    unit_id, share
        Accepted for signature consistency with `protect(unit_id=...)`; see
        `winsorize`'s docstring for the rationale — `bin` is deterministic
        per value, so `unit_id` is inert (warns) and non-default `share` is
        rejected.
    """
    _reject_inert_share(share, "bin")
    _warn_inert_unit_id(unit_id, "bin")
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
            # Build the documented "lo-hi" labels from interval edges
            # ourselves — `cat.astype(str)` produces pandas' own repr
            # ("(0.999, 2.5]") instead, and (being a plain str cast) also
            # turns missing values into the literal string 'nan' rather
            # than leaving them as real, still-missing NaN.
            categories = getattr(cat, "cat", cat).categories
            # `include_lowest=True` nudges the LOWEST interval's left bound
            # a hair below the requested edge (e.g. -0.001) so a
            # right-closed interval can still capture the exact minimum
            # value — an implementation detail of pd.cut, not a real bin
            # boundary. `categories` is always in ascending order, so the
            # first entry is that nudged interval; display the originally
            # requested lower edge for it instead.
            true_min = float(np.min(edges))
            lowest_iv = categories[0] if len(categories) else None
            mapping = {
                iv: f"{_fmt_bin_edge(true_min if iv == lowest_iv else iv.left)}"
                    f"-{_fmt_bin_edge(iv.right)}"
                for iv in categories
            }
            # na_action='ignore': NaN (missing input) must stay NaN, not be
            # looked up in `mapping` (where it isn't a key anyway).
            out[col] = cat.map(mapping, na_action="ignore")
        elif labels == "midpoint":
            mids = {iv: (iv.left + iv.right) / 2 for iv in cat.cat.categories}
            out[col] = cat.map(mids).astype(float)
        elif labels == "index":
            out[col] = cat.cat.codes
        else:
            mapping = dict(zip(cat.cat.categories, labels))
            out[col] = cat.map(mapping)

    return out


def _fmt_bin_edge(x: float) -> str:
    """Format a bin edge for the 'range' label: whole numbers print without
    a trailing '.0' (so labels read "10-20", matching the docstring), other
    values keep a compact decimal form."""
    if float(x).is_integer():
        return str(int(x))
    return f"{x:g}"


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
        Accepted for signature consistency. `coarsen` is deterministic
        per-value, so `unit_id`/`random_state` are inert; a non-default
        `share` (< 1.0) is rejected rather than silently ignored, since
        partial coarsening would produce inconsistent data.

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
    _reject_inert_share(share, "coarsen")
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


def _to_int_maybe_nullable(s: pd.Series) -> pd.Series:
    """Truncate a float Series toward zero and cast to int, preserving any
    NaN as a per-row missing value instead of raising or corrupting the
    whole column.

    Uses plain `int64` (via `.astype(int)`) when there is no missing value
    at all — matching the exact dtype callers got before NaN-handling was
    added, so existing exact-dtype comparisons (`assert_series_equal`)
    keep working. Only switches to nullable `Int64` (which can hold <NA>)
    when the column actually contains a missing row.
    """
    truncated = np.trunc(s)
    if truncated.isna().any():
        return truncated.astype("Int64")
    return truncated.astype(int)


def _ym_to_date(year_series: pd.Series, month: int, na_mask: pd.Series) -> pd.Series:
    """Build a day-1-of-`month` date from an already-NaN-filled integer year
    series, then mask rows back to NaT wherever the original input (per
    `na_mask`) was missing.

    `year_series` must contain no NaN itself (the caller fills a
    placeholder value first): building the date via string concatenation
    can't parse a NaN-derived fragment like 'nan-01-01', so we sidestep
    that by filling, building, and only then re-applying missingness
    row-by-row via `na_mask` — instead of letting one NaT poison every
    row's string (pandas would upcast the whole int column to float once
    any NaN is present, and 2020 becomes '2020.0').
    """
    result = pd.to_datetime(year_series.astype(str) + f"-{month:02d}-01")
    return result.mask(na_mask, other=pd.NaT)


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

    A missing (NaT) input propagates to a missing output on that row only —
    other rows in the same column are computed normally, not corrupted by
    the presence of the NaT (see `_year_month_na_safe_int`/date-string
    helpers below).
    """
    _reject_inert_share(share, "year")
    columns = _validate_columns(data, columns)
    out = data.copy()
    for col in columns:
        dt = pd.to_datetime(out[col])
        na_mask = dt.isna()
        y = dt.dt.year
        if bin is None:
            if as_date:
                out[col] = _ym_to_date(y.fillna(1904).astype(int), 1, na_mask)
            else:
                # Nullable Int64 (not plain int64/`.astype(int)`) so a
                # missing row becomes <NA> instead of raising
                # IntCastingNaNError for the whole column.
                out[col] = y.astype("Int64").mask(na_mask)
        else:
            floor = (y // bin) * bin
            ceil = floor + bin - 1
            if as_date:
                out[col] = _ym_to_date(floor.fillna(1904).astype(int), 1, na_mask)
            else:
                lbl = (floor.fillna(0).astype("Int64").astype(str) + "-" +
                       ceil.fillna(0).astype("Int64").astype(str))
                out[col] = lbl.mask(na_mask)
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
    """Truncate dates to month resolution. `bin=3` groups into quarters.

    A missing (NaT) input propagates to a missing output on that row only.
    Previously, once ANY row was NaT, `y`/`m` upcast to float for the WHOLE
    column and `.astype(str)` rendered every row as e.g. '2020.0-1.0' (and
    the NaT rows as 'nan-nan') instead of leaving the valid rows correct.
    """
    _reject_inert_share(share, "month")
    columns = _validate_columns(data, columns)
    out = data.copy()
    for col in columns:
        dt = pd.to_datetime(out[col])
        na_mask = dt.isna()
        y = dt.dt.year
        m = dt.dt.month
        if bin is not None:
            m = ((m - 1) // bin) * bin + 1
        # Fill missing rows with a placeholder BEFORE building strings, so
        # valid rows keep clean int formatting ('2020', not '2020.0'); the
        # placeholder rows are overwritten with real missingness afterward.
        y_i = y.fillna(1904).astype("Int64").astype(str)
        m_i = m.fillna(1).astype("Int64").astype(str).str.zfill(2)
        if as_date:
            result = pd.to_datetime(y_i + "-" + m_i + "-01")
            out[col] = result.mask(na_mask, other=pd.NaT)
        else:
            result = y_i + "-" + m_i
            out[col] = result.mask(na_mask)
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
        # `.dt.days` already yields NaN (not a crash) for a NaT row; the
        # rest of this function must not let that NaN corrupt other rows.

        if unit == "days":
            result_f = days.astype(float)
        elif unit == "months":
            result_f = days / 30.44
        elif unit == "years":
            result_f = days / 365.25
        else:
            raise ValueError(f"Unknown unit: {unit!r}")

        # Previously `.astype(int)` here raised IntCastingNaNError as soon
        # as any date in `col` was missing, for the WHOLE column — not just
        # the missing row. `_to_int_maybe_nullable` truncates and keeps
        # every valid row's integer value, turning only the missing row(s)
        # into <NA>.
        result = _to_int_maybe_nullable(result_f)

        if keep_order and unit_id is not None:
            for pid, grp in data.groupby(unit_id):
                orig_order = dt.loc[grp.index].rank(method="first")
                # `.astype(float)` first: `Series.rank()` on nullable Int64
                # doesn't treat pd.NA as missing the way it treats a plain
                # float NaN (it ranks it as if it were a real value instead
                # of excluding it per na_option='keep'), which would make a
                # unit with a missing date look reordered even though
                # nothing moved. Float NaN ranks correctly.
                new_order = result.loc[grp.index].astype(float).rank(method="first")
                # NaN ranks as NaN (na_option='keep' is the rank() default);
                # NaN == NaN is False, so without this NaN-safe comparison a
                # unit with even one NaT date would always look "reordered"
                # and raise, even though nothing was actually reordered.
                same = ((orig_order.values == new_order.values) |
                        (pd.isna(orig_order.values) & pd.isna(new_order.values)))
                if not same.all():
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
        # Work on the raw column, not `.astype(str)` first: casting the
        # whole column to str up front turns missing values into the
        # literal string 'nan' *before* `_truncate`'s `pd.isna` guard ever
        # runs, so that guard becomes dead code and missing codes get
        # truncated like real data (e.g. 'nan' -> 'n' with keep=1). Let
        # each value decide for itself via `_truncate`, which already
        # stringifies non-missing values and passes missing ones through.
        raw = out[col]

        if per_value:
            def _apply_rule(v):
                if pd.isna(v):
                    return v
                vs = str(v)
                for pattern, action in per_value.items():
                    matches = (vs == pattern or
                               (pattern.endswith("*") and vs.startswith(pattern[:-1])))
                    if matches:
                        if action == "keep_full":
                            return vs
                        if action.startswith("keep_"):
                            n = int(action.split("_")[1])
                            return _truncate(v, n)
                return _truncate(v, keep)
            s = raw.map(_apply_rule)
        else:
            s = raw.map(lambda v: _truncate(v, keep))

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

    Rows whose `by` value is NaN are left unperturbed (treated as outside
    any group) rather than turned into NaN.
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
            # `series.isin(keep_set)` is False for NaN (NaN is never "in"
            # anything, and value_counts() already excludes it from
            # keep_set), so without the explicit `| series.isna()` below
            # missingness would silently get recoded to `other_label` —
            # collapsing "we don't know" into a specific category and
            # destroying the fact that it was missing.
            return series.where(series.isin(keep_set) | series.isna(), other_label)

        if by is not None:
            by_notna = out[by].notna()
            out.loc[by_notna, col] = out.loc[by_notna].groupby(
                by, group_keys=False
            )[col].apply(_apply_threshold)
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

    share : float in [0, 1], default 0.01
        Fraction of decoy rows/units to add, relative to the real data. The
        smallest default of the three share-bearing verbs (vs. `noise`/
        `jitter`'s 1.0 and `swap`'s 0.05): decoys are fabricated records
        that distort real aggregate statistics (counts, sums, means) the
        more of them there are, so a low default limits that distortion;
        this function also warns above share=0.05.

    Decoy unit_id values are generated to blend into the format of the
    real IDs (continuing a numeric range, or matching the observed
    character set/length) rather than a fixed marker like 'DECOY000000' —
    a constant prefix defeats the point of a decoy, since one filter
    removes every one of them. The exact count of decoys added is only
    surfaced via a `UserWarning` (this function still returns a plain
    DataFrame); which specific rows/IDs are decoys is not disclosed.
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
            sample[unit_id] = _decoy_ids(data[unit_id], n_decoys, rng)
        warnings.warn(
            f"insert: added {n_decoys} decoy row(s) (not marked as such — "
            f"see docstring)",
            stacklevel=2,
        )
        return pd.concat([data, sample], ignore_index=True)

    # level == "unit"
    n_units = data[unit_id].nunique()
    n_decoy_units = n if n is not None else int(round(n_units * share))
    row_counts = data.groupby(unit_id).size().values
    decoy_ids = _decoy_ids(data[unit_id], n_decoy_units, rng)
    decoys = []
    for i in range(n_decoy_units):
        rc = int(rng.choice(row_counts))
        sample = _generate_decoys(data, rc, source, rng, modify)
        sample[unit_id] = decoy_ids[i]
        decoys.append(sample)
    warnings.warn(
        f"insert: added {n_decoy_units} decoy unit(s) (not marked as such "
        f"— see docstring)",
        stacklevel=2,
    )
    if decoys:
        return pd.concat([data] + decoys, ignore_index=True)
    return data.copy()


def _decoy_ids(real_ids: pd.Series, n: int, rng: np.random.Generator) -> list:
    """Generate `n` new IDs that blend into the format of `real_ids`,
    instead of an obviously-filterable fixed marker like 'DECOY000000'.

    If every real ID is <non-digit prefix><zero-padded number> and shares
    one common prefix (e.g. 'PT0001', 'PT0002', ...), decoys continue the
    numeric range past the observed max, zero-padded to at least the
    widest existing width. Otherwise, decoys are random strings drawn from
    the character set and length distribution actually observed in
    `real_ids`, resampled on any collision so they can never coincide with
    (or be distinguished on sight from) a real ID.
    """
    ids = [str(x) for x in real_ids]
    matches = [_DECOY_ID_PATTERN.match(s) for s in ids]
    if ids and all(matches):
        prefixes = {m.group("prefix") for m in matches}
        if len(prefixes) == 1:
            prefix = next(iter(prefixes))
            nums = [int(m.group("num")) for m in matches]
            width = max(len(m.group("num")) for m in matches)
            start = max(nums) + 1
            return [
                f"{prefix}{(start + i):0{max(width, len(str(start + i)))}d}"
                for i in range(n)
            ]

    # Fallback: random strings matching the observed length/character-set,
    # resampled on collision.
    charset = sorted(set("".join(ids))) or list("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    lengths = [len(s) for s in ids] or [8]
    seen = set(ids)
    out: list = []
    attempts = 0
    max_attempts = n * 200 + 1000
    while len(out) < n and attempts < max_attempts:
        length = int(rng.choice(lengths))
        candidate = "".join(rng.choice(charset, size=length))
        attempts += 1
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    extra_len = max(lengths) + 1 if lengths else 8
    while len(out) < n:
        # Character space exhausted at the observed length (tiny real ID
        # space) — extend the length rather than give up or fall back to a
        # fixed marker.
        candidate = "".join(rng.choice(charset, size=extra_len)) + str(len(out))
        extra_len += 1
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


_DECOY_ID_PATTERN = re.compile(r"^(?P<prefix>\D*)(?P<num>\d+)$")


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

    level='unit' exchanges each matched pair's *entire* value sequence for
    `columns` (not just one row broadcast to the whole unit): unit A's rows
    get unit B's values and vice versa, matched positionally in row order.
    Units are only ever paired with another unit that has the same number of
    rows, so the exchange is always exact (no truncation/padding). If a unit
    has no same-row-count partner available it is left unswapped.

    share : float in [0, 1], default 0.05
        Fraction of rows/units to swap for method='rank'/'random'. Lower
        than `noise`/`jitter`'s default of 1.0 because swapping is a much
        more disruptive per-record edit (an entire value trades places with
        another record's) — swapping every row by default would scramble
        far more than needed for k-anonymity-style protection; contrast
        with `insert` (default 0.01, distortion-driven for a different
        reason — see its docstring). Ignored (and rejected if given a
        non-default value) for method='shuffle'/'pram', which always
        permute/recode every value in `columns` — there's no notion of a
        partial share for those.

    Raises
    ------
    ValueError
        If share > 0 but the requested swap changed nothing (see
        `_assert_perturbed`) — e.g. every unit has a unique row count under
        level='unit', or n < 2 under level='row'. Also raised if `share` is
        given a non-default value together with method='shuffle'/'pram',
        since those methods always act on 100% of values and a partial
        share would be silently ignored otherwise.
    """
    columns = _validate_columns(data, columns)
    if level == "unit" and unit_id is None:
        raise ValueError("level='unit' requires unit_id to be set")

    rng = _resolve_random_state(random_state)
    out = data.copy()

    if method == "shuffle":
        _reject_inert_share(share, "swap(method='shuffle')", default=0.05)
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
        _reject_inert_share(share, "swap(method='pram')", default=0.05)
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
            if share > 0 and n >= 2:
                # Guarantee at least one pair rather than silently rounding
                # down to 0 (default share=0.05 on <30 rows swapped nothing
                # while still logging success).
                n_swap = max(2, n_swap)
            col_idx = out.columns.get_loc(col)
            if method == "rank":
                # order[pos] = rad-indeks ved rang-posisjon pos.
                order = out[col].rank(method="first").values.argsort()
                # Invers: rang-posisjonen til hver rad. Uten denne ble den
                # tilfeldige rad-indeksen `i` brukt som om den var en rang-
                # posisjon, så byttepartneren var ikke nær i verdi.
                rank_pos = np.empty(n, dtype=int)
                rank_pos[order] = np.arange(n)
                window = max(1, int(n * swap_range_pct))
                pairs_done = 0
                attempts = 0
                while pairs_done < n_swap // 2 and attempts < n_swap * 10:
                    i = int(rng.integers(0, n))
                    pos_i = int(rank_pos[i])
                    j_candidates = order[max(0, pos_i - window):min(n, pos_i + window + 1)]
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
        if share > 0:
            _assert_perturbed(data, out, columns, "swap")
        return out

    # level == "unit": swap whole unit records between matched units.
    # Only units with an equal number of rows are paired, so the exchange
    # below is always a full, exact positional swap of both units' values.
    units = data[unit_id].unique()
    n_units = len(units)
    row_counts = data.groupby(unit_id).size()
    n_swap_units = int(round(n_units * share))
    if share > 0 and n_units >= 2:
        n_swap_units = max(2, n_swap_units)

    if method == "random":
        buckets: dict[int, list] = {}
        for u in units:
            buckets.setdefault(int(row_counts[u]), []).append(u)
        pairs_list: list = []
        budget = n_swap_units // 2
        for bucket_units in buckets.values():
            if budget <= 0:
                break
            bu = np.array(bucket_units, dtype=object)
            rng.shuffle(bu)
            n_pairs_here = min(len(bu) // 2, budget)
            for k in range(n_pairs_here):
                pairs_list.append((bu[2 * k], bu[2 * k + 1]))
            budget -= n_pairs_here
        pairs = pairs_list
    elif method == "rank":
        # rank units by the first column's per-unit mean, swap within window,
        # restricted to partners with the same row count (see docstring).
        first_col = columns[0]
        unit_vals = data.groupby(unit_id)[first_col].mean().sort_values()
        ordered = list(unit_vals.index)
        window = max(1, int(n_units * swap_range_pct))
        used: set = set()
        pairs_list = []
        for _ in range(n_swap_units // 2):
            available_positions = [k for k in range(n_units) if ordered[k] not in used]
            if not available_positions:
                break
            i_pos = int(rng.choice(available_positions))
            i = ordered[i_pos]
            j_candidates = [ordered[k]
                            for k in range(max(0, i_pos - window), min(n_units, i_pos + window + 1))
                            if ordered[k] != i and ordered[k] not in used
                            and row_counts[ordered[k]] == row_counts[i]]
            if not j_candidates:
                continue
            j = j_candidates[int(rng.integers(0, len(j_candidates)))]
            pairs_list.append((i, j))
            used.add(i)
            used.add(j)
        pairs = pairs_list
    else:
        raise ValueError(f"Unknown method for level='unit': {method!r}")

    for u1, u2 in pairs:
        idx1 = out.index[out[unit_id] == u1]
        idx2 = out.index[out[unit_id] == u2]
        n_rows = min(len(idx1), len(idx2))
        idx1, idx2 = idx1[:n_rows], idx2[:n_rows]
        for col in columns:
            v1 = out.loc[idx1, col].to_numpy().copy()
            v2 = out.loc[idx2, col].to_numpy().copy()
            out.loc[idx1, col] = v2
            out.loc[idx2, col] = v1

    if share > 0:
        _assert_perturbed(data, out, columns, "swap")
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
            if not contribs:
                # Ingen bidragsdata for cellen — ingenting å vurdere
                continue
            x1 = contribs[0]
            if x1 == 0:
                # Alle bidrag er null — ingenting å avsløre
                continue
            sum_rest = sum(contribs[2:])
            # 1-2 bidragsytere er maksimalt avslørende (nest største kan
            # beregne den største eksakt); sum_rest == 0 gir samme situasjon.
            # Begge skal alltid undertrykkes — ikke hoppes over.
            if len(contribs) < 3 or sum_rest == 0 or sum_rest / x1 < p_percent:
                if isinstance(out, pd.Series):
                    out[idx] = np.nan
                else:
                    out.loc[idx] = np.nan

    # rounding
    if round is not None:
        out = (out / round).round() * round

    # fuzzy ranges
    if ranges is not None:
        range_min = ranges[0][0]
        range_max = ranges[-1][1]

        def _range_label(v):
            if pd.isna(v):
                return v
            for lo, hi in ranges:
                if lo <= v <= hi:
                    return f"{lo}-{hi}"
            if v < range_min:
                return f"<{range_min}"
            return f">{range_max}"
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
    random_state: int | np.random.Generator | None = None,
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
        rng = _resolve_random_state(random_state)
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
    k_below_5: int  # RECORDS (not equivalence classes) in classes with k < 5
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
    columns: str | Sequence[str] | None = None,
    *,
    quasi_ids: Sequence[str] | None = None,
    sensitive: Sequence[str] | None = None,
    unit_id: str | None = None,
) -> RiskReport:
    """Compute disclosure-risk metrics for a set of quasi-identifiers.

    Returns a RiskReport with k-anonymity, l-diversity (if `sensitive` given),
    uniqueness counts, and heuristic suggestions.

    Parameters
    ----------
    columns : str | list of str | None
        Positional alias for `quasi_ids` — e.g. `risk(df, ["sex", "zip"])`,
        or even `risk(df, VAR1, VAR2)`-style callers that only have a bare
        list of variable names (no dict/kwargs) can pass them here instead
        of building `quasi_ids=[...]` themselves. Mutually exclusive with
        `quasi_ids`; at least one of the two is required.
    quasi_ids : list of str | None
        Keyword form of the same thing.

    A record with a missing (NaN) value on one or more quasi-IDs is still a
    real record: it forms its own equivalence class(es) with other NaN
    records rather than being dropped from the count (dropping it would
    understate risk — an all-NaN group of 1 is exactly as unique as any
    other singleton).

    When `unit_id` is given, l-diversity/t-closeness are computed on the
    SAME one-row-per-unit projection as k-anonymity (first `sensitive`
    value per unit), not on raw rows — otherwise k describes units while
    l/t describe visits/events, mixing denominators within one report.

    Raises
    ------
    ValueError
        If `data` has 0 rows (nothing to assess), or if quasi-identifiers
        are missing, or given both positionally and via `quasi_ids`.
    """
    if len(data) == 0:
        raise ValueError(
            "risk(): empty dataframe — cannot compute disclosure risk on 0 rows"
        )

    if columns is not None:
        if quasi_ids is not None:
            raise ValueError(
                "risk(): pass quasi-identifiers either positionally "
                "(risk(df, columns)) or via quasi_ids=[...], not both"
            )
        quasi_ids = [columns] if isinstance(columns, str) else list(columns)
    if not quasi_ids:
        raise ValueError(
            "risk() requires quasi-identifiers: risk(df, ['a', 'b']) or "
            "risk(df, quasi_ids=['a', 'b'])"
        )

    quasi_ids = list(quasi_ids)
    sensitive = list(sensitive) if sensitive else None
    sens_col = sensitive[0] if sensitive else None

    # Per-unit projection: each unit counted once on its (assumed-invariant)
    # quasi_ids AND (if given) the sensitive column — l-diversity/t-closeness
    # must use the exact same one-row-per-unit population as k-anonymity, or
    # the report mixes a unit-level k with a row/visit-level l/t.
    # dropna=False: a record with a missing quasi-ID is a real (and often
    # maximally risky) equivalence class, not an invisible one.
    if unit_id is not None:
        proj_cols = quasi_ids + ([sens_col] if sens_col else [])
        base_df = data.groupby(unit_id)[proj_cols].first().reset_index()
    else:
        base_df = data

    eq_classes = base_df.groupby(quasi_ids, dropna=False).size()

    k_min = int(eq_classes.min())
    k_median = float(eq_classes.median())
    # Records (not equivalence classes) that fall in a class with k < 5.
    k_below_5 = int(eq_classes[eq_classes < 5].sum())
    units_at_risk = int((eq_classes == 1).sum())
    distinct_combos = int(len(eq_classes))

    l_min = l_median = None
    t_max = None
    if sens_col:
        # Global fordeling for t-closeness (total-variasjonsavstand per gruppe).
        global_probs = base_df[sens_col].value_counts(normalize=True)
        l_vals = []
        t_vals = []
        # iterate over equivalence classes; build mask from quasi_id tuple
        for keys, _ in eq_classes.items():
            if not isinstance(keys, tuple):
                keys = (keys,)
            mask = np.ones(len(base_df), dtype=bool)
            for c, v in zip(quasi_ids, keys):
                if pd.isna(v):
                    mask &= base_df[c].isna().values
                else:
                    mask &= (base_df[c] == v).values
            sub = base_df.loc[mask, sens_col]
            if len(sub) == 0:
                continue
            sub_probs = sub.value_counts(normalize=True)
            probs = sub_probs.values
            entropy = -np.sum(probs * np.log(np.clip(probs, 1e-12, 1)))
            l_vals.append(np.exp(entropy))
            # t-closeness: 0.5 * Σ|P_gruppe(v) − P_global(v)| over kategoriene
            aligned = sub_probs.reindex(global_probs.index, fill_value=0.0)
            t_vals.append(0.5 * float(np.abs(aligned.values - global_probs.values).sum()))
        if l_vals:
            l_min = float(min(l_vals))
            l_median = float(np.median(l_vals))
        if t_vals:
            t_max = float(max(t_vals))

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

            key_discarded_note = None
            if verb_name in _verb_registry:
                fn = _verb_registry[verb_name]
                if verb_name == "pseudonymize":
                    result = fn(out, col, **params)
                    if isinstance(result, tuple):
                        out, _key = result
                        # `protect()` only returns the transformed data +
                        # TransformLog, not per-verb keys — so the
                        # pseudonymization mapping `_key` is discarded here.
                        # For method='random' (the default) that mapping is
                        # the ONLY way back to the original values; once
                        # it's gone the pseudonymization is irreversible.
                        # Silently dropping it would let a caller believe
                        # they could still re-identify later. Say so loudly
                        # in the log instead of changing what protect()
                        # returns (audit trail, not a new return shape).
                        pseudo_method = params.get("method", "random")
                        key_discarded_note = (
                            f"pseudonymize key for column {col!r} "
                            f"(method={pseudo_method!r}) was discarded by "
                            f"protect() and is NOT recoverable from this "
                            f"call" +
                            (" — method='random' has no other way to "
                             "regenerate it; call pseudonymize() directly "
                             "and keep return_key=True's key if you need "
                             "to reverse this later."
                             if pseudo_method == "random" else ".")
                        )
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
                notes=key_discarded_note,
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
    zip_population: dict | None = None,
    zip_min_count: int = 20_000,
    random_state: int | None = None,
):
    """HIPAA Safe Harbor (Sec 164.514(b)(2)).

    ZIP handling
    ------------
    Safe Harbor's ZIP rule is about Census POPULATION: the first 3 digits
    of a ZIP code may be released as-is only if the 3-digit area's Census
    population is > 20,000 (per the Bureau's published Safe-Harbor ZCTA
    list); all others (and all ZIPs with population <= 20,000) are
    suppressed to '000'. That population figure is NOT something this
    function can know from your dataset alone — a dataset's own row/person
    count for a ZIP3 has nothing to do with how many people live there.

    - Pass `zip_population={'003': 25000, ...}` (a real ZIP3 -> Census
      population mapping, e.g. from the Bureau's Safe-Harbor list) to apply
      the actual population>=20,000 rule; the log will say so.
    - Leave `zip_population=None` (default) and this function instead
      falls back to a heuristic that suppresses ZIP3s whose SAMPLE count in
      THIS dataset is below `zip_min_count` (still named/defaulted 20,000,
      but now honestly documented as a heuristic). This is NOT by itself
      proof of Sec 164.514(b)(2) compliance — a ZIP3 can be common in your
      sample yet have a small Census population, or vice versa — the log
      entry says so explicitly rather than claiming "HIPAA".
    """
    out = data.copy()
    log = TransformLog()

    for col in id_cols:
        if col in out.columns:
            out, _key = pseudonymize(out, col, method="random", random_state=random_state)
            log.add(function="pseudonymize", columns=[col],
                    params={"method": "random"}, rows_affected=len(out),
                    notes="HIPAA Safe Harbor identifier removal; the "
                          "pseudonymization key was discarded here (not "
                          "returned by this profile) — method='random' "
                          "means this is irreversible. Call "
                          "pseudonymize() directly and keep its key if "
                          "you need to re-identify later.")

    for col in date_cols:
        if col in out.columns:
            out = year(out, col)
            log.add(function="year", columns=[col], params={},
                    rows_affected=len(out),
                    notes="HIPAA: year-only resolution")

    if zip_col and zip_col in out.columns:
        out = shorten(out, zip_col, keep=3)
        if zip_population is not None:
            below = [z for z in out[zip_col].unique()
                     if pd.notna(z) and zip_population.get(z, 0) < 20_000]
            out.loc[out[zip_col].isin(below), zip_col] = "***"
            log.add(function="shorten", columns=[zip_col],
                    params={"keep": 3, "zip_population_rule": True},
                    rows_affected=len(out),
                    notes="HIPAA Sec 164.514(b)(2): ZIP3 suppressed unless "
                          "its Census population (per the zip_population "
                          "mapping you supplied) is >= 20,000")
        else:
            zip3_counts = out[zip_col].value_counts()
            below = zip3_counts[zip3_counts < zip_min_count].index
            out.loc[out[zip_col].isin(below), zip_col] = "***"
            log.add(function="shorten", columns=[zip_col],
                    params={"keep": 3, "zip_min_count": zip_min_count},
                    rows_affected=len(out),
                    notes=f"HEURISTIC, NOT the HIPAA population rule: ZIP3 "
                          f"suppressed unless it has >= {zip_min_count} "
                          f"rows in THIS dataset. True Sec 164.514(b)(2) "
                          f"compliance requires Census population data — "
                          f"pass zip_population={{'003': <population>, ...}} "
                          f"to apply the actual rule.")

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
    max_iterations: int = 20,
):
    """Composed defaults for typical health-research release: collapse rare
    `sensitive_cols` categories, then — if `quasi_ids` is given — generalize
    them (via the same greedy k-anonymization `profile('k_anonymize')`
    uses) so the release actually reaches k-anonymity on those columns
    rather than accepting `quasi_ids` and silently doing nothing with it.
    """
    out = data.copy()
    log = TransformLog()

    for col in sensitive_cols:
        if col in out.columns:
            out = collapse(out, col, rare_below=k)
            log.add(function="collapse", columns=[col],
                    params={"rare_below": k}, rows_affected=len(out))

    if quasi_ids:
        out, sub_log = _profile_k_anonymize(
            out, quasi_ids=list(quasi_ids), k=k, unit_id=unit_id,
            max_iterations=max_iterations,
        )
        log.entries.extend(sub_log.entries)

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
    # Etter løkka: verifiser at målet faktisk er nådd. Tidligere kunne
    # funksjonen returnere data som IKKE var k-anonyme (iterasjonene tok slutt,
    # eller ingen kolonne lot seg kollapse) med en ren logg — verste utfall for
    # et personvern-verktøy.
    final = risk(out, quasi_ids=list(quasi_ids), unit_id=unit_id)
    if final.k_min < k:
        log.add(function="_k_anonymize_FAILED",
                params={"k": k, "max_iterations": max_iterations},
                rows_affected=len(out),
                notes=_t("k_min={k_min} < mål k={k}", k_min=final.k_min, k=k))
        raise ValueError(_t(
            "k-anonymisering nådde ikke mål k={k}: minste gruppe har "
            "k_min={k_min} etter {max_iterations} iterasjoner. Øk "
            "max_iterations, reduser k, eller generaliser/fjern quasi-"
            "identifikatorer.",
            k=k, k_min=final.k_min, max_iterations=max_iterations,
        ))
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
