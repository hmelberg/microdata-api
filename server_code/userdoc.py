"""Generisk per-bruker-dokumentlagring (askstat konto-runden fase 2, askstat-
spec 2026-08-05-konto-runden §Fase 2b). Ett endepunktpar dekker profiles/
history/askkeys — klienten eier semantikken (askkeys-innholdet er AES-GCM-
ciphertext under brukerens login-kode; serveren lagrer AS-IS og kan ikke lese
det). Merge gjøres av KLIENTEN (union-by-id + tombstones); serveren lagrer
bare siste innsendte dokument. Delt-kode-økter har ingen brukerrad → 403 →
ingen synk (bevisst).

  GET  /userdoc/:name                → {"doc": str|null, "updated": str|null}
  POST /userdoc/:name {"doc":"..."}  → {"ok": true, "updated": str}

Tabell `userdocs` (opprettes manuelt i Anvil-editoren ved pull):
  email: text, name: text, doc: text, updated: text — én rad per (email, name).
"""
from __future__ import annotations
import anvil.microsoft.auth

import json

CAPS = {"askkeys": 65536, "profiles": 131072, "history": 262144}


def validate_userdoc(name, raw) -> str:
    """Minimal validering (allowlist, cap, JSON-objekt m/updated); returnerer
    `updated`. Kaster ValueError med norsk melding ved ugyldig dokument."""
    if name not in CAPS:
        raise ValueError(f"ukjent dokumentnavn: {name}")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("doc mangler")
    if len(raw.encode("utf-8")) > CAPS[name]:
        raise ValueError(f"dokumentet er for stort (maks {CAPS[name] // 1024} kB)")
    try:
        doc = json.loads(raw)
    except Exception:
        raise ValueError("doc er ikke gyldig JSON")
    if not isinstance(doc, dict):
        raise ValueError("doc må være et JSON-objekt")
    updated = doc.get("updated")
    if not isinstance(updated, str) or not updated:
        raise ValueError("doc mangler updated-tidsstempel")
    return updated


# ---------------------------------------------------------------------------
# HTTP endpoints (Anvil). Kept below the pure logic so tests never import anvil.

try:
    import anvil.server
    from anvil.tables import app_tables
    import auth
    import http_utils
    _ANVIL = True
except Exception:            # ren testkjøring
    _ANVIL = False


if _ANVIL:
    _json = http_utils.json_response
    _load_body = http_utils.load_body

    def _require_user():
        principal, err = auth.authenticate_or_fail()
        if err:
            return None, err
        user = auth.principal_user(principal)
        if user is None:
            return None, _json({"error": "krever innlogget bruker"}, status=403)
        return user, None

    @anvil.server.http_endpoint("/userdoc/:name", methods=["GET"],
                                cross_site_session=False, enable_cors=True)
    def http_userdoc_get(name):
        user, err = _require_user()
        if err:
            return err
        if name not in CAPS:
            return _json({"error": "ukjent dokumentnavn"}, status=404)
        row = app_tables.userdocs.get(email=user["email"], name=name)
        if row is None:
            return _json({"doc": None, "updated": None})
        return _json({"doc": row["doc"], "updated": row["updated"]})

    @anvil.server.http_endpoint("/userdoc/:name", methods=["POST", "PUT"],
                                cross_site_session=False, enable_cors=True)
    def http_userdoc_put(name):
        user, err = _require_user()
        if err:
            return err
        body = _load_body()
        try:
            updated = validate_userdoc(name, body.get("doc"))
        except ValueError as exc:
            return _json({"error": str(exc)}, status=400)
        row = app_tables.userdocs.get(email=user["email"], name=name)
        if row is None:
            app_tables.userdocs.add_row(email=user["email"], name=name,
                                        doc=body["doc"], updated=updated)
        else:
            row.update(doc=body["doc"], updated=updated)
        return _json({"ok": True, "updated": updated})
