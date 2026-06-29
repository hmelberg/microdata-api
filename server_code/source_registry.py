# microdata-api/server_code/source_registry.py
"""Minimal source registry for SafeStat remote compute (v1).

The protection level + location live HERE (server-side), keyed by source_id;
the request only references source_id. v1 is a hardcoded dict with one public
source — the Anvil Data Table + admin CRUD (register/revoke/version, upload,
access policy) is the deferred admin-layer upgrade behind this same
resolve_source() seam.
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


def resolve_source(source_id: str) -> dict:
    src = _SOURCES.get(source_id)
    if src is None or src.get("status") != "active":
        raise KeyError(f"unknown or inactive source_id: {source_id!r}")
    return src
