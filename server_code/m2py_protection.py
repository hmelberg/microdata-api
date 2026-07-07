# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the m2py repo. This file is produced by sync_to_api.py.
# Edit the engine in the m2py repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""Protection policy + the pandas ProtectionAdapter for SafeStat remote compute.

resolve_policy turns one-or-more source protection levels into a single policy
(most-restrictive-source-wins). The suppression numbers come from safepy's
tier presets when the vendored safepy package is importable (the server), so
both engines read ONE shared config; the fallback table below mirrors those
presets for non-server contexts (Pyodide, bare test runs).

PandasProtect wraps the `protect` package for result-side disclosure control:
count-bearing result tables (tabulate's "n", summarize's "count") go through
the full protect.suppress with counts pairing + rounding. Aggregate tables
without a count column and model objects pass through for now — they are the
next slice (the DSL ops must emit contribution counts first).
"""
from __future__ import annotations

PUBLIC = "public"
PROTECTED = "protected"
SENSITIVE = "sensitive"

_ORDER = {PUBLIC: 0, PROTECTED: 1, SENSITIVE: 2}

# MUST mirror safepy/policy.py PRESETS ("standard" -> protected, "microdata"
# -> sensitive). Only used when safepy is not importable.
_FALLBACK = {
    PROTECTED: {"min_n": 5, "round": 10, "percentile_sig_figs": 3,
                "max_low_cell_share": 0.5},
    SENSITIVE: {"min_n": 5, "round": 10, "percentile_sig_figs": 3,
                "max_low_cell_share": 0.5},
}


def _preset_for(level):
    """Suppression levers for a level, read from safepy's presets when available."""
    try:
        try:
            # Sets SAFEPY_NOISE_SALT from the Anvil secret BEFORE any safepy
            # import (safepy reads it at import time). No-op off the server.
            import safepy_shim  # noqa: F401
        except Exception:
            pass
        from safepy.policy import PRESETS, _LEVEL_PRESET
        s = PRESETS[_LEVEL_PRESET[level]]
        return {"min_n": s.min_n, "round": s.round_to,
                "percentile_sig_figs": s.percentile_sig_figs,
                "max_low_cell_share": s.max_low_cell_share}
    except Exception:
        return dict(_FALLBACK[level])


def resolve_policy(levels):
    """Most-restrictive-source-wins. Returns a ProtectionPolicy dict."""
    level = max(levels, key=lambda lv: _ORDER[lv]) if levels else PUBLIC
    if level == PUBLIC:
        return {"level": PUBLIC, "auth_required": False, "log": False,
                "pre_recipe": None, "post_suppress": None}
    spec = _preset_for(level)
    spec["secondary"] = level == SENSITIVE
    if level == PROTECTED:
        return {"level": PROTECTED, "auth_required": True, "log": True,
                "pre_recipe": None, "post_suppress": spec}
    return {"level": SENSITIVE, "auth_required": True, "log": True,
            "pre_recipe": {"profile": "microdata_no"},
            "post_suppress": spec}


class PandasProtect:
    """Result-side suppression for pandas, backed by `protect`.

    `suppress` runs on the structured result object BEFORE it is serialized
    to HTML. Handled here:
      - tabulate-style frames (count column "n"): the counts get primary
        suppression (min_n) + rounding; category-key columns are untouched.
      - summarize-style frames (count column "count"): every numeric stat
        column is suppressed PAIRED with the counts (a mean over a small
        group disappears with its group), then the counts themselves.
    Frames without a count column and model objects pass through — next
    slice ("secondary" in the spec is reserved for crosstab-shaped releases).
    pre() (pre_recipe) is likewise not applied yet.
    """

    _COUNT_COLS = ("n", "count")
    # tabulate's derived percentage columns: exact values would let a reader
    # back out a suppressed count, so they are masked with the counts and
    # rounded to whole percent.
    _PCT_COLS = ("cellpct", "rowpct", "colpct")

    def suppress(self, result, spec):
        if spec is None:
            return result
        try:
            import pandas as pd
        except Exception:
            return result
        if hasattr(result, "params") and hasattr(result, "conf_int"):
            return self._suppress_model(result, spec)
        if not isinstance(result, pd.DataFrame):
            return result
        count_col = next((c for c in self._COUNT_COLS if c in result.columns), None)
        if count_col is None:
            return result
        import protect as p
        min_n = spec.get("min_n", 5)
        counts = result[count_col]
        # Tiltak 5, sparse-table stop: when most cells are below min_n the
        # row LABELS alone enumerate raw values (e.g. tabulate of a nearly
        # unique column) — refuse the whole table instead of releasing it.
        low_share = spec.get("max_low_cell_share")
        if (count_col == "n" and low_share is not None and len(counts) > 1
                and (counts < min_n).mean() > low_share):
            return ("Personvern: tabellen er for spredt (de fleste cellene har "
                    f"færre enn {min_n} enheter) og frigis ikke. Grupper "
                    "variabelen grovere (f.eks. med recode) og prøv igjen.")
        out = result.copy()
        if count_col == "count":
            for c in out.columns:
                if c == count_col or not pd.api.types.is_numeric_dtype(out[c]):
                    continue
                out[c] = p.suppress(out[c], counts=counts, min_n=min_n)
        for c in self._PCT_COLS:
            if c in out.columns:
                out[c] = p.suppress(out[c], counts=counts, min_n=min_n, round=1)
        out[count_col] = p.suppress(counts, min_n=min_n, round=spec.get("round"))
        if spec.get("secondary") and count_col == "n":
            out = self._secondary_two_way(out, result)
        return out

    def _secondary_two_way(self, out, original):
        """Secondary suppression for two-way tabulate frames (long format,
        two key columns + n): pivot, run protect's greedy secondary pass so
        marginals can't recover a lone suppressed cell, and map the extra
        NaNs back onto the long frame. No-op for one-way/other shapes."""
        try:
            import protect as p
            keys = [c for c in out.columns
                    if c != "n" and c not in self._PCT_COLS
                    and not c.startswith("chi2")]
            if len(keys) != 2:
                return out
            pivot = out.pivot(index=keys[0], columns=keys[1], values="n")
            masked = p.suppress(pivot, secondary=True)
            long = masked.stack(dropna=False).rename("n").reset_index()
            merged = out.drop(columns=["n"]).merge(long, on=keys, how="left")
            return merged[list(out.columns)]
        except Exception:
            return out

    def _suppress_model(self, result, spec):
        """statsmodels-style results: per-coefficient at-risk suppression.

        The at-risk count of a coefficient is the number of nonzero entries in
        its design-matrix column (a dummy's coefficient is close to a small
        group's mean). Renders as a coef table; the full summary() (loglik,
        residual details, …) is withheld on non-public data. Results without a
        readable design matrix pass through unchanged (documented gap)."""
        try:
            import numpy as np
            import pandas as pd
            exog = getattr(getattr(result, "model", None), "exog", None)
            if exog is None:
                return result
            params = result.params
            if not hasattr(params, "index"):
                names = [f"x{i}" for i in range(len(params))]
                params = pd.Series(np.asarray(params), index=names)
            ci = result.conf_int()
            if not hasattr(ci, "loc"):
                ci = pd.DataFrame(np.asarray(ci), index=params.index, columns=[0, 1])
            at_risk = pd.Series((np.asarray(exog) != 0).sum(axis=0),
                                index=params.index)
            min_n = spec.get("min_n", 5)
            out = pd.DataFrame({
                "coef": params.where(at_risk >= min_n),
                "ci_low": ci.iloc[:, 0].where(at_risk >= min_n),
                "ci_high": ci.iloc[:, 1].where(at_risk >= min_n),
            })
            out.index.name = "term"
            return out
        except Exception as exc:
            # 2026-07-07 review finding: this bare except previously made the
            # documented "exog is None -> pass through" gap indistinguishable
            # from a genuinely unexpected failure (a numpy shape mismatch, a
            # future statsmodels version changing .conf_int()'s shape, etc) —
            # both silently released the raw, unsuppressed model result. Not
            # narrowing the except itself (the range of model-like objects
            # that can reach here isn't fully enumerable without over-fitting
            # to statsmodels specifically), but making the unexpected case
            # OBSERVABLE instead of silent, so a future regression here is at
            # least visible in logs rather than a silent disclosure-control
            # gap nobody notices.
            try:
                print(f"[m2py_protection] _suppress_model fell through unsuppressed: {exc!r}")
            except Exception:
                pass
            return result
