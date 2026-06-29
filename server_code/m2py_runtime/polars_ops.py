# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the m2py repo. This file is produced by sync_to_api.py.
# Edit the engine in the m2py repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""Pure polars runtime ops (lazy).

Each op takes a ``pl.LazyFrame`` plus parsed-IR arguments and returns a new
``pl.LazyFrame`` — the whole pipeline stays lazy so a single
``.collect(engine="streaming")`` at the end can process larger-than-memory data.
Mirrors :mod:`m2py_runtime.pandas_ops` op-for-op. polars is imported lazily
inside the module so importing this package never fails under Pyodide.

Expression handling goes through :mod:`m2py_runtime.exprcompile`, which raises
``UnsupportedExpr`` for syntax it can't map — callers (the translator) turn that
into an ``UNTRANSLATED`` marker rather than emitting wrong polars.
"""

from .exprcompile import compile_expr, UnsupportedExpr  # noqa: F401
from .sources import scan_source as read_source  # noqa: F401  (generated code calls ops.read_source)

# microdata collapse/summarize stat -> polars Expr method (element of a group agg)
_AGG_METHOD = {
    "mean": "mean", "sum": "sum", "min": "min", "max": "max",
    "count": "count", "median": "median", "std": "std", "sd": "std",
    "var": "var", "first": "first", "last": "last",
}


def _pl():
    import polars as pl
    return pl


def _agg_expr(stat, src, alias):
    pl = _pl()
    method = _AGG_METHOD.get(stat)
    if method is None:
        raise UnsupportedExpr(f"collapse/summarize stat ({stat}) unsupported in polars")
    return getattr(pl.col(src), method)().alias(alias)


# ── value-producing verbs ────────────────────────────────────────────────────
# Expressions compile to native polars (lazy) when possible; anything the
# compiler can't express (scipy distributions, label funcs, etc.) falls back to
# the emulator's own pandas evaluator — materialise, eval, re-lazy — so every one
# of microdata's 85 functions works, exactly as in the emulator.

def _eval_fallback(lf, fn, *args):
    pl = _pl()
    from . import pandas_ops as pdo
    return pl.from_pandas(getattr(pdo, fn)(lf.collect().to_pandas(), *args)).lazy()


def generate(lf, target, expression, cond=None):
    pl = _pl()
    try:
        e = compile_expr(expression)
        if cond:
            c = compile_expr(cond, condition=True)
            existing = lf.collect_schema().names()
            otherwise = pl.col(target) if target in existing else pl.lit(None)
            e = pl.when(c).then(e).otherwise(otherwise)
        return lf.with_columns(e.alias(target))
    except UnsupportedExpr:
        return _eval_fallback(lf, "generate", target, expression, cond)


def replace(lf, target, expression, cond=None):
    return generate(lf, target, expression, cond)


def recode(lf, vars, rules, prefix=None):
    # delegate to the pandas op (emulator's DataTransformHandler) for the full
    # rule grammar — multi-value, ranges, min/max, missing/*, labels.
    pl = _pl()
    from . import pandas_ops as pdo
    return pl.from_pandas(pdo.recode(lf.collect().to_pandas(), vars, rules, prefix)).lazy()


# ── row/column shaping ───────────────────────────────────────────────────────

def keep(lf, vars=None, cond=None):
    try:
        out = lf.select(vars) if vars else lf
        if cond:
            out = out.filter(compile_expr(cond, condition=True))
        return out
    except UnsupportedExpr:
        return _eval_fallback(lf, "keep", vars, cond)


def drop(lf, vars=None, cond=None):
    try:
        out = lf.drop(vars) if vars else lf
        if cond:
            out = out.filter(~compile_expr(cond, condition=True))
        return out
    except UnsupportedExpr:
        return _eval_fallback(lf, "drop", vars, cond)


# ── aggregation / reshaping ──────────────────────────────────────────────────

def collapse(lf, targets, by=None):
    if isinstance(by, str) and by.strip():
        by = by.strip().split()[0]
    aggs = [_agg_expr(t["stat"], t["src"], t["target"] or t["src"]) for t in targets]
    if not by:
        return lf.select(aggs)
    return lf.group_by(by).agg(aggs)


def aggregate(lf, targets, by=None):
    pl = _pl()
    cols = []
    for t in targets:
        method = _AGG_METHOD.get(t["stat"])
        if method is None:
            raise UnsupportedExpr(f"aggregate stat ({t['stat']}) unsupported in polars")
        new_var = t["target"] or t["src"]
        cols.append(getattr(pl.col(t["src"]), method)().over(by).alias(new_var))
    return lf.with_columns(cols)


def emulate_import(name):
    """Synthesize a base population (lazy) for ``name`` via the emulator — opt-in
    fallback when an input isn't provided. Mock data, keyed by PERSONID_1."""
    pl = _pl()
    from . import pandas_ops as pdo
    return pl.from_pandas(pdo.emulate_import(name)).lazy()


def merge(lf, other, on, how="left"):
    # pandas uses how="outer"; polars renamed that to "full" and needs
    # coalesce=True so the join key stays a single column (matching pandas).
    if how == "outer":
        return lf.join(other, on=on, how="full", coalesce=True)
    return lf.join(other, on=on, how=how)


def merge_into(target, source, vars, left_on, right_on):
    """Into-form merge: bring ``vars`` from ``source`` onto ``target``. The
    emulator's column-drop/dedup semantics are fiddly and not streamable, so we
    materialise, reuse the tested pandas op, and re-lazy."""
    pl = _pl()
    from . import pandas_ops as pdo
    t = target.collect().to_pandas() if hasattr(target, "collect") else target
    s = source.collect().to_pandas() if hasattr(source, "collect") else source
    return pl.from_pandas(pdo.merge_into(t, s, vars, left_on, right_on)).lazy()


# reshape needs the whole frame (not streamable) -> materialise, reshape via the
# tested pandas op, and re-lazy; the pipeline continues lazily afterwards.
def _reshape(lf, fn, *a):
    pl = _pl()
    from . import pandas_ops as pdo
    return pl.from_pandas(getattr(pdo, fn)(lf.collect().to_pandas(), *a))


def reshape_to_panel(lf, prefixes):
    return _reshape(lf, "reshape_to_panel", prefixes).lazy()


def reshape_from_panel(lf):
    return _reshape(lf, "reshape_from_panel").lazy()


def rename(lf, old, new):
    return lf.rename({old: new})


def clone_variables(lf, pairs, prefix="", suffix=""):
    pl = _pl()
    names = lf.collect_schema().names()
    cols = []
    for old, new in pairs:
        if old in names:
            actual = f"{prefix}{old}{suffix}" if (prefix or suffix) else new
            cols.append(pl.col(old).alias(actual))
    return lf.with_columns(cols) if cols else lf


def clone_units(lf):
    pl = _pl()
    from . import pandas_ops as pdo
    return pl.from_pandas(pdo.clone_units(lf.collect().to_pandas())).lazy()


def destring(lf, vars):
    pl = _pl()
    return lf.with_columns(
        [pl.col(v).cast(pl.Float64, strict=False) for v in vars])


# ── analysis ─────────────────────────────────────────────────────────────────

# ── analysis sinks ────────────────────────────────────────────────────────────
# These are terminal outputs (not part of the lazy transform pipeline), so they
# collect the frame and delegate to the tested pandas implementation, returning
# a pl.DataFrame. Equivalence with the pandas backend is therefore guaranteed;
# the lazy/streaming benefit lives in the transform ops above. regress needs
# pandas+statsmodels regardless.

def _analysis(lf, fn, *a, **kw):
    pl = _pl()
    from . import pandas_ops as pdo
    pdf = lf.collect().to_pandas()
    return pl.from_pandas(getattr(pdo, fn)(pdf, *a, **kw).reset_index(drop=True))


def summarize(lf, vars=None, by=None, gini=False, iqr=False):
    return _analysis(lf, "summarize", vars, by, gini=gini, iqr=iqr)


def tabulate(lf, vars, by=None, missing=False,
             cellpct=False, rowpct=False, colpct=False,
             chi2=False, top=None, bottom=None):
    return _analysis(lf, "tabulate", vars, by, missing=missing,
                     cellpct=cellpct, rowpct=rowpct, colpct=colpct,
                     chi2=chi2, top=top, bottom=bottom)


def correlate(lf, vars, pairwise=False, covariance=False):
    return _analysis(lf, "correlate", vars, pairwise=pairwise, covariance=covariance)


def summarize_panel(lf, vars=None, gini=False, iqr=False):
    return _analysis(lf, "summarize_panel", vars, gini=gini, iqr=iqr)


def tabulate_panel(lf, var1, missing=False, rowpct=False, colpct=False):
    return _analysis(lf, "tabulate_panel", var1, missing=missing, rowpct=rowpct, colpct=colpct)


def transitions_panel(lf, vars=None):
    return _analysis(lf, "transitions_panel", vars)


def normaltest(lf, vars=None):
    return _analysis(lf, "normaltest", vars)


def ci(lf, vars=None, level=95):
    return _analysis(lf, "ci", vars, level=level)


def anova(lf, dep, factors):
    return _analysis(lf, "anova", dep, factors)


def hausman(lf, dep, indep, key=None):
    return _analysis(lf, "hausman", dep, indep, key=key)


def regress(lf, dep, indep, noconstant=False):
    return _analysis(lf, "regress", dep, indep, noconstant=noconstant)


def logit(lf, dep, indep, noconstant=False):
    return _analysis(lf, "logit", dep, indep, noconstant=noconstant)


def probit(lf, dep, indep, noconstant=False):
    return _analysis(lf, "probit", dep, indep, noconstant=noconstant)


def poisson(lf, dep, indep, noconstant=False):
    return _analysis(lf, "poisson", dep, indep, noconstant=noconstant)


def negative_binomial(lf, dep, indep, noconstant=False):
    return _analysis(lf, "negative_binomial", dep, indep, noconstant=noconstant)


# ── predict (transform: fit a model and augment the frame with predictions) ───
# Model fitting needs all rows in pandas, so these materialise, run the pandas
# op, and return a fresh LazyFrame; subsequent transforms continue lazily.

def _predict_transform(lf, fn, *a, **kw):
    pl = _pl()
    from . import pandas_ops as pdo
    return pl.LazyFrame(getattr(pdo, fn)(lf.collect().to_pandas(), *a, **kw))


def regress_predict(lf, dep, indep, predicted="predicted", residuals=None, noconstant=False):
    return _predict_transform(lf, "regress_predict", dep, indep, predicted, residuals, noconstant)


def negative_binomial_predict(lf, dep, indep, predicted="predicted", residuals=None,
                              noconstant=False):
    return _predict_transform(lf, "negative_binomial_predict", dep, indep, predicted,
                              residuals, noconstant)


def logit_predict(lf, dep, indep, predicted=None, probabilities=None, residuals=None,
                  noconstant=False):
    return _predict_transform(lf, "logit_predict", dep, indep, predicted, probabilities,
                              residuals, noconstant)


def probit_predict(lf, dep, indep, predicted=None, probabilities=None, residuals=None,
                   noconstant=False):
    return _predict_transform(lf, "probit_predict", dep, indep, predicted, probabilities,
                              residuals, noconstant)


def mlogit(lf, dep, indep, noconstant=False):
    return _analysis(lf, "mlogit", dep, indep, noconstant=noconstant)


def mlogit_predict(lf, dep, indep, predicted=None, probabilities=None, residuals=None,
                   noconstant=False):
    return _predict_transform(lf, "mlogit_predict", dep, indep, predicted, probabilities,
                              residuals, noconstant)


def rdd(lf, dep, runvar, exog=(), cutoff=0.0, polynomial=1, fuzzy=None):
    return _analysis(lf, "rdd", dep, runvar, exog=exog, cutoff=cutoff,
                     polynomial=polynomial, fuzzy=fuzzy)


def regress_panel(lf, dep, indep, effect="fe", key=None):
    return _analysis(lf, "regress_panel", dep, indep, effect=effect, key=key)


def regress_panel_diff(lf, dep, group, treated, covars=()):
    return _analysis(lf, "regress_panel_diff", dep, group, treated, covars=covars)


def regress_panel_predict(lf, dep, indep, effect="fe", key=None,
                          predicted="predicted", residuals=None, effects=None):
    return _predict_transform(lf, "regress_panel_predict", dep, indep, effect, key,
                              predicted, residuals, effects)


def ivregress(lf, dep, exog, endog, instruments):
    return _analysis(lf, "ivregress", dep, exog, endog, instruments)


def ivregress_predict(lf, dep, exog, endog, instruments, predicted="predicted",
                      residuals=None):
    return _predict_transform(lf, "ivregress_predict", dep, exog, endog, instruments,
                              predicted, residuals)


def cox(lf, event, duration, covars=(), level=95):
    return _analysis(lf, "cox", event, duration, covars=covars, level=level)


def kaplan_meier(lf, event, duration):
    return _analysis(lf, "kaplan_meier", event, duration)


def weibull(lf, event, duration):
    return _analysis(lf, "weibull", event, duration)


# ── plots ─────────────────────────────────────────────────────────────────────
# Terminal sinks: collect and delegate to the tested pandas plot builders,
# returning the plotly Figure directly (not a frame).

def _plot(lf, fn, *a, **kw):
    from . import pandas_ops as pdo
    return getattr(pdo, fn)(lf.collect().to_pandas(), *a, **kw)


def histogram(lf, vars, bins=30, discrete=False, percent=False, density=False,
              normal=False):
    return _plot(lf, "histogram", vars, bins=bins, discrete=discrete,
                 percent=percent, density=density, normal=normal)


def barchart(lf, vars, stat="count", over=None, horizontal=False, stack=False):
    return _plot(lf, "barchart", vars, stat=stat, over=over,
                 horizontal=horizontal, stack=stack)


def scatter(lf, vars, by=None):
    return _plot(lf, "scatter", vars, by=by)


def boxplot(lf, vars, over=None):
    return _plot(lf, "boxplot", vars, over=over)


def piechart(lf, vars, stat="count"):
    return _plot(lf, "piechart", vars, stat=stat)


def hexbin(lf, vars, bins=30):
    return _plot(lf, "hexbin", vars, bins=bins)


def sankey(lf, vars):
    return _plot(lf, "sankey", vars)


def coefplot(lf, reg_cmd, dep, indep, standardize=False, noconstant=False):
    return _plot(lf, "coefplot", reg_cmd, dep, indep,
                 standardize=standardize, noconstant=noconstant)
