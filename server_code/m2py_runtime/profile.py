# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the m2py repo. This file is produced by sync_to_api.py.
# Edit the engine in the m2py repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""Best-effort variable metadata inference for sources without a schema.

Fills dtype + a nominal/continuous guess + cardinality. Ordinality and code-set
meaning are never inferred (they must be declared). UX/validation only — the
run-time dtype remains authoritative for actual behaviour.
"""

import pandas as pd

_CONTINUOUS_MIN_CARD = 10


def infer_schema(df) -> dict:
    out = {}
    for col in df.columns:
        s = df[col]
        if pd.api.types.is_bool_dtype(s):
            dtype = "bool"
        elif pd.api.types.is_integer_dtype(s):
            dtype = "int"
        elif pd.api.types.is_float_dtype(s):
            dtype = "float"
        elif pd.api.types.is_datetime64_any_dtype(s):
            dtype = "date"
        else:
            dtype = "string"
        card = int(s.nunique(dropna=True))
        if dtype in ("int", "float"):
            level = "continuous" if card >= _CONTINUOUS_MIN_CARD else "nominal"
        else:
            level = "nominal"
        out[col] = {"dtype": dtype, "level": level, "cardinality": card}
    return out
