# microdata-api/server_code/owner_sources.py
"""Self-service source registration (spec 2026-07-05-encrypted-external-
sources-design.md §3; m2py page deldata.html).

Any logged-in user can register a URL source: the server fetches the bytes,
validates them (safepy-enc-v1 envelope -> kind="encrypted_url" with recomputed
fingerprint; otherwise a readable csv/parquet -> kind="url"), and stores the
row with the owner's access policy. The decryption key is stored ONLY when the
owner explicitly asks (store_key, mode 3) and is Fernet-wrapped at rest.

validate_registration() is pure (no Anvil) and unit-tested; the HTTP endpoints
below wrap it. Owners may only overwrite/deactivate their own rows.
"""
from __future__ import annotations

import datetime as dt
import io
import json

MAX_BYTES = 50 * 1024 * 1024
VALID_LEVELS = {"public", "protected", "sensitive"}
VALID_FORMATS = {"csv", "parquet"}
VALID_LOCAL_MODES = {"none", "strict", "open"}


def _utcnow():
    return dt.datetime.now(dt.timezone.utc)


def validate_registration(fields: dict, raw: bytes) -> dict:
    """fields + fetched bytes -> sources-row values (plus _store_key).
    Raises ValueError (norsk) on any problem."""
    from safepy import encfile

    sid = (fields.get("source_id") or "").strip()
    if not sid or not sid.replace("_", "").replace("-", "").isalnum():
        raise ValueError("source_id må være satt (bokstaver/tall/_/-)")
    level = (fields.get("level") or "").strip()
    if level not in VALID_LEVELS:
        raise ValueError(f"level må være en av {sorted(VALID_LEVELS)}")
    local_mode = (fields.get("local_mode") or "").strip() \
        or ("open" if level == "public" else "none")
    if local_mode not in VALID_LOCAL_MODES:
        raise ValueError(f"local_mode må være en av {sorted(VALID_LOCAL_MODES)}")
    fmt = (fields.get("format") or "csv").strip()
    if fmt not in VALID_FORMATS:
        raise ValueError(f"format må være en av {sorted(VALID_FORMATS)}")
    location = (fields.get("location") or "").strip()
    if not location.startswith(("http://", "https://")):
        raise ValueError("location må være en http(s)-URL")

    import source_access
    audience = (fields.get("audience") or "").strip() or None
    if audience is not None and audience not in source_access.VALID_AUDIENCES:
        raise ValueError(f"audience må være en av {sorted(source_access.VALID_AUDIENCES)}")

    emails = [str(e).strip().lower() for e in (fields.get("emails") or []) if str(e).strip()]
    domains = [str(d).strip().lower().lstrip("@") for d in (fields.get("domains") or []) if str(d).strip()]
    key = (fields.get("key") or "").strip() or None
    store_key = bool(fields.get("store_key")) and key is not None
    store_he = None

    try:
        env = json.loads(raw.decode("utf-8"))
    except Exception:
        env = None

    if encfile.is_envelope(env):
        kind = "encrypted_url"
        fingerprint = encfile.envelope_fingerprint(env)
        fmt = env.get("payload_format") or fmt
        if key:
            encfile.decrypt_envelope(env, key)   # raises "feil nøkkel..." if wrong
    elif isinstance(env, dict) and env.get("format") == "safepy-he-v1":
        # Homomorf kilde (Plane B): artefakten kan ligge hvor som helst; den
        # analyseres ALLTID på serveren i HE-dialekten (individdata frigis aldri),
        # så local_mode tvinges til "none". Eieren oppgir autoritets-privatnøkkelen,
        # som lagres Fernet-kryptert (he_key) og valideres mot artefaktens modulus.
        from safepy import he
        kind = "url"
        fmt = "he"
        local_mode = "none"
        fingerprint = he.dataset_fingerprint(env)
        he_key_json = (fields.get("he_private_key") or "").strip() or None
        if not he_key_json:
            raise ValueError("he-kilde krever he_private_key (autoritetsnøkkelen fra encrypt.html)")
        try:
            priv = he.load_private_key(json.loads(he_key_json))
        except Exception:
            raise ValueError("he_private_key må være gyldig nøkkel-JSON ({p, q, n} hex)")
        if format(priv.public_key.n, "x") != env["public_key"]["n"]:
            raise ValueError("he_private_key hører ikke til denne artefaktens offentlige nøkkel")
        store_he = he_key_json
    elif local_mode == "strict":
        # Strict-lokal forutsetter kryptert kilde: V4-garantiene (ingen klartekst
        # på FS, dekrypter-ved-kjøring) og per-kjørings-nøkler gjelder bare
        # safepy-enc-v1-konvolutter.
        raise ValueError("local_mode=strict krever en kryptert kilde "
                         "(safepy-enc-v1) — krypter filen først (encrypt.html)")
    else:
        kind = "url"
        fingerprint = None
        try:
            import pandas as pd
            buf = io.BytesIO(raw)
            if fmt == "parquet":
                df = pd.read_parquet(buf)
            else:
                # pandas «leser» gjerne binært søppel som én kolonne — ekte CSV
                # inneholder aldri NUL-bytes
                if b"\x00" in raw:
                    raise ValueError("binært innhold")
                df = pd.read_csv(buf)
            ok = df is not None and len(df.columns) > 0
        except Exception:
            ok = False
        if not ok:
            raise ValueError(f"kunne ikke lese filen som {fmt} (og den er ikke "
                             f"en safepy-enc-v1-fil)")

    policy = {"emails": emails, "domains": domains}
    if audience:
        policy["audience"] = audience   # utelatt → "listed" (bakoverkompatibelt)
    return {
        "source_id": sid,
        "name": (fields.get("name") or sid).strip(),
        "kind": kind,
        "location": location,
        "format": fmt,
        "level": level,
        "local_mode": local_mode,
        "default_exec": "remote" if local_mode == "none"
            else ("local" if level == "public" else "remote"),
        "fingerprint": fingerprint,
        # alltid en policy for selvregistrerte kilder: tom liste + audience «listed»
        # (default) = kun eier; audience styrer publikum forøvrig
        "access_policy": policy,
        "_store_key": key if store_key else None,
        "_store_he_key": store_he,
    }


# ---------------------------------------------------------------------------
# HTTP endpoints (Anvil). Kept below the pure logic so tests never import anvil.

try:
    import anvil.server
    from anvil.tables import app_tables
    import auth
    _ANVIL = True
except Exception:            # pure test run
    _ANVIL = False


if _ANVIL:

    def _json(body, status=200):
        return anvil.server.HttpResponse(
            status=status,
            body=json.dumps(body, ensure_ascii=False),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    def _load_body() -> dict:
        req = anvil.server.request
        body = req.body_json
        if body is None and req.body:
            try:
                body = json.loads(req.body.get_bytes().decode("utf-8"))
            except Exception:
                body = None
        return body or {}

    def _require_user():
        """Logged-in user principal (email) or an error response."""
        principal, err = auth.authenticate_or_fail()
        if err:
            return None, err
        user = auth.principal_user(principal)
        if user is None:
            return None, _json({"error": "krever innlogget bruker"}, status=403)
        return user, None

    def _audit(email, action, detail):
        try:
            app_tables.audit_log.add_row(when=_utcnow(), who=email,
                                         action=action, detail=detail)
        except Exception:
            pass  # auditing must never block the operation itself

    def _cell(row, name, default=None):
        try:
            return row[name]
        except Exception:
            return default

    def _own_summary(row):
        pol = _cell(row, "access_policy") or {}
        return {"source_id": row["source_id"],
                "name": _cell(row, "name") or row["source_id"],
                "kind": row["kind"], "location": row["location"] or "",
                "format": _cell(row, "format") or "csv", "level": row["level"],
                "local_mode": _cell(row, "local_mode") or "",
                "audience": pol.get("audience") or "listed",
                "status": row["status"],
                "has_key": bool(_cell(row, "enc_key") or _cell(row, "he_key")),
                "access_policy": pol}

    @anvil.server.http_endpoint("/sources/register", methods=["POST"],
                                cross_site_session=False, enable_cors=True)
    def http_sources_register():
        user, err = _require_user()
        if err:
            return err
        body = _load_body()

        location = (body.get("location") or "").strip()
        if not location.startswith(("http://", "https://")):
            return _json({"error": "location må være en http(s)-URL"}, status=400)
        try:
            from source_registry import _raw_bytes
            raw = _raw_bytes({"kind": "url", "location": location})
        except Exception as exc:
            return _json({"error": f"kunne ikke hente filen: {exc}"}, status=400)
        if len(raw) > MAX_BYTES:
            return _json({"error": "filen er større enn 50 MB"}, status=400)

        try:
            values = validate_registration(body, raw)
        except ValueError as exc:
            return _json({"error": str(exc)}, status=400)

        store_key = values.pop("_store_key")
        store_he = values.pop("_store_he_key", None)
        if store_key or store_he:
            from media_crypto import encrypt_bytes
            if store_key:
                values["enc_key"] = encrypt_bytes(store_key.encode("utf-8")).decode("ascii")
            if store_he:
                values["he_key"] = encrypt_bytes(store_he.encode("utf-8")).decode("ascii")
        row = app_tables.sources.get(source_id=values["source_id"])
        if row is not None and (_cell(row, "owner_email") or "") != user["email"]:
            return _json({"error": "source_id er allerede i bruk av en annen eier"},
                         status=409)
        now = _utcnow()
        values.update(status="active", updated=now, owner_email=user["email"])
        if row is None:
            app_tables.sources.add_row(created=now, **values)
        else:
            for k, v in values.items():
                row[k] = v
        _audit(user["email"], "source_register", values["source_id"])
        return _json({"ok": True, "source_id": values["source_id"],
                      "kind": values["kind"], "fingerprint": values["fingerprint"],
                      "level": values["level"]})

    @anvil.server.http_endpoint("/sources/mine", methods=["GET"],
                                cross_site_session=False, enable_cors=True)
    def http_sources_mine(**kwargs):
        user, err = _require_user()
        if err:
            return err
        rows = app_tables.sources.search(owner_email=user["email"])
        return _json({"sources": [_own_summary(r) for r in rows
                                  if r["status"] != "deleted"]})

    @anvil.server.http_endpoint("/sources/deactivate", methods=["POST"],
                                cross_site_session=False, enable_cors=True)
    def http_sources_deactivate():
        user, err = _require_user()
        if err:
            return err
        body = _load_body()
        sid = (body.get("source_id") or "").strip()
        row = app_tables.sources.get(source_id=sid)
        if row is None or (_cell(row, "owner_email") or "") != user["email"]:
            return _json({"error": f"ukjent kilde: {sid}"}, status=404)
        row["status"] = "deleted"
        row["updated"] = _utcnow()
        _audit(user["email"], "source_deactivate", sid)
        return _json({"ok": True})
