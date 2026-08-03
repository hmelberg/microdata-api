"""Ren validering for /feil-endepunktet (ingen anvil-imports — pytest-bar).

Kontrakt (askstat-spec 2026-08-03-datasok-og-nedlasting-v1-design.md §1a):
endepunktet er bevisst dumt — parse + størrelsesvakt, ingen skjemavalidering
utover toppnivåform. Payloaden lagres som JSON-tekst; analyse skjer offline.
"""
import json

MAX_BYTES = 200_000


def valider_feilrapport(raw):
    """raw: bytes | None -> (ok, payload_tekst | None, feilmelding | None)."""
    if not raw:
        return False, None, "tom kropp"
    if len(raw) > MAX_BYTES:
        return False, None, f"for stor ({len(raw)} > {MAX_BYTES} bytes)"
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        return False, None, f"ugyldig JSON: {e}"
    if not isinstance(obj, dict) or not isinstance(obj.get("app"), str):
        return False, None, "toppnivå må være et objekt med app-felt (streng)"
    return True, json.dumps(obj, ensure_ascii=False), None
