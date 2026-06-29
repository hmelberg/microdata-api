# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the m2py repo. This file is produced by sync_to_api.py.
# Edit the engine in the m2py repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""Protection policy + the pandas ProtectionAdapter for SafeStat remote compute.

resolve_policy turns one-or-more source protection levels into a single policy
(most-restrictive-source-wins). PandasProtect is the v1 reference adapter; it
wraps the `protect` package for result-side disclosure control. No emulator or
translator code is touched here — this is purely additive.
"""
from __future__ import annotations

PUBLIC = "public"
PROTECTED = "protected"
SENSITIVE = "sensitive"

_ORDER = {PUBLIC: 0, PROTECTED: 1, SENSITIVE: 2}


def resolve_policy(levels):
    """Most-restrictive-source-wins. Returns a ProtectionPolicy dict."""
    level = max(levels, key=lambda lv: _ORDER[lv]) if levels else PUBLIC
    if level == PUBLIC:
        return {"level": PUBLIC, "auth_required": False, "log": False,
                "pre_recipe": None, "post_suppress": None}
    if level == PROTECTED:
        return {"level": PROTECTED, "auth_required": True, "log": True,
                "pre_recipe": None, "post_suppress": {"min_n": 5}}
    return {"level": SENSITIVE, "auth_required": True, "log": True,
            "pre_recipe": {"profile": "microdata_no"},
            "post_suppress": {"min_n": 5, "secondary": True}}


class PandasProtect:
    """v1 reference ProtectionAdapter. Result-side suppression for pandas.

    `suppress` is the post-protect hook (design stage 7): it runs on the
    structured result object BEFORE it is serialized to HTML. v1 handles the
    frequency-table case (a DataFrame with an 'n' count column); other result
    types pass through unchanged. pre()/admissible() arrive in later parts.
    """

    def suppress(self, result, spec):
        if spec is None:
            return result
        try:
            import pandas as pd
        except Exception:
            return result
        if isinstance(result, pd.DataFrame) and "n" in result.columns:
            import protect as p
            out = result.copy()
            out["n"] = p.suppress(out["n"], min_n=spec["min_n"])
            return out
        return result
