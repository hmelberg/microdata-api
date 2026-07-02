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
    PROTECTED: {"min_n": 5, "round": 10},
    SENSITIVE: {"min_n": 5, "round": 10},
}


def _preset_for(level):
    """min_n/round for a level, read from safepy's presets when available."""
    try:
        try:
            # Sets SAFEPY_NOISE_SALT from the Anvil secret BEFORE any safepy
            # import (safepy reads it at import time). No-op off the server.
            import safepy_shim  # noqa: F401
        except Exception:
            pass
        from safepy.policy import PRESETS, _LEVEL_PRESET
        s = PRESETS[_LEVEL_PRESET[level]]
        return {"min_n": s.min_n, "round": s.round_to}
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

    def suppress(self, result, spec):
        if spec is None:
            return result
        try:
            import pandas as pd
        except Exception:
            return result
        if not isinstance(result, pd.DataFrame):
            return result
        count_col = next((c for c in self._COUNT_COLS if c in result.columns), None)
        if count_col is None:
            return result
        import protect as p
        min_n = spec.get("min_n", 5)
        out = result.copy()
        counts = result[count_col]
        if count_col == "count":
            for c in out.columns:
                if c == count_col or not pd.api.types.is_numeric_dtype(out[c]):
                    continue
                out[c] = p.suppress(out[c], counts=counts, min_n=min_n)
        out[count_col] = p.suppress(counts, min_n=min_n, round=spec.get("round"))
        return out
