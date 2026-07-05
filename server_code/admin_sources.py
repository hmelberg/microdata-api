# microdata-api/server_code/admin_sources.py
"""Admin CRUD for the `sources` Data Table (Milestone 2).

Called from the Anvil-native AdminSources form via anvil.server.call. Auth is
the Anvil Users service session (the users table is shared with the magic-link
flow, which sets enabled/confirmed_email); every callable requires
user["is_admin"].

Non-public uploads are encrypted at rest (media_crypto, design D6): the stored
Media holds Fernet ciphertext, `encrypted=True` on the row, and
source_registry.load_dataframe decrypts in memory. Uploads are validated by
round-tripping through load_dataframe (the real read path) before the row is
activated.
"""
from __future__ import annotations

import datetime as dt

import anvil.server
import anvil.users
from anvil import BlobMedia
from anvil.tables import app_tables

VALID_KINDS = {"url", "media", "encrypted_url"}
VALID_FORMATS = {"csv", "parquet", "he"}   # "he" = safepy-he-v1 encrypted artifact
VALID_LEVELS = {"public", "protected", "sensitive"}
VALID_EXEC = {"local", "remote", "strict_remote"}


def _utcnow():
    return dt.datetime.now(dt.timezone.utc)


def _require_admin():
    user = anvil.users.get_user()
    if user is None or not user["is_admin"]:
        raise anvil.server.PermissionDenied("admin access required")
    return user


def _cell(row, name, default=None):
    try:
        return row[name]
    except Exception:
        return default


def _audit(user, action: str, detail: str):
    try:
        app_tables.audit_log.add_row(
            when=_utcnow(), who=user["email"], action=action, detail=detail)
    except Exception:
        pass  # auditing must never block the operation itself


def _row_summary(row) -> dict:
    return {
        "source_id": row["source_id"],
        "name": _cell(row, "name") or row["source_id"],
        "description": _cell(row, "description") or "",
        "kind": row["kind"] or "url",
        "location": row["location"] or "",
        "format": _cell(row, "format") or "csv",
        "level": row["level"] or "protected",
        "default_exec": row["default_exec"] or "",
        "status": row["status"] or "active",
        "encrypted": bool(_cell(row, "encrypted", False)),
        "has_file": row["file"] is not None,
        "owner_email": _cell(row, "owner_email") or "",
        "updated": (_cell(row, "updated") or _cell(row, "created") or None),
    }


@anvil.server.callable
def admin_list_sources():
    _require_admin()
    rows = sorted(app_tables.sources.search(), key=lambda r: r["source_id"] or "")
    return [_row_summary(r) for r in rows]


def _validate_fields(fields: dict) -> dict:
    src_id = (fields.get("source_id") or "").strip()
    if not src_id or not src_id.replace("_", "").replace("-", "").isalnum():
        raise ValueError("source_id må være satt (bokstaver/tall/_/-)")
    kind = (fields.get("kind") or "url").strip()
    if kind not in VALID_KINDS:
        raise ValueError(f"kind må være en av {sorted(VALID_KINDS)}")
    fmt = (fields.get("format") or "csv").strip()
    if fmt not in VALID_FORMATS:
        raise ValueError(f"format må være en av {sorted(VALID_FORMATS)}")
    level = (fields.get("level") or "protected").strip()
    if level not in VALID_LEVELS:
        raise ValueError(f"level må være en av {sorted(VALID_LEVELS)}")
    default_exec = (fields.get("default_exec") or "").strip() \
        or ("local" if level == "public" else "remote")
    if default_exec not in VALID_EXEC:
        raise ValueError(f"default_exec må være en av {sorted(VALID_EXEC)}")
    location = (fields.get("location") or "").strip()
    if kind == "url" and not location.startswith(("http://", "https://")):
        raise ValueError("kind=url krever en http(s)-location")
    return {
        "source_id": src_id,
        "name": (fields.get("name") or src_id).strip(),
        "description": (fields.get("description") or "").strip(),
        "kind": kind,
        "location": location,
        "format": fmt,
        "level": level,
        "default_exec": default_exec,
    }


def _validate_he_source(f, media, encrypted, he_key_json, existing_row):
    """Plane B registration: parse the safepy-he-v1 artifact (uploaded media or
    fetched from the URL), record its content fingerprint, and store the
    authority private key Fernet-encrypted. The key must match the artifact's
    public modulus; on update the existing key is kept if none is supplied.
    The raw key JSON never touches the row or the audit log."""
    import json
    from source_registry import _raw_bytes
    from safepy import he

    probe = {"kind": f["kind"], "file": media, "location": f["location"],
             "format": "he", "encrypted": encrypted, "source_id": f["source_id"]}
    if f["kind"] == "media" and media is None and existing_row is not None:
        probe["file"] = existing_row["file"]
        probe["encrypted"] = bool(_cell(existing_row, "encrypted", False))
    try:
        ds = json.loads(_raw_bytes(probe).decode("utf-8"))
    except Exception:
        raise ValueError("kunne ikke lese kilden som en safepy-he-v1 JSON-artefakt")
    if not isinstance(ds, dict) or ds.get("format") != "safepy-he-v1":
        raise ValueError("format=he krever en safepy-he-v1-artefakt "
                         f"(fant: {ds.get('format') if isinstance(ds, dict) else 'ikke-JSON'})")

    if he_key_json:
        try:
            key_dict = json.loads(he_key_json)
            priv = he.load_private_key(key_dict)
        except Exception:
            raise ValueError("he_private_key må være gyldig nøkkel-JSON ({p, q, n} hex)")
        if format(priv.public_key.n, "x") != ds["public_key"]["n"]:
            raise ValueError("he_private_key hører ikke til denne artefaktens "
                             "offentlige nøkkel (modulus stemmer ikke)")
        from media_crypto import encrypt_bytes
        stored_key = encrypt_bytes(he_key_json.encode("utf-8")).decode("ascii")
    elif existing_row is not None and _cell(existing_row, "he_key"):
        stored_key = existing_row["he_key"]        # update without re-supplying
    else:
        raise ValueError("ny he-kilde krever he_private_key (autoritetsnøkkelen)")

    return {"fingerprint": he.dataset_fingerprint(ds),
            "he_key": stored_key,
            "nrows": int(ds.get("n_rows") or 0)}


@anvil.server.callable
def admin_save_source(fields: dict, file=None):
    """Create or update a source (matched on source_id). For kind=media with a
    new upload: validate by parsing, encrypt when level != public, then store.
    Returns the saved row summary."""
    user = _require_admin()
    f = _validate_fields(fields or {})

    media, encrypted, nrows = None, False, None
    if file is not None:
        if f["kind"] != "media":
            raise ValueError("filopplasting krever kind=media")
        raw = file.get_bytes()
        if not raw:
            raise ValueError("tom fil")
        if len(raw) > 50 * 1024 * 1024:
            raise ValueError("filen er større enn 50 MB")
        encrypted = f["level"] != "public"
        stored = raw
        if encrypted:
            from media_crypto import encrypt_bytes
            stored = encrypt_bytes(raw)
        name = getattr(file, "name", None) or f"{f['source_id']}.{f['format']}"
        media = BlobMedia("application/octet-stream", stored, name=name)
        if f["format"] != "he":
            # Validate through the REAL read path (incl. decryption) before saving.
            from source_registry import load_dataframe
            df = load_dataframe({"kind": "media", "file": media,
                                 "format": f["format"], "encrypted": encrypted})
            if df is None or len(df.columns) == 0:
                raise ValueError("kunne ikke lese filen som " + f["format"])
            nrows = int(len(df))

    he_values = {}
    if f["format"] == "he":
        existing = app_tables.sources.get(source_id=f["source_id"])
        he_values = _validate_he_source(
            f, media, encrypted, (fields or {}).get("he_private_key"), existing)
        nrows = he_values.pop("nrows")

    row = app_tables.sources.get(source_id=f["source_id"])
    now = _utcnow()
    values = dict(f, status="active", updated=now,
                  owner_email=user["email"], **he_values)
    if media is not None:
        values["file"] = media
        values["encrypted"] = encrypted
    if row is None:
        if f["kind"] == "media" and media is None:
            raise ValueError("ny media-kilde krever en fil")
        app_tables.sources.add_row(created=now, **values)
        row = app_tables.sources.get(source_id=f["source_id"])
        _audit(user, "source_create", f["source_id"])
    else:
        for k, v in values.items():
            row[k] = v
        _audit(user, "source_update", f["source_id"])

    out = _row_summary(row)
    if nrows is not None:
        out["nrows"] = nrows
    return out


@anvil.server.callable
def admin_delete_source(source_id: str):
    """Soft delete: status='deleted' (resolve_source already filters on it)."""
    user = _require_admin()
    row = app_tables.sources.get(source_id=(source_id or "").strip())
    if row is None:
        raise ValueError(f"ukjent kilde: {source_id!r}")
    row["status"] = "deleted"
    row["updated"] = _utcnow()
    _audit(user, "source_delete", source_id)
    return {"ok": True}


@anvil.server.callable
def admin_restore_source(source_id: str):
    user = _require_admin()
    row = app_tables.sources.get(source_id=(source_id or "").strip())
    if row is None:
        raise ValueError(f"ukjent kilde: {source_id!r}")
    row["status"] = "active"
    row["updated"] = _utcnow()
    _audit(user, "source_restore", source_id)
    return {"ok": True}
