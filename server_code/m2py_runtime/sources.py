# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the m2py repo. This file is produced by sync_to_api.py.
# Edit the engine in the m2py repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""Source reader seam: location + format -> DataFrame / LazyFrame.

Reads csv/parquet via the native pandas/polars readers. DuckDB-backed reading
(url/sql/duckdb and larger-than-memory) is a named follow-on that plugs into this
same interface.
"""

import pandas as pd

from .manifest import _format_from


def read_source(location, fmt=None):
    fmt = _format_from(location, fmt)
    if fmt == "parquet":
        return pd.read_parquet(location)
    if fmt == "csv":
        return pd.read_csv(location)
    raise NotImplementedError(
        f"source format {fmt!r} needs the DuckDB-backed reader (follow-on)")


def scan_source(location, fmt=None):
    import polars as pl
    fmt = _format_from(location, fmt)
    if fmt == "parquet":
        return pl.scan_parquet(location)
    if fmt == "csv":
        return pl.scan_csv(location)
    raise NotImplementedError(
        f"source format {fmt!r} needs the DuckDB-backed reader (follow-on)")
