"""Kontosynk av nøkkellageret (safestat key store fase 2; spec §3.5 i
safestat-repoets docs/superpowers/specs/2026-07-30-key-store-design.md).

Zero-knowledge: serveren lagrer md_keys-dokumentet AS-IS. locked/secret-poster
er AES-GCM-ciphertext under brukerens hovedpassord, som aldri forlater
klienten; kun open-poster er lesbare her. Newest-wins på `updated` gjøres av
KLIENTEN (ISO-strenger sammenlignes leksikografisk) — serveren lagrer bare.

  GET  /keystore                    → {"doc": str|null, "updated": str|null}
  POST /keystore  {"doc": "<json>"} → {"ok": true, "updated": str}
"""

from __future__ import annotations

import json

MAX_DOC_BYTES = 65536


def validate_doc(raw) -> str:
    """Valider klientens md_keys-dokument; returner `updated` (ISO-streng).
    Kaster ValueError med norsk melding ved ugyldig dokument."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("doc mangler")
    if len(raw.encode("utf-8")) > MAX_DOC_BYTES:
        raise ValueError(f"dokumentet er for stort (maks {MAX_DOC_BYTES // 1024} kB)")
    try:
        doc = json.loads(raw)
    except Exception:
        raise ValueError("doc er ikke gyldig JSON")
    if not isinstance(doc, dict) or doc.get("v") != 2:
        raise ValueError("doc må være md_keys v2")
    if not isinstance(doc.get("entries"), dict):
        raise ValueError("doc mangler entries")
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

    @anvil.server.http_endpoint("/keystore", methods=["GET"],
                                cross_site_session=False, enable_cors=True)
    def http_keystore_get():
        user, err = _require_user()
        if err:
            return err
        row = app_tables.keystores.get(email=user["email"])
        if row is None:
            return _json({"doc": None, "updated": None})
        return _json({"doc": row["doc"], "updated": row["updated"]})

    @anvil.server.http_endpoint("/keystore", methods=["POST", "PUT"],
                                cross_site_session=False, enable_cors=True)
    def http_keystore_put():
        user, err = _require_user()
        if err:
            return err
        body = _load_body()
        try:
            updated = validate_doc(body.get("doc"))
        except ValueError as exc:
            return _json({"error": str(exc)}, status=400)
        row = app_tables.keystores.get(email=user["email"])
        if row is None:
            app_tables.keystores.add_row(email=user["email"],
                                         doc=body["doc"], updated=updated)
        else:
            row.update(doc=body["doc"], updated=updated)
        return _json({"ok": True, "updated": updated})
