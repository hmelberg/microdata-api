"""Kodehash for magic-/delt-koder (askstat konto-runden fase 2, askstat-spec
2026-08-05-konto-runden §Fase 2d): PBKDF2-600k med FAST salt — gjør
DB-dump-knekking av 3-ords koder (~39 bits) dyr, mens verify forblir ett
PBKDF2-kall + indeksert radoppslag. Sesjonstokens (høy entropi, prefiks
mdapi_) beholder sha256 i auth.py. Fast salt er akseptabelt: iterasjonstallet
er forsvaret, og kodene er tilfeldige (ingen rainbow-gevinst på tvers).

normalize_code speiles byte-for-byte i askstats js/keys-crypto.js (KEK-
avledning fra samme kode) — endres reglene her, endres de der."""
import hashlib
import re

CODE_SALT = b"mdataapi-code-salt-v1"
ITERATIONS = 600_000


def normalize_code(raw: str) -> str:
    """Lowercase, runs av ikke-bokstaver → én bindestrek, strip bindestreker.
    'Abacus Charity Twelve' → 'abacus-charity-twelve'."""
    s = (raw or "").lower().strip()
    s = re.sub(r"[^a-z]+", "-", s)
    return s.strip("-")


def hash_code(raw: str) -> str:
    """PBKDF2-hex av NORMALISERT kode — brukes ved både utstedelse og verify."""
    normalized = normalize_code(raw)
    return hashlib.pbkdf2_hmac(
        "sha256", normalized.encode("utf-8"), CODE_SALT, ITERATIONS
    ).hex()
