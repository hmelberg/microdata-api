# microdata-api/server_code/source_registry.py
"""Source registry for SafeStat/safepy remote compute.

The protection level + location live HERE (server-side), keyed by source_id;
the request only references source_id. Sources come from the `sources` Data
Table (admin-managed, bytes stored as Anvil Media for kind="media"); the
hardcoded fixtures below remain as a fallback so non-Anvil test runs and a
fresh install keep working.

Future seam: kind="encrypted_url" — location points at encrypted bytes (e.g. a
GitHub repo) and the request carries a per-run decryption key that is used in
memory and never stored.
"""
from __future__ import annotations

_SOURCES = {
    # Non-public fixture: forced remote + login + suppression. (Bytes happen to
    # sit at a public URL, so this tests the EXECUTION path, not data residency.)
    "hospital_public_csv": {
        "source_id": "hospital_public_csv",
        "kind": "url",
        "location": "https://raw.githubusercontent.com/hmelberg/health-analytics-using-python/refs/heads/master/hospital.csv",
        "level": "protected",
        "default_exec": "remote",   # ignored for non-public (always remote); set for clarity
        "status": "active",
    },
    # Public fixture: small CSV, runs LOCAL by default; exec(remote) opts in.
    "demo_public_csv": {
        "source_id": "demo_public_csv",
        "kind": "url",
        "location": "https://raw.githubusercontent.com/mwaskom/seaborn-data/master/penguins.csv",
        "level": "public",
        "default_exec": "local",
        "status": "active",
    },
}


def _cell(row, name, default=None):
    """Read a column that may not exist yet on older rows (Anvil only adds
    columns on write, and reading a missing column raises)."""
    try:
        return row[name]
    except Exception:
        return default


def _row_to_source(row) -> dict:
    level = row["level"] or "protected"
    return {
        "source_id": row["source_id"],
        "kind": row["kind"] or "url",
        "location": row["location"],
        "file": row["file"],
        "format": row["format"],
        "level": level,
        "default_exec": row["default_exec"]
            or ("local" if level == "public" else "remote"),
        "encrypted": bool(_cell(row, "encrypted", False)),
        "status": "active",
    }


def resolve_source(source_id: str) -> dict:
    """Data Table first, hardcoded fixtures as fallback. Raises KeyError for
    unknown/inactive ids (callers translate that to a 404/refusal)."""
    row = None
    try:
        from anvil.tables import app_tables
        row = app_tables.sources.get(source_id=source_id)
    except Exception:
        row = None                     # table missing / non-Anvil test run
    if row is not None:
        if row["status"] != "active":
            raise KeyError(f"unknown or inactive source_id: {source_id!r}")
        return _row_to_source(row)
    src = _SOURCES.get(source_id)
    if src is None or src.get("status") != "active":
        raise KeyError(f"unknown or inactive source_id: {source_id!r}")
    return src


def load_dataframe(src: dict):
    """Resolved source dict -> pandas DataFrame.

    kind="media": bytes stored in Anvil (Media object) — never leaves the app.
    kind="url":   fetched via the shared engine reader (csv/parquet).
    Anvil-specific loading lives here (native file), never in GENERATED code.
    """
    if src.get("kind") == "media" and src.get("file") is not None:
        import io
        import pandas as pd
        data = src["file"].get_bytes()
        if src.get("encrypted"):
            from media_crypto import decrypt_bytes
            data = decrypt_bytes(data)   # plaintext exists only in memory
        buf = io.BytesIO(data)
        name = getattr(src["file"], "name", "") or ""
        fmt = src.get("format") or ("parquet" if name.endswith(".parquet") else "csv")
        return pd.read_parquet(buf) if fmt == "parquet" else pd.read_csv(buf)
    from m2py_runtime.sources import read_source
    return read_source(src["location"], src.get("format"))
