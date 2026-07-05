# microdata-api/server_code/source_registry.py
"""Source registry for SafeStat/safepy remote compute.

The protection level + location live HERE (server-side), keyed by source_id;
the request only references source_id. Sources come from the `sources` Data
Table (admin-managed, bytes stored as Anvil Media for kind="media"); the
hardcoded fixtures below remain as a fallback so non-Anvil test runs and a
fresh install keep working.

kind="encrypted_url" (spec m2py docs/superpowers/specs/2026-07-05-encrypted-
external-sources-design.md): location points at a safepy-enc-v1 envelope (e.g.
in a GitHub repo). The key comes per-run from the request (_run_key, mode 2 —
never stored) or Fernet-unwrapped from the row (enc_key, mode 3); plaintext
exists only in memory.

format="he" (Plane B, safepy homomorphic-release design): the source is a
Paillier-encrypted JSON artifact (safepy-he-v1) that may live at any URL — the
host needs zero trust. `load_encrypted_source` fingerprint-checks it against
the registered hash and pairs it with the authority private key stored
Fernet-encrypted on the row (`he_key`).
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
        "fingerprint": _cell(row, "fingerprint"),
        "he_key": _cell(row, "he_key"),
        "enc_key": _cell(row, "enc_key"),
        "access_policy": _cell(row, "access_policy"),
        "owner_email": _cell(row, "owner_email") or "",
        "name": _cell(row, "name") or row["source_id"],
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
    if src.get("kind") == "encrypted_url":
        return _load_enc_envelope(src)
    from m2py_runtime.sources import read_source
    return read_source(src["location"], src.get("format"))


def _load_enc_envelope(src: dict):
    """kind="encrypted_url": location holds a safepy-enc-v1 envelope. The key
    comes per-run (src["_run_key"], from the request) or Fernet-unwrapped from
    the row (enc_key). Plaintext exists only in memory (spec §5)."""
    import io
    import json
    import pandas as pd
    from safepy import encfile

    try:
        env = json.loads(_raw_bytes(src).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        env = None
    if not encfile.is_envelope(env):
        raise ValueError(
            f"kilden {src.get('source_id')!r} er ikke en safepy-enc-v1-fil")
    want = src.get("fingerprint")
    if want and encfile.envelope_fingerprint(env) != want:
        raise ValueError(
            f"kilden {src.get('source_id')!r} matcher ikke registrert fingerprint "
            f"— filen kan være byttet ut siden registrering")
    key = src.get("_run_key")
    if not key and src.get("enc_key"):
        from media_crypto import decrypt_bytes
        key = decrypt_bytes(src["enc_key"].encode("ascii")).decode("ascii")
    if not key:
        raise ValueError(
            f"kilden {src.get('source_id')!r} krever dekrypteringsnøkkel "
            f"(key(...) i scriptet, eller nøkkel lagret ved registrering)")
    data = encfile.decrypt_envelope(env, key)
    buf = io.BytesIO(data)
    fmt = env.get("payload_format") or src.get("format") or "csv"
    return pd.read_parquet(buf) if fmt == "parquet" else pd.read_csv(buf)


def _raw_bytes(src: dict) -> bytes:
    """Raw bytes of a source (media or url), at-rest-decrypted when flagged."""
    if src.get("kind") == "media" and src.get("file") is not None:
        data = src["file"].get_bytes()
        if src.get("encrypted"):
            from media_crypto import decrypt_bytes
            data = decrypt_bytes(data)   # plaintext exists only in memory
        return data
    import urllib.request
    with urllib.request.urlopen(src["location"]) as resp:
        return resp.read()


def load_encrypted_source(src: dict):
    """format="he" source dict -> safepy.he.EncryptedSource (dataset + key).

    The artifact is fingerprint-checked against the registered hash, so a
    swapped file (e.g. edited on GitHub) is refused. The authority private key
    is Fernet-decrypted from the row and exists only in memory."""
    import json
    from safepy import he
    ds = json.loads(_raw_bytes(src).decode("utf-8"))
    want = src.get("fingerprint")
    if want and he.dataset_fingerprint(ds) != want:
        raise ValueError(
            f"kilden {src.get('source_id')!r} matcher ikke registrert fingerprint "
            f"— filen kan være byttet ut siden registrering")
    enc_key = src.get("he_key")
    if not enc_key:
        raise ValueError(
            f"kilden {src.get('source_id')!r} mangler autoritetsnøkkel (he_key)")
    from media_crypto import decrypt_bytes
    key_dict = json.loads(decrypt_bytes(enc_key.encode("ascii")).decode("utf-8"))
    return he.EncryptedSource(ds, he.load_private_key(key_dict))
