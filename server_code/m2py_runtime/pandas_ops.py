# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the m2py repo. This file is produced by sync_to_api.py.
# Edit the engine in the m2py repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""Pure pandas runtime ops.

Each op takes a ``pd.DataFrame`` plus parsed-IR arguments and returns a *new*
frame; none mutate their input or log. Expression and condition evaluation is
delegated to the emulator's own helpers so behaviour matches the in-browser
engine bit-for-bit.

IR argument shapes (from ``m2py.MicroParser.parse_line``):
    generate/replace : args={'target', 'expression'}, condition=str|None
    keep/drop        : args={'mode', 'vars'}, condition=str|None
    recode           : args={'vars', 'rules': ['1=10', ...], 'prefix'}
    collapse/aggregate: args={'targets': [{'stat','src','target'}]}, options={'by'}
    merge            : args=[name, 'on', key]  (resolved by the caller)
    summarize        : args=[var, ...], options={'by'}
"""

import threading

import numpy as np
import pandas as pd

from m2py import _py_eval_expr, _py_eval_cond, AGG_STAT_ALIAS
from .sources import read_source  # noqa: F401  (used by generated code: ops.read_source)


# ── release spec (compute-to-data) ───────────────────────────────────────────
# m2py_remote sets the active post-suppression spec ({min_n, round, …} or None)
# around each server run; value-releasing ops consult it so chart values and
# normalized tables can't bypass the table suppression. Thread-local: concurrent
# runs in other threads never see each other's spec; local/Pyodide runs never
# set it, so behaviour there is unchanged.

_release_ctx = threading.local()


def set_release_spec(spec):
    _release_ctx.spec = spec or None


def get_release_spec():
    return getattr(_release_ctx, "spec", None)


def _released_counts(counts, drop=True):
    """Counts (Series) -> suppressed (< min_n masked) + rounded per the active
    release spec; unchanged when no spec is active. drop=True removes masked
    entries (charts drop the category instead of plotting a gap)."""
    spec = get_release_spec()
    if not spec:
        return counts
    import protect as p
    out = p.suppress(counts, min_n=spec.get("min_n", 5), round=spec.get("round"))
    return out.dropna() if drop else out


def _released_values(values, counts):
    """Aggregate values (Series) masked where their group count < min_n."""
    spec = get_release_spec()
    if not spec:
        return values
    import protect as p
    return p.suppress(values, counts=counts, min_n=spec.get("min_n", 5))


def _mask_thin_terms(out, design, stat_cols=("coef", "se", "t", "p")):
    """Coefficient-table release rule: mask the stats of every term whose
    design-matrix column has fewer than min_n nonzero entries (a thin dummy's
    coefficient is close to a small group's mean). ``design`` is a DataFrame
    whose column names match ``out['term']``; None ⇒ fail closed (all masked).
    No-op without an active release spec."""
    spec = get_release_spec()
    if not spec:
        return out
    cols = [c for c in stat_cols if c in out.columns]
    if design is None:
        out[cols] = np.nan
        return out
    at_risk = {str(c): int((pd.Series(design[c]).to_numpy() != 0).sum())
               for c in design.columns}
    # terms without a design column (e.g. negbin's alpha) are model-level
    # parameters over all rows — treat as full-n, not thin
    thin = out["term"].astype(str).map(at_risk).fillna(len(design)) < spec.get("min_n", 5)
    out.loc[thin, cols] = np.nan
    return out


def _released_extremes(s, spec):
    """Order-stat rule for a numeric Series: its min/max may be released only
    when at least min_n observations sit AT the extreme value (otherwise the
    extreme is one identifiable unit's value). Returns (min, max) with np.nan
    where refused."""
    min_n = spec.get("min_n", 5)
    lo, hi = s.min(), s.max()
    lo_ok = (s == lo).sum() >= min_n
    hi_ok = (s == hi).sum() >= min_n
    return (lo if lo_ok else np.nan), (hi if hi_ok else np.nan)


def _coarsen_sig(v, sig):
    """Round to ``sig`` significant digits (Tiltak 8 percentile coarsening)."""
    if v is None or (isinstance(v, float) and not np.isfinite(v)) or v == 0:
        return v
    from math import floor, log10
    return round(v, -int(floor(log10(abs(v)))) + (sig - 1))


# ── value-producing verbs ────────────────────────────────────────────────────

def _normalize_expr(expression):
    """The emulator's generate preprocessing: join line-continuation newlines,
    apply the Stata-like &/| precedence fixup, and rewrite ``N if cond`` to
    ``np.where``. Keeps generate/replace expression semantics identical."""
    from m2py import _stata_like_bool_fixup
    import re
    expr = expression
    if isinstance(expr, str):
        if "\n" in expr:
            expr = " ".join(expr.splitlines())
        if "&" in expr or "|" in expr:
            expr = _stata_like_bool_fixup(expr)
        m = re.match(r"^(\d+)\s+if\s+(.+)$", expr.strip())
        if m:
            expr = f"np.where({m.group(2)}, {int(m.group(1))}, np.nan)"
    return expr


def _assign(df, target, expression, cond):
    out = df.copy()
    values = _py_eval_expr(out, _normalize_expr(expression))
    if cond:
        mask = _py_eval_cond(out, cond)
        if target in out.columns:
            out.loc[mask, target] = values[mask]
        else:
            col = pd.Series(np.nan, index=out.index)
            col[mask] = values[mask]
            out[target] = col
    else:
        out[target] = values
    return out


def generate(df, target, expression, cond=None):
    """Add (or, with a condition on an existing column, partially set) a column."""
    return _assign(df, target, expression, cond)


def replace(df, target, expression, cond=None):
    """Overwrite a column's values (only where ``cond`` holds, if given)."""
    return _assign(df, target, expression, cond)


def _parse_recode_rule(rule):
    """'1=10' -> (1.0, 10.0). Numeric where possible, else string."""
    lhs, rhs = rule.split("=", 1)
    return _coerce(lhs.strip()), _coerce(rhs.strip())


def _coerce(tok):
    tok = tok.strip().strip("'\"")
    try:
        f = float(tok)
        return int(f) if f.is_integer() else f
    except ValueError:
        return tok


def recode(df, vars, rules, prefix=None):
    """Map values per microdata recode rules. Delegates to the emulator's own
    DataTransformHandler so the full rule grammar — multi-value (``1 2 = 1``),
    ranges (``000000/099999 = 1``), ``min``/``max``, ``missing``/``nonmissing``/
    ``*``, labels, first-match-wins — matches the emulator exactly."""
    import m2py
    out = df.copy()
    res = m2py.DataTransformHandler(label_manager=None).execute(
        "recode", out, {"vars": vars, "rules": rules, "prefix": prefix}, {})
    return res if res is not None else out


# ── row/column shaping ───────────────────────────────────────────────────────

def keep(df, vars=None, cond=None):
    out = df
    if vars:
        out = out[[c for c in vars if c in out.columns]]
    if cond:
        mask = _py_eval_cond(out, cond)
        out = out.loc[mask]
    return out.reset_index(drop=True).copy()


def drop(df, vars=None, cond=None):
    out = df
    if vars:
        out = out.drop(columns=[c for c in vars if c in out.columns])
    if cond:
        mask = _py_eval_cond(out, cond)
        out = out.loc[~mask]
    return out.reset_index(drop=True).copy()


# ── aggregation / reshaping ──────────────────────────────────────────────────

def collapse(df, targets, by=None):
    """Replace the frame with one aggregated row per ``by`` group (or one row
    overall when ``by`` is None)."""
    if isinstance(by, str) and by.strip():
        by = by.strip().split()[0]
    agg_dict = {}
    for t in targets:
        stat_fn = AGG_STAT_ALIAS.get(t["stat"], t["stat"])
        target_col = t["target"] or t["src"]
        agg_dict[target_col] = (t["src"], stat_fn)
    if not by:
        row = {}
        for name, (src, fn) in agg_dict.items():
            s = df[src]
            row[name] = fn(s) if callable(fn) else s.agg(fn)
        return pd.DataFrame([row])
    return df.groupby(by, dropna=False).agg(**agg_dict).reset_index()


def aggregate(df, targets, by=None):
    """Add group-wise aggregate columns without collapsing rows (groupby
    transform)."""
    out = df.copy()
    for t in targets:
        stat_fn = AGG_STAT_ALIAS.get(t["stat"], t["stat"])
        new_var = t["target"] or t["src"]
        out[new_var] = out.groupby(by)[t["src"]].transform(stat_fn)
    return out


def emulate_import(name):
    """Synthesize a base population for dataset ``name`` via the emulator.

    Used only on the opt-in ``allow_emulated`` path, when a script needs an input
    the caller did not provide — so the generated script still runs end-to-end
    (e.g. for testing on Anvil before real data is wired in). Returns a generic
    person population keyed by ``PERSONID_1``; it is mock data, not the caller's
    real dataset.
    """
    import m2py
    it = m2py.MicroInterpreter()
    it.run_script(f"create-dataset {name}\nimport INNTEKT/WLONN as _emulated")
    df = it.datasets.get(name)
    return df if df is not None else pd.DataFrame()


def merge(df, other, on, how="left"):
    """Left-join ``other`` onto ``df`` by key ``on`` (adds the right frame's
    non-key columns)."""
    return pd.merge(df, other, on=on, how=how)


def merge_into(target, source, vars, left_on, right_on):
    """Into-form merge ``merge vars into TARGET``: bring ``vars`` from ``source``
    onto ``target`` (left=target, right=source deduped on its key), always a
    left-join. Mirrors the emulator's column handling exactly, including the
    asymmetric-key suffix/drop behaviour. Accepts scalar or list keys.
    """
    lon = left_on if isinstance(left_on, list) else [left_on]
    ron = right_on if isinstance(right_on, list) else [right_on]
    cols_from_source = [c for c in (vars or []) if c in source.columns]
    right_cols = list(dict.fromkeys(list(ron) + cols_from_source))
    right = source[right_cols].drop_duplicates(subset=ron)
    if lon == ron:
        return pd.merge(target, right, on=lon, how="left")
    merged = pd.merge(target, right, left_on=lon, right_on=ron,
                      how="left", suffixes=("", "_src_dup"))
    merged = merged.drop(columns=[c for c in merged.columns if c.endswith("_src_dup")])
    drop = [c for c in ron if c not in lon and c not in target.columns and c in merged.columns]
    return merged.drop(columns=drop) if drop else merged


def reshape_to_panel(df, prefixes):
    """Wide -> long panel. For each prefix, collect columns ``<prefix><suffix>``
    where the suffix is non-alphabetic (years/dates) and stack them into one
    column ``<prefix>`` with a ``tid`` (and ``panel@date``) time column; rows are
    entity×time (entity-major). Mirrors the emulator exactly."""
    from m2py import _get_df_key_col
    if not prefixes:
        raise ValueError("reshape-to-panel requires at least one variable prefix")
    id_col = _get_df_key_col(df) or df.index.name or "id"
    id_col = id_col if id_col in df.columns else df.columns[0]
    stub_cols, time_vals = {}, set()
    for col in df.columns:
        for pre in prefixes:
            if col.startswith(pre) and col != pre:
                suf = col[len(pre):]
                if suf and all(not c.isalpha() for c in suf):
                    stub_cols.setdefault(pre, []).append((col, suf))
                    time_vals.add(suf)
    if not stub_cols:
        raise ValueError(
            f"reshape-to-panel found no <prefix><suffix> columns for {prefixes}")
    time_vals = sorted(time_vals)
    n, n_t = len(df), len(time_vals)
    stub_set = {full for cols in stub_cols.values() for full, _ in cols}
    rep_idx = np.repeat(np.arange(n), n_t)
    out = pd.DataFrame(index=pd.RangeIndex(n * n_t))
    out[id_col] = (df[id_col].to_numpy()[rep_idx] if id_col in df.columns
                   else np.repeat(df.index.to_numpy(), n_t))
    tid_block = np.tile(np.asarray(time_vals, dtype=object), n)
    out["tid"] = tid_block
    out["panel@date"] = tid_block
    for pre, cols in stub_cols.items():
        suf_to_col = {suf: full for full, suf in cols}
        per_t = [df[suf_to_col[t]].reset_index(drop=True)
                 if t in suf_to_col else pd.Series(np.nan, index=range(n))
                 for t in time_vals]
        out[pre] = pd.concat(per_t, axis=1).to_numpy().ravel(order="C")
    for c in df.columns:
        if c not in stub_set and c != id_col:
            out[c] = df[c].to_numpy()[rep_idx]
    return out


def reshape_from_panel(df):
    """Long -> wide. Pivot each non-id column over ``tid`` into ``<var><tid>``
    columns (one row per entity). Mirrors the emulator."""
    from m2py import _get_df_key_col
    if "tid" not in df.columns:
        raise ValueError("reshape-from-panel requires a 'tid' column")
    id_col = _get_df_key_col(df) or df.columns[0]
    wide = df.pivot_table(index=id_col, columns="tid", aggfunc="first")
    wide.columns = [f"{a}{b}" if b != "" else str(a) for a, b in wide.columns]
    return wide.reset_index()


def rename(df, old, new):
    """Rename column ``old`` to ``new``."""
    return df.rename(columns={old: new})


def clone_variables(df, pairs, prefix="", suffix=""):
    """Copy columns: each ``(old, new)`` adds a copy of ``old`` named ``new``
    (default ``<old>_clone``), or ``<prefix><old><suffix>`` when given."""
    out = df.copy()
    for old, new in pairs:
        if old in out.columns:
            actual = f"{prefix}{old}{suffix}" if (prefix or suffix) else new
            out[actual] = out[old]
    return out


def clone_units(df):
    """New one-column dataset of the (deduplicated) entity key — like the
    emulator's clone-units."""
    from m2py import _get_df_key_col
    key = _get_df_key_col(df) or "unit_id"
    return df[[key]].drop_duplicates().reset_index(drop=True)


def destring(df, vars):
    """Coerce string columns to numeric (non-parseable values -> NaN)."""
    out = df.copy()
    for v in vars:
        if v in out.columns:
            out[v] = pd.to_numeric(out[v], errors="coerce")
    return out


# ── analysis ─────────────────────────────────────────────────────────────────

def _numeric_vars(df, vars):
    if not vars:
        vars = [c for c in df.columns if c not in ("unit_id", "PERSONID_1")]
    return [v for v in vars if v in df.columns and pd.api.types.is_numeric_dtype(df[v])]


# Percentiles the emulator reports for an ungrouped summarize (incl. median).
_SUM_PCTLS = [("p1", 0.01), ("p25", 0.25), ("p50", 0.5), ("p75", 0.75), ("p99", 0.99)]


def _extra_stat_cols(s, gini, iqr):
    cols = {}
    if gini:
        cols["gini"] = AGG_STAT_ALIAS["gini"](s)
    if iqr:
        cols["iqr"] = AGG_STAT_ALIAS["iqr"](s)
    return cols


def summarize(df, vars=None, by=None, gini=False, iqr=False):
    """Descriptive statistics for numeric ``vars`` as a tidy long frame, matching
    the emulator's two paths (verified against ``StatsEngine``):

      - ungrouped: ``[variable, mean, std, count, p1, p25, p50, p75, p99]``
        (percentiles incl. the median; no min/max — same as the emulator)
      - grouped (``by``): ``[<by>, variable, mean, std, min, max, count]``
        (no percentiles — same as the emulator)

    ``gini``/``iqr`` (reusing the emulator's ``calculate_gini``/``calculate_iqr``)
    append columns in either path. Analysis result; the dataset is unchanged."""
    vars = _numeric_vars(df, vars)

    spec = get_release_spec()
    if by and by in df.columns:
        recs = []
        for key, sub in df.groupby(by, dropna=False):
            for v in vars:
                s = sub[v]
                if spec:      # min/max are individual values: order-stat rule
                    lo, hi = _released_extremes(s.dropna(), spec)
                else:
                    lo, hi = s.min(), s.max()
                r = {by: key, "variable": v, "mean": s.mean(), "std": s.std(),
                     "min": lo, "max": hi, "count": s.count()}
                r.update(_extra_stat_cols(s, gini, iqr))
                recs.append(r)
        return pd.DataFrame(recs)

    sig = spec.get("percentile_sig_figs") if spec else None
    recs = []
    for v in vars:
        s = df[v]
        r = {"variable": v, "mean": s.mean(), "std": s.std(), "count": s.count()}
        # Tiltak 8: percentiles coarsened to sig figs (p1/p99 sit next to an
        # identifiable unit's value at full precision)
        r.update({label: (_coarsen_sig(s.quantile(q), sig) if sig else s.quantile(q))
                  for label, q in _SUM_PCTLS})
        r.update(_extra_stat_cols(s, gini, iqr))
        recs.append(r)
    return pd.DataFrame(recs)


def _chi2_stats(sub, v1, v2, dropna):
    """(chi2, p, dof) for the v1×v2 contingency of ``sub``; NaN if degenerate."""
    from scipy.stats import chi2_contingency
    ct = pd.crosstab(sub[v1], sub[v2], dropna=dropna)
    if ct.shape[0] < 2 or ct.shape[1] < 2:
        return (np.nan, np.nan, np.nan)
    chi2, p, dof, _ = chi2_contingency(ct)
    return (float(chi2), float(p), float(dof))


def normaltest(df, vars=None):
    """Normality diagnostics per numeric variable (skewness, kurtosis,
    D'Agostino-Pearson, Jarque-Bera, Shapiro-Wilk for n<=5000). Returns a long
    frame ``[variable, test, statistic, p]``."""
    from scipy import stats as st
    vars = _numeric_vars(df, vars)
    rows = []
    for v in vars:
        s = df[v].dropna()
        if len(s) < 3:
            rows.append({"variable": v, "test": "-", "statistic": np.nan, "p": np.nan})
            continue
        rows.append({"variable": v, "test": "skewness", "statistic": st.skew(s), "p": np.nan})
        rows.append({"variable": v, "test": "kurtosis", "statistic": st.kurtosis(s), "p": np.nan})
        nt = st.normaltest(s)
        rows.append({"variable": v, "test": "normaltest (s-k)", "statistic": nt[0], "p": nt[1]})
        jb = st.jarque_bera(s)
        rows.append({"variable": v, "test": "Jarque-Bera", "statistic": jb[0], "p": jb[1]})
        sw = st.shapiro(s) if len(s) <= 5000 else (np.nan, np.nan)
        rows.append({"variable": v, "test": "Shapiro-Wilk", "statistic": sw[0], "p": sw[1]})
    return pd.DataFrame(rows)


def ci(df, vars=None, level=95):
    """Confidence interval for the mean of each numeric variable (Student-t).
    Returns ``[variable, count, mean, se, ci_low, ci_high]`` — the count column
    both informs and lets the release layer suppress small-n rows."""
    from scipy import stats as st
    vars = _numeric_vars(df, vars)
    lv = float(level) / 100 if level else 0.95
    rows = []
    for v in vars:
        s = df[v].dropna()
        n, mean = len(s), df[v].dropna().mean()
        if n < 2:
            rows.append({"variable": v, "count": n, "mean": mean, "se": np.nan,
                         "ci_low": np.nan, "ci_high": np.nan})
            continue
        sem = st.sem(s)
        t = st.t.ppf((1 + lv) / 2, n - 1)
        rows.append({"variable": v, "count": n, "mean": mean, "se": sem,
                     "ci_low": mean - t * sem, "ci_high": mean + t * sem})
    return pd.DataFrame(rows)


def anova(df, dep, factors):
    """Type-II ANOVA of ``dep`` on the categorical ``factors`` (statsmodels OLS +
    ``anova_lm``). ``#`` in a factor denotes an interaction. Returns the ANOVA
    table ``[term, sum_sq, df, F, PR(>F)]``."""
    from statsmodels.formula.api import ols
    from statsmodels.stats.anova import anova_lm
    terms = [f"C({f})" for f in factors if "#" not in f and f in df.columns]
    for a in factors:
        if "#" in a:
            parts = a.replace("##", "#").split("#")
            terms.append(":".join(f"C({p.strip()})" for p in parts if p.strip() in df.columns))
    model = ols(f"{dep} ~ " + " + ".join(terms), data=df).fit()
    return anova_lm(model, typ=2).reset_index(names="term")


def hausman(df, dep, indep, key=None):
    """Hausman FE-vs-RE test (linearmodels). Needs a ``tid`` column + entity key.
    Returns ``[statistic, df, p]`` (the chi-square test of coefficient
    differences; small p favours fixed effects)."""
    import statsmodels.api as sm
    from linearmodels.panel import PanelOLS, RandomEffects
    from scipy.stats import chi2 as chi2_dist
    from m2py import _get_df_key_col
    key = key or _get_df_key_col(df) or "unit_id"
    if "tid" not in df.columns:
        raise ValueError("hausman requires a 'tid' column")
    d = df[[dep] + list(indep) + [key, "tid"]].copy()
    for v in [dep] + list(indep):
        d[v] = pd.to_numeric(d[v], errors="coerce")
    d = d.dropna()
    pidx = d.set_index([key, "tid"])
    Y = pidx[dep]
    X = sm.add_constant(pidx[list(indep)], has_constant="add")
    fe = PanelOLS(Y, X, entity_effects=True, drop_absorbed=True).fit()
    re = RandomEffects(Y, X).fit()
    common = fe.params.index.intersection(re.params.index)
    diff = (fe.params.loc[common] - re.params.loc[common]).to_numpy()
    vdiff = fe.cov.loc[common, common].to_numpy() - re.cov.loc[common, common].to_numpy()
    chi2 = float(diff @ np.linalg.solve(vdiff, diff))
    return pd.DataFrame([{"statistic": chi2, "df": len(common),
                          "p": float(1 - chi2_dist.cdf(chi2, len(common)))}])


def summarize_panel(df, vars=None, gini=False, iqr=False):
    """``summarize`` per time period: mean/std/min/max/count of each numeric
    variable within each ``tid`` (+ gini/iqr). Returns a tidy frame
    ``[tid, variable, mean, std, min, max, count, ...]``."""
    if "tid" not in df.columns:
        raise ValueError("summarize-panel requires a 'tid' column")
    vars = [v for v in _numeric_vars(df, vars) if v != "tid"]
    spec = get_release_spec()
    recs = []
    for tid_val, sub in df.groupby("tid"):
        for v in vars:
            s = sub[v]
            if spec:          # min/max are individual values: order-stat rule
                lo, hi = _released_extremes(s.dropna(), spec)
            else:
                lo, hi = s.min(), s.max()
            r = {"tid": tid_val, "variable": v, "mean": s.mean(), "std": s.std(),
                 "min": lo, "max": hi, "count": s.count()}
            r.update(_extra_stat_cols(s, gini, iqr))
            recs.append(r)
    return pd.DataFrame(recs)


def transitions_panel(df, vars=None):
    """Transition matrix per variable: within each entity (sorted by ``tid``), the
    row-normalised probability of moving from each value to the next period's
    value. Returns a long frame ``[variable, from, to, prob]``. Mirrors the
    emulator's crosstab(current, next, normalize='index')."""
    from m2py import _get_df_key_col
    key = _get_df_key_col(df) or "unit_id"
    if "tid" not in df.columns:
        raise ValueError("transitions-panel requires a 'tid' column")
    if not vars:
        vars = [c for c in df.columns if c not in ("unit_id", "PERSONID_1", "tid", key)]
    vars = [v for v in vars if v in df.columns]
    frames = []
    for var in vars:
        s = df[[key, "tid", var]].sort_values([key, "tid"]).dropna(subset=[var])
        s = s.assign(_next=s.groupby(key)[var].shift(-1)).dropna(subset=["_next"])
        if s.empty:
            continue
        ct = pd.crosstab(s[var], s["_next"], normalize="index")
        spec = get_release_spec()
        if spec:
            # probabilities of transitions with < min_n movers are masked —
            # a normalized cell must not reveal what the count table suppresses
            counts = pd.crosstab(s[var], s["_next"])
            ct = ct.where(counts >= spec.get("min_n", 5))
        long = ct.reset_index().melt(id_vars=var, var_name="to", value_name="prob")
        long = long.rename(columns={var: "from"})
        long.insert(0, "variable", var)
        frames.append(long)
    return (pd.concat(frames, ignore_index=True) if frames
            else pd.DataFrame(columns=["variable", "from", "to", "prob"]))


def tabulate_panel(df, var1, missing=False, rowpct=False, colpct=False):
    """Frequency of ``var1`` across time periods (``var1`` rows × ``tid``).
    Counts of each ``(var1, tid)``; rowpct is within ``var1``, colpct within
    ``tid`` — same as a two-way ``tabulate var1 tid``."""
    if "tid" not in df.columns:
        raise ValueError("tabulate-panel requires a 'tid' column")
    return tabulate(df, [var1, "tid"], missing=missing, rowpct=rowpct, colpct=colpct)


def tabulate(df, vars, by=None, missing=False,
             cellpct=False, rowpct=False, colpct=False,
             chi2=False, top=None, bottom=None):
    """Frequency table: counts of each combination of ``vars`` (one-way for a
    single variable, cross-tab for two), optionally within ``by`` groups.

    Missing/null key values are dropped by default and kept when ``missing`` is
    set (consistent for one-way and two-way, matching the corrected emulator).
    Percentage columns (0-100), within the ``by`` group when given:
      - ``cellpct``: share of the whole table
      - ``rowpct``:  share within the first variable (``vars[0]``)
      - ``colpct``:  share within the second variable (``vars[1]``, or the only
        variable for a one-way table)
    ``chi2`` (two-way only) adds constant ``chi2``/``chi2_p``/``chi2_dof`` columns
    (per ``by`` group), using scipy's chi-square test of independence — computed
    on the full table, before any top/bottom row limit.
    ``top``/``bottom`` keep the first/last n categories of the first variable
    (positional, in value-sorted order — same as microdata/the emulator, which
    head/tail the table rows; ``top(n)``; bare ``top`` -> 10). Columns: the
    grouping variables, ``n``, then any extras."""
    keys = ([by] if by and by in df.columns else []) + list(vars)
    out = df.groupby(keys, dropna=not missing).size().reset_index(name="n")
    grp = [by] if by and by in df.columns else []
    first = vars[0]
    second = vars[1] if len(vars) > 1 else vars[0]
    if cellpct:
        denom = out.groupby(grp)["n"].transform("sum") if grp else out["n"].sum()
        out["cellpct"] = 100.0 * out["n"] / denom
    if rowpct:
        out["rowpct"] = 100.0 * out["n"] / out.groupby(grp + [first])["n"].transform("sum")
    if colpct:
        out["colpct"] = 100.0 * out["n"] / out.groupby(grp + [second])["n"].transform("sum")
    if chi2 and len(vars) >= 2:
        if grp:
            stats = {k: _chi2_stats(sub, first, second, not missing)
                     for k, sub in df.groupby(by)}
            for i, col in enumerate(("chi2", "chi2_p", "chi2_dof")):
                out[col] = out[by].map(lambda k, i=i: stats.get(k, (np.nan,) * 3)[i])
        else:
            out["chi2"], out["chi2_p"], out["chi2_dof"] = _chi2_stats(
                df, first, second, not missing)
    if top is not None or bottom is not None:
        from m2py import _parse_count_option
        # positional (emulator/microdata): first/last n categories of the first
        # variable, in value-sorted order (groupby already sorts keys ascending).
        cats = out[first].drop_duplicates()
        n = _parse_count_option(top if top is not None else bottom)
        keep = cats.head(n) if top is not None else cats.tail(n)
        out = out[out[first].isin(keep)].reset_index(drop=True)
    return out


def correlate(df, vars, pairwise=False, covariance=False):
    """Pearson correlation matrix for numeric ``vars`` as a frame whose first
    column ``variable`` labels each row. Matching the emulator: by default rows
    with any missing value are dropped (listwise); ``pairwise`` keeps them and
    correlates pairwise; ``covariance`` returns the covariance matrix instead."""
    vars = _numeric_vars(df, vars)
    sub = df[vars]
    if not pairwise:
        sub = sub.dropna()
    m = sub.cov() if covariance else sub.corr(method="pearson")
    spec = get_release_spec()
    if spec:
        # mask correlations computed from fewer than min_n (pairwise) rows
        notna = sub.notna().astype(int)
        pair_n = notna.T @ notna
        m = m.where(pair_n >= spec.get("min_n", 5))
    return m.reset_index(names="variable")


# ── plots (terminal; return a plotly Figure) ─────────────────────────────────
# plotly is imported lazily so this module stays importable without it (and
# under Pyodide). Figures are built with plotly express where it matches the
# emulator's trace data, mirroring m2py.PlotHandler so the offline charts equal
# the in-browser ones (verified by comparing trace x/y in the tests).

_BAR_AGG = {"mean": "mean", "median": "median", "sum": "sum",
            "sd": "std", "min": "min", "max": "max"}


def histogram(df, vars, bins=30, discrete=False, percent=False, density=False,
              normal=False):
    """Histogram of ``vars[0]``. Numeric -> ``go.Histogram`` (``histnorm`` for
    percent/density); categorical or ``discrete`` -> value-counts bar (as percent
    when requested). ``normal`` overlays a fitted normal curve (numeric only),
    scaled to the histogram's y units. Mirrors the emulator."""
    import plotly.express as px
    import plotly.graph_objects as go
    var = vars[0]
    s = df[var].dropna()
    if discrete or not pd.api.types.is_numeric_dtype(s):
        vc = _released_counts(s.value_counts().sort_index())
        if percent:
            vc = (vc / vc.sum() * 100).round(2)
        return px.bar(x=vc.index.tolist(), y=vc.values.tolist())
    spec = get_release_spec()
    if spec:
        # compute-to-data: px.histogram embeds the raw column in the figure
        # JSON, so pre-bin server-side and release suppressed bin counts only
        import numpy as np  # rebind: the `normal` branch below shadows the global
        counts, edges = np.histogram(s, bins=bins)
        centers = ((edges[:-1] + edges[1:]) / 2).tolist()
        bc = _released_counts(pd.Series(counts, index=centers))
        if percent:
            bc = (bc / bc.sum() * 100).round(2)
        elif density:
            width = float(edges[1] - edges[0]) or 1.0
            bc = bc / (bc.sum() * width)
        return go.Figure(data=[go.Bar(x=bc.index.tolist(), y=bc.values.tolist())])
    histnorm = "probability density" if density else ("percent" if percent else None)
    fig = px.histogram(df.dropna(subset=[var]), x=var, nbins=bins, histnorm=histnorm)
    if normal:
        import numpy as np
        from scipy.stats import norm
        mu, sigma = float(s.mean()), float(s.std())
        x_range = np.linspace(float(s.min()), float(s.max()), 200)
        y_pdf = norm.pdf(x_range, mu, sigma)
        if density:
            y_curve = y_pdf
        else:
            bin_width = (float(s.max()) - float(s.min())) / bins
            y_curve = y_pdf * bin_width * (100 if percent else len(s))
        fig.add_trace(go.Scatter(
            x=x_range.tolist(), y=y_curve.tolist(), mode="lines",
            line=dict(color="red", width=2),
            name=f"Normal(μ={mu:.1f}, σ={sigma:.1f})"))
    return fig


def barchart(df, vars, stat="count", over=None, horizontal=False, stack=False):
    """Bar chart of the listed variable(s). The statistic comes from the
    parenthesised ``(stat)`` form: count/percent -> value counts; a numeric stat
    (mean/median/sum/sd/min/max) -> that statistic. One trace per category grouped
    over ``over`` (``stack`` vs grouped); one bar per variable when several are
    listed; ``horizontal`` swaps the axes. Mirrors the emulator's traces."""
    import plotly.graph_objects as go
    vars_list = [v for v in vars if v in df.columns]
    over_var = over if over and over in df.columns else None
    orient = "h" if horizontal else "v"
    barmode = "stack" if stack else "group"

    if stat in ("count", "percent"):
        as_pct = stat == "percent"
        if len(vars_list) > 1:                       # one bar per variable
            counts = _released_counts(pd.Series(
                [int(df[v].count()) for v in vars_list], index=vars_list))
            if as_pct:
                total = counts.sum() or 1
                counts = (counts / total * 100).round(1)
            x, y = counts.index.tolist(), counts.values.tolist()
            return go.Figure(data=[go.Bar(
                x=x if not horizontal else y, y=y if not horizontal else x,
                orientation=orient)])
        var = vars_list[0]
        if over_var:                                 # one trace per category
            ct = pd.crosstab(df[over_var], df[var], dropna=False)
            ct = _released_counts(ct, drop=False)    # small cells -> gaps
            if as_pct:
                ct = ct.div(ct.sum(axis=1), axis=0).multiply(100).round(1)
            fig = go.Figure()
            for col in ct.columns:
                fig.add_trace(go.Bar(name=str(col), x=ct.index.tolist(), y=ct[col].values.tolist()))
            fig.update_layout(barmode=barmode)
            return fig
        s = _released_counts(df[var].value_counts(dropna=False).sort_index())
        if as_pct:
            s = (s / s.sum() * 100).round(1)
        labels, vals = s.index.tolist(), s.values.tolist()
        x, y = (labels, vals) if not horizontal else (vals, labels)
        return go.Figure(data=[go.Bar(x=x, y=y, orientation=orient)])

    agg = _BAR_AGG.get(stat, "mean")
    if len(vars_list) > 1:
        if over_var:                                 # one trace per variable, grouped
            fig = go.Figure()
            for v in vars_list:
                grp = df.groupby(over_var, dropna=False)[v].agg(agg)
                grp = _released_values(grp, df.groupby(over_var, dropna=False)[v].count())
                fig.add_trace(go.Bar(name=v, x=grp.index.tolist(), y=grp.values.tolist()))
            fig.update_layout(barmode=barmode)
            return fig
        vals = _released_values(
            pd.Series([df[v].agg(agg) for v in vars_list], index=vars_list),
            pd.Series([int(df[v].count()) for v in vars_list], index=vars_list))
        return go.Figure(data=[go.Bar(
            x=vars_list, y=vals.values.tolist(), orientation=orient)])
    var = vars_list[0]
    if over_var:
        grp = df.groupby(over_var, dropna=False)[var].agg(agg)
        grp = _released_values(grp, df.groupby(over_var, dropna=False)[var].count())
        x, y = grp.index.tolist(), grp.values.tolist()
    else:
        vals = _released_values(pd.Series([df[var].agg(agg)], index=[var]),
                                pd.Series([int(df[var].count())], index=[var]))
        x, y = [var], vals.values.tolist()
    return go.Figure(data=[go.Bar(x=x, y=y, orientation=orient)])


def scatter(df, vars, by=None):
    """Scatter of ``vars[0]`` (x) vs ``vars[1]`` (y); one trace per ``by`` group
    (in first-seen order, matching the emulator) when given."""
    import plotly.express as px
    import plotly.graph_objects as go
    x, y = vars[0], vars[1]
    if by and by in df.columns:
        sub = df[[x, y, by]].dropna()
        fig = go.Figure()
        for val in sub[by].unique():
            m = sub[by] == val
            fig.add_trace(go.Scatter(x=sub.loc[m, x], y=sub.loc[m, y],
                                     mode="markers", name=str(val)))
        return fig
    sub = df[[x, y]].dropna()
    return px.scatter(sub, x=x, y=y)


def boxplot(df, vars, over=None):
    """Box plot of ``vars[0]`` (grouped by ``over`` when given), or one box per
    variable when several are listed. Mirrors the emulator."""
    import plotly.express as px
    import plotly.graph_objects as go
    if len(vars) > 1:
        fig = go.Figure()
        for v in vars:
            s = df[v].dropna()
            if not s.empty:
                fig.add_trace(go.Box(y=s, name=v))
        return fig
    var = vars[0]
    if over and over in df.columns:
        return px.box(df[[over, var]], x=over, y=var)
    return px.box(df[[var]], y=var)


def piechart(df, vars, stat="count"):
    """Pie chart of ``vars[0]`` value counts, or percents with the ``(percent)``
    statistic. Mirrors the emulator."""
    import plotly.graph_objects as go
    s = _released_counts(df[vars[0]].value_counts(dropna=False).sort_index())
    if stat == "percent":
        values = (s / s.sum() * 100).round(1).tolist()
    else:
        values = s.values.tolist()
    return go.Figure(data=[go.Pie(labels=s.index.tolist(), values=values, hole=0)])


def hexbin(df, vars, bins=30):
    """2-D density (hexbin-style) of ``vars[0]`` vs ``vars[1]`` via Histogram2d."""
    import plotly.graph_objects as go
    x, y = vars[0], vars[1]
    sub = df[[x, y]].dropna()
    return go.Figure(data=[go.Histogram2d(
        x=sub[x], y=sub[y], nbinsx=bins, nbinsy=bins,
        colorscale="Blues", showscale=True)])


def sankey(df, vars):
    """Sankey diagram of transitions across the listed categorical variables
    (one node per stage+value). Mirrors the emulator's node/link construction."""
    import plotly.graph_objects as go
    vars_list = [v for v in vars if v in df.columns]
    sub = df[vars_list].dropna(how="any")
    stages, stage_idx, offsets = [], [], [0]
    for va in vars_list:
        uniq = sub[va].dropna().unique().tolist()
        stages.append(uniq)
        stage_idx.append({v: offsets[-1] + j for j, v in enumerate(uniq)})
        offsets.append(offsets[-1] + len(uniq))
    labels = [str(v) for uniq in stages for v in uniq]
    src, tgt, val = [], [], []
    for i in range(len(vars_list) - 1):
        va, vb = vars_list[i], vars_list[i + 1]
        grp = sub.groupby([va, vb], dropna=False).size().reset_index(name="count")
        ia, ib = stage_idx[i], stage_idx[i + 1]
        for _, row in grp.iterrows():
            a, b = row[va], row[vb]
            if pd.isna(a) or pd.isna(b):
                continue
            s, t = ia.get(a), ib.get(b)
            if s is not None and t is not None:
                src.append(s)
                tgt.append(t)
                val.append(int(row["count"]))
    return go.Figure(data=[go.Sankey(
        node=dict(label=labels, pad=15, thickness=20),
        link=dict(source=src, target=tgt, value=val))])


# ── regression family (statsmodels, matching the emulator's model calls) ──────

def _fit_model(df, family, dep, indep, noconstant=False, standardize=False,
               return_design=False):
    """Fit one regression model the same way the emulator does (numeric coercion,
    listwise dropna, optional standardised predictors, intercept unless
    ``noconstant``). ``family`` in regress/logit/probit/poisson/negative-binomial.
    With ``return_design`` also returns ``(X, Y)`` and the kept-row index, for
    prediction."""
    import statsmodels.api as sm
    d = df[[dep] + list(indep)].apply(pd.to_numeric, errors="coerce").dropna().astype(float)
    X = d[list(indep)].copy()
    if standardize:
        for v in indep:
            sd = X[v].std()
            if sd > 0:
                X[v] = (X[v] - X[v].mean()) / sd
    if not noconstant:
        X = sm.add_constant(X, has_constant="add")
    Y = d[dep]
    if family == "regress":
        model = sm.OLS(Y, X).fit()
    elif family == "logit":
        model = sm.Logit(Y, X).fit(disp=0)
    elif family == "probit":
        model = sm.Probit(Y, X).fit(disp=0)
    elif family == "poisson":
        model = sm.GLM(Y, X, family=sm.families.Poisson()).fit()
    elif family == "negative-binomial":
        from statsmodels.discrete.discrete_model import NegativeBinomial
        model = NegativeBinomial(Y, X).fit(disp=0)
    else:
        raise ValueError(f"unknown regression family '{family}'")
    if return_design:
        return model, X, Y, d.index
    return model


def _predict(df, family, dep, indep, predicted="predicted", residuals=None,
             noconstant=False):
    """Add fitted values (and optionally response residuals) as new columns,
    aligned to the original rows (NaN where dropped). Returns a new frame."""
    model, X, Y, idx = _fit_model(df, family, dep, indep, noconstant, return_design=True)
    fitted = pd.Series(np.asarray(model.predict(X)), index=idx)
    out = df.copy()
    out[predicted or "predicted"] = fitted.reindex(df.index)
    if residuals:
        out[residuals] = (Y - fitted).reindex(df.index)
    return out


def regress_predict(df, dep, indep, predicted="predicted", residuals=None, noconstant=False):
    """OLS: add fitted values (and residuals)."""
    return _predict(df, "regress", dep, indep, predicted, residuals, noconstant)


def negative_binomial_predict(df, dep, indep, predicted="predicted", residuals=None,
                              noconstant=False):
    """Negative-binomial: add predicted counts (and Y-fitted residuals)."""
    return _predict(df, "negative-binomial", dep, indep, predicted, residuals, noconstant)


def _binary_predict(df, family, dep, indep, predicted=None, probabilities=None,
                    residuals=None, noconstant=False):
    """logit/probit predictions, matching the emulator: ``predicted`` is the
    LINEAR predictor (Xβ), ``probabilities`` is P(Y=1|X), ``residuals`` is the
    response residual; with no option, add ``predicted_prob`` (probabilities)."""
    model, X, Y, idx = _fit_model(df, family, dep, indep, noconstant, return_design=True)
    out = df.copy()
    probs = pd.Series(np.asarray(model.predict(X)), index=idx)
    added = False
    if probabilities:
        name = probabilities if probabilities is not True else "probabilities"
        out[name] = probs.reindex(df.index)
        added = True
    if predicted:
        name = predicted if predicted is not True else "predicted"
        out[name] = pd.Series(np.asarray(X @ model.params), index=idx).reindex(df.index)
        added = True
    if residuals:
        out[residuals] = pd.Series(np.asarray(model.resid_response), index=idx).reindex(df.index)
        added = True
    if not added:
        out["predicted_prob"] = probs.reindex(df.index)
    return out


def logit_predict(df, dep, indep, predicted=None, probabilities=None, residuals=None,
                  noconstant=False):
    """Logit: linear prediction / probabilities / response residuals."""
    return _binary_predict(df, "logit", dep, indep, predicted, probabilities, residuals, noconstant)


def probit_predict(df, dep, indep, predicted=None, probabilities=None, residuals=None,
                   noconstant=False):
    """Probit: linear prediction / probabilities / response residuals."""
    return _binary_predict(df, "probit", dep, indep, predicted, probabilities, residuals, noconstant)


def _coef_table(model):
    """Tidy coefficient table ``[term, coef, se, t, p]`` (``t`` is the z-statistic
    for non-OLS models — statsmodels stores it as ``tvalues``). Under an active
    release spec, coefficients whose design-matrix column has fewer than min_n
    nonzero entries are masked (a thin dummy's coefficient ≈ a small group's
    mean); no readable design matrix ⇒ fail closed (all masked)."""
    out = pd.DataFrame({
        "term": list(model.params.index),
        "coef": model.params.to_numpy(),
        "se": model.bse.to_numpy(),
        "t": model.tvalues.to_numpy(),
        "p": model.pvalues.to_numpy(),
    })
    exog = getattr(getattr(model, "model", None), "exog", None)
    design = None
    if exog is not None:
        arr = np.asarray(exog)
        # negbin appends a dispersion param (alpha) to params with no design
        # column — align on the design width; extra params count as full-n
        design = pd.DataFrame(arr, columns=list(model.params.index)[: arr.shape[1]])
    return _mask_thin_terms(out, design)


def regress(df, dep, indep, noconstant=False):
    """OLS coefficient table ``[term, coef, se, t, p]``."""
    return _coef_table(_fit_model(df, "regress", dep, indep, noconstant))


def logit(df, dep, indep, noconstant=False):
    """Logistic-regression coefficient table."""
    return _coef_table(_fit_model(df, "logit", dep, indep, noconstant))


def probit(df, dep, indep, noconstant=False):
    """Probit coefficient table."""
    return _coef_table(_fit_model(df, "probit", dep, indep, noconstant))


def poisson(df, dep, indep, noconstant=False):
    """Poisson (GLM) coefficient table."""
    return _coef_table(_fit_model(df, "poisson", dep, indep, noconstant))


def negative_binomial(df, dep, indep, noconstant=False):
    """Negative-binomial coefficient table (includes the dispersion ``alpha``)."""
    return _coef_table(_fit_model(df, "negative-binomial", dep, indep, noconstant))


# ── multinomial logit & regression discontinuity ─────────────────────────────

def mlogit(df, dep, indep, noconstant=False):
    """Multinomial logit (statsmodels MNLogit). Returns one coefficient row per
    (non-reference category, term): ``[category, term, coef, se, t, p]``. The
    reference category is the smallest value of ``dep``."""
    import statsmodels.api as sm
    from statsmodels.discrete.discrete_model import MNLogit
    d = df[[dep] + list(indep)].apply(pd.to_numeric, errors="coerce").dropna()
    Y = d[dep]
    cats = sorted(Y.unique())
    X = d[list(indep)].astype(float)
    if not noconstant:
        X = sm.add_constant(X, has_constant="add")
    model = MNLogit(Y, X).fit(disp=0)
    params, se, t, p = model.params, model.bse, model.tvalues, model.pvalues
    rows = []
    for j in range(params.shape[1]):            # one equation per non-reference cat
        cat = cats[j + 1]
        for ti, term in enumerate(params.index):
            rows.append({"category": cat, "term": term,
                         "coef": params.iloc[ti, j], "se": se.iloc[ti, j],
                         "t": t.iloc[ti, j], "p": p.iloc[ti, j]})
    return _mask_thin_terms(pd.DataFrame(rows), X)


def rdd(df, dep, runvar, exog=(), cutoff=0.0, polynomial=1, fuzzy=None):
    """Regression-discontinuity estimate, matching the emulator: use the
    ``rdrobust`` package when available (proper bandwidth selection; returns the
    Conventional/Bias-Corrected/Robust estimates as ``[method, estimate, se, p,
    ci_lower, ci_upper]``), else fall back to local-polynomial OLS (returns the
    single ``[term, estimate, se, z, p]`` discontinuity = the coefficient on
    T=1{runvar>=cutoff}, or the fuzzy treatment via 2SLS)."""
    import statsmodels.api as sm
    cols = [dep, runvar] + list(exog) + ([fuzzy] if fuzzy else [])
    d = df[cols].apply(pd.to_numeric, errors="coerce").dropna().astype(float)

    try:
        from rdrobust import rdrobust as _rdrobust
        kw = dict(y=d[dep].values, x=d[runvar].values, c=cutoff, p=polynomial)
        if exog:
            kw["covs"] = d[list(exog)].values
        if fuzzy:
            kw["fuzzy"] = d[fuzzy].values
        res = _rdrobust(**kw)
        return pd.DataFrame({
            "method": list(res.coef.index),
            "estimate": res.coef.iloc[:, 0].to_numpy(),
            "se": res.se.iloc[:, 0].to_numpy(),
            "p": res.pv.iloc[:, 0].to_numpy(),
            "ci_lower": res.ci.iloc[:, 0].to_numpy(),
            "ci_upper": res.ci.iloc[:, 1].to_numpy(),
        })
    except ImportError:
        pass

    # ---- fallback: manual local-polynomial OLS ----
    R = d[runvar] - cutoff
    T = (R >= 0).astype(float)
    X = pd.DataFrame({"const": 1.0, "T": T, "R": R, "T_R": T * R}, index=d.index)
    if polynomial >= 2:
        X["R2"] = R ** 2
        X["T_R2"] = T * R ** 2
    for c in exog:
        X[c] = d[c]
    if fuzzy:
        fuzzy_hat = sm.OLS(d[fuzzy], X).fit().predict()   # T instruments fuzzy
        X2 = X.drop(columns=["T"]).copy()
        X2[fuzzy] = fuzzy_hat
        model = sm.OLS(d[dep], X2).fit()
        disc = fuzzy
    else:
        model = sm.OLS(d[dep], X).fit()
        disc = "T"
    est, se = model.params[disc], model.bse[disc]
    return pd.DataFrame([{
        "term": "discontinuity", "estimate": est, "se": se,
        "z": est / se if se > 0 else np.nan, "p": model.pvalues[disc],
    }])


def mlogit_predict(df, dep, indep, predicted=None, probabilities=None, residuals=None,
                   noconstant=False):
    """Multinomial-logit predictions, one column per category (suffix ``_<cat>``),
    matching the emulator: ``probabilities`` -> P(Y=cat), ``predicted`` -> the
    linear predictor Xβ (0 for the reference category), ``residuals`` -> 1{Y=cat}-P;
    with no option, add ``prob_<cat>`` probabilities."""
    import statsmodels.api as sm
    from statsmodels.discrete.discrete_model import MNLogit
    d = df[[dep] + list(indep)].apply(pd.to_numeric, errors="coerce").dropna()
    Y = d[dep]
    cats = sorted(Y.unique())
    X = d[list(indep)].astype(float)
    if not noconstant:
        X = sm.add_constant(X, has_constant="add")
    model = MNLogit(Y, X).fit(disp=0)
    probs = model.predict()                       # n × K
    labels = [str(int(c)) if float(c) == int(c) else str(c) for c in cats]
    out = df.copy()

    def add(base, series_for):
        for i, lab in enumerate(labels):
            out[f"{base}_{lab}"] = pd.Series(series_for(i), index=d.index).reindex(df.index)

    added = False
    if probabilities:
        add(probabilities if probabilities is not True else "prob", lambda i: probs[:, i])
        added = True
    if predicted:
        base = predicted if predicted is not True else "predicted"
        for i, lab in enumerate(labels):
            col = (np.zeros(len(d)) if i == 0
                   else np.asarray(X @ model.params.iloc[:, i - 1]))
            out[f"{base}_{lab}"] = pd.Series(col, index=d.index).reindex(df.index)
        added = True
    if residuals:
        base = residuals if residuals is not True else "residuals"
        add(base, lambda i: (Y == cats[i]).astype(float).to_numpy() - probs[:, i])
        added = True
    if not added:
        add("prob", lambda i: probs[:, i])
    return out


# ── panel & instrumental-variables regression ────────────────────────────────

def regress_panel(df, dep, indep, effect="fe", key=None):
    """Panel regression. ``effect`` in fe (PanelOLS entity effects, default),
    re (RandomEffects), be (BetweenOLS), pooled (OLS). Needs a ``tid`` column and
    an entity-key column (auto-detected like the emulator, default ``unit_id``).
    Returns a coefficient table ``[term, coef, se, t, p]``."""
    import statsmodels.api as sm
    from m2py import _get_df_key_col
    key = key or _get_df_key_col(df) or "unit_id"
    if "tid" not in df.columns:
        raise ValueError("regress-panel requires a 'tid' (time) column")
    if key not in df.columns:
        raise ValueError(f"regress-panel requires an entity-key column ('{key}')")
    d = df[[dep] + list(indep) + [key, "tid"]].copy()
    for v in [dep] + list(indep):
        d[v] = pd.to_numeric(d[v], errors="coerce")
    d = d.dropna()
    pidx = d.set_index([key, "tid"])
    Y = pidx[dep]
    X = sm.add_constant(pidx[list(indep)], has_constant="add")
    if effect == "pooled":
        return _coef_table(sm.OLS(Y, X).fit())
    from linearmodels.panel import PanelOLS, RandomEffects, BetweenOLS
    if effect == "re":
        model = RandomEffects(Y, X).fit()
    elif effect == "be":
        model = BetweenOLS(Y, X).fit()
    else:
        model = PanelOLS(Y, X, entity_effects=True, drop_absorbed=True).fit()
    out = pd.DataFrame({
        "term": list(model.params.index),
        "coef": model.params.to_numpy(),
        "se": model.std_errors.to_numpy(),
        "t": model.tstats.to_numpy(),
        "p": model.pvalues.to_numpy(),
    })
    return _mask_thin_terms(out, X[list(model.params.index)])


def _iv_fit(df, dep, exog, endog, instruments):
    """Manual 2SLS. Returns (robust_model, X2_columns, fitted_pred, resid, index)
    where fitted_pred uses the ACTUAL endog values (for 2SLS SEs/predictions)."""
    import statsmodels.api as sm
    exog, endog, instruments = list(exog), list(endog), list(instruments)
    d = df[[dep] + exog + endog + instruments].apply(
        pd.to_numeric, errors="coerce").dropna().astype(float)
    Y = d[dep]
    Z = sm.add_constant(d[instruments + exog], has_constant="add")
    fitted = pd.DataFrame(index=d.index)
    for ev in endog:
        fitted[ev] = sm.OLS(d[ev], Z).fit().predict()
    X2 = d[exog].copy() if exog else pd.DataFrame(index=d.index)
    for ev in endog:
        X2[ev] = fitted[ev]
    X2 = sm.add_constant(X2, has_constant="add")
    model = sm.OLS(Y, X2).fit()
    Xa = sm.add_constant(d[exog + endog], has_constant="add").reindex(
        columns=X2.columns, fill_value=0.0)
    pred = Xa @ model.params
    resid = Y - pred
    sigma2 = float(resid @ resid) / model.df_resid
    robust = model.get_robustcov_results(cov_type="fixed scale", scale=sigma2)
    return robust, list(X2.columns), pred, resid, d.index


def regress_panel_predict(df, dep, indep, effect="fe", key=None,
                          predicted="predicted", residuals=None, effects=None):
    """Panel regression with predictions added as columns. With linearmodels
    (fe/re/be) uses ``model.fitted_values``/``resids``/``estimated_effects``;
    pooled uses OLS ``predict``. Matches the emulator's regress-panel-predict."""
    import statsmodels.api as sm
    from m2py import _get_df_key_col
    key = key or _get_df_key_col(df) or "unit_id"
    if "tid" not in df.columns or key not in df.columns:
        raise ValueError("regress-panel-predict requires a 'tid' column and an entity key")
    d = df[[dep] + list(indep) + [key, "tid"]].copy()
    for v in [dep] + list(indep):
        d[v] = pd.to_numeric(d[v], errors="coerce")
    d = d.dropna()
    idx = d.index
    pidx = d.set_index([key, "tid"])
    Y = pidx[dep]
    X = sm.add_constant(pidx[list(indep)], has_constant="add")
    out = df.copy()

    def put(name, values):
        out[name] = pd.Series(np.asarray(values).ravel(), index=idx).reindex(df.index)

    if effect == "pooled":
        model = sm.OLS(Y, X).fit()
        put(predicted or "predicted", model.predict(X))
        if residuals:
            put(residuals, (Y - model.predict(X)).to_numpy())
        return out
    from linearmodels.panel import PanelOLS, RandomEffects, BetweenOLS
    if effect == "re":
        model = RandomEffects(Y, X).fit()
    elif effect == "be":
        model = BetweenOLS(Y, X).fit()
    else:
        model = PanelOLS(Y, X, entity_effects=True, drop_absorbed=True).fit()
    put(predicted or "predicted", model.fitted_values.to_numpy())
    if residuals:
        put(residuals, model.resids.to_numpy())
    if effects:
        try:
            put(effects, model.estimated_effects.to_numpy())
        except Exception:
            pass
    return out


def ivregress(df, dep, exog, endog, instruments):
    """Instrumental-variables (2SLS) regression with the emulator's fixed-scale
    2SLS standard errors. Returns the second-stage coefficient table
    ``[term, coef, se, t, p]``."""
    robust, terms, _, _, _ = _iv_fit(df, dep, exog, endog, instruments)
    out = pd.DataFrame({
        "term": terms,
        "coef": np.asarray(robust.params),
        "se": np.asarray(robust.bse),
        "t": np.asarray(robust.tvalues),
        "p": np.asarray(robust.pvalues),
    })
    rexog = getattr(getattr(robust, "model", None), "exog", None)
    design = (pd.DataFrame(np.asarray(rexog), columns=terms)
              if rexog is not None else None)
    return _mask_thin_terms(out, design)


def ivregress_predict(df, dep, exog, endog, instruments, predicted="predicted",
                      residuals=None):
    """IV (2SLS): add fitted values (using the actual endog) and residuals."""
    _, _, pred, resid, idx = _iv_fit(df, dep, exog, endog, instruments)
    out = df.copy()
    out[predicted or "predicted"] = pd.Series(np.asarray(pred), index=idx).reindex(df.index)
    if residuals:
        out[residuals] = pd.Series(np.asarray(resid), index=idx).reindex(df.index)
    return out


def regress_panel_diff(df, dep, group, treated, covars=()):
    """Difference-in-differences (pooled OLS with a group×treated interaction).
    Returns the coefficient table; the interaction term is the ATET."""
    import statsmodels.api as sm
    interact = f"{group}_x_{treated}"
    base = [group, treated] + [c for c in covars if c not in (group, treated)]
    d = df[[dep] + base].apply(pd.to_numeric, errors="coerce").dropna().astype(float)
    d[interact] = d[group] * d[treated]
    indep = [group, treated, interact] + [c for c in base if c not in (group, treated)]
    X = sm.add_constant(d[indep], has_constant="add")
    return _coef_table(sm.OLS(d[dep], X).fit())


# ── survival analysis (lifelines, matching the emulator) ──────────────────────

def cox(df, event, duration, covars=(), level=95):
    """Cox proportional-hazards model. Returns a coefficient table
    ``[term, coef, hazard_ratio, se, z, p]`` (rows dropped if missing or
    duration<=0, matching the emulator)."""
    from lifelines import CoxPHFitter
    cols = [event, duration] + [c for c in covars if c in df.columns]
    sub = df[cols].apply(pd.to_numeric, errors="coerce").dropna()
    sub = sub[sub[duration] > 0]
    cph = CoxPHFitter(alpha=1 - level / 100)
    cph.fit(sub, duration_col=duration, event_col=event)
    s = cph.summary
    out = pd.DataFrame({
        "term": list(s.index),
        "coef": s["coef"].to_numpy(),
        "hazard_ratio": s["exp(coef)"].to_numpy(),
        "se": s["se(coef)"].to_numpy(),
        "z": s["z"].to_numpy(),
        "p": s["p"].to_numpy(),
    })
    covar_cols = [c for c in sub.columns if c not in (event, duration)]
    return _mask_thin_terms(out, sub[covar_cols],
                            stat_cols=("coef", "hazard_ratio", "se", "z", "p"))


def kaplan_meier(df, event, duration):
    """Kaplan-Meier estimate. Returns the survival function as
    ``[time, survival]`` (the curve the emulator plots)."""
    from lifelines import KaplanMeierFitter
    sub = df[[event, duration]].apply(pd.to_numeric, errors="coerce").dropna()
    kmf = KaplanMeierFitter()
    kmf.fit(sub[duration], sub[event])
    out = kmf.survival_function_.reset_index()
    out.columns = ["time", "survival"]
    spec = get_release_spec()
    if spec:
        # release the curve only while >= min_n units remain at risk — the
        # tail steps are individual event times over a near-empty risk set
        min_n = spec.get("min_n", 5)
        at_risk = out["time"].map(lambda t: int((sub[duration] >= t).sum()))
        out = out[at_risk >= min_n].reset_index(drop=True)
    return out


def weibull(df, event, duration):
    """Weibull (AFT) survival model. Returns the fitted ``lambda``/``rho``
    parameters plus ``n``/``events``, mirroring the emulator."""
    from lifelines import WeibullAFTFitter
    sub = df[[event, duration]].apply(pd.to_numeric, errors="coerce").dropna()
    sub = sub[sub[duration] > 0]
    waf = WeibullAFTFitter()
    waf.fit(sub, duration_col=duration, event_col=event)
    row = {"n": len(sub), "events": int(sub[event].sum())}
    if hasattr(waf, "lambda_") and hasattr(waf, "rho_"):
        row["lambda"] = float(waf.lambda_)
        row["rho"] = float(waf.rho_)
    else:
        # lifelines >= 0.30 has no lambda_/rho_ attrs: read the intercept coefs
        # (same fallback the emulator uses); strip the trailing underscore.
        for pname, pval in waf.summary["coef"].items():
            key = (pname[0] if isinstance(pname, tuple) else str(pname)).rstrip("_")
            row[key] = float(pval)
    return pd.DataFrame([row])


def coefplot(df, reg_cmd, dep, indep, standardize=False, noconstant=False):
    """Coefficient plot: fit ``reg_cmd`` (regress/logit/probit/poisson) and plot
    the non-intercept coefficients (x) vs variable names (y) with 95% CI error
    bars. Mirrors the emulator's `_fit_simple`."""
    import plotly.graph_objects as go
    if reg_cmd not in ("regress", "logit", "probit", "poisson"):
        raise ValueError(f"coefplot does not support '{reg_cmd}'")
    model = _fit_model(df, reg_cmd, dep, indep, noconstant, standardize)
    params = model.params.drop("const", errors="ignore")
    ci = model.conf_int().drop("const", errors="ignore")
    spec = get_release_spec()
    if spec:
        # same thin-term rule as the coefficient tables — a plot must not
        # reveal what the table masks. Fail closed without a design matrix.
        exog = getattr(getattr(model, "model", None), "exog", None)
        if exog is None:
            params, ci = params.iloc[:0], ci.iloc[:0]
        else:
            design = pd.DataFrame(np.asarray(exog), columns=list(model.params.index))
            at_risk = (design[params.index] != 0).sum()
            keep = at_risk[at_risk >= spec.get("min_n", 5)].index
            params, ci = params[keep], ci.loc[keep]
    coefs = params.values.tolist()
    lo, hi = ci.iloc[:, 0].tolist(), ci.iloc[:, 1].tolist()
    err_minus = [c - l for c, l in zip(coefs, lo)]
    err_plus = [h - c for c, h in zip(coefs, hi)]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=coefs, y=list(params.index), mode="markers",
        marker=dict(size=9, color="#2563eb"),
        error_x=dict(type="data", symmetric=False, array=err_plus,
                     arrayminus=err_minus, thickness=1.5, width=6)))
    fig.add_vline(x=0, line_dash="dot", line_color="#9ca3af", line_width=1)
    return fig
