# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the safepy repo. This file is produced by sync_to_api.py.
# Edit the engine in the safepy repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
# safepy/encfile.py
"""safepy-enc-v1: whole-file AES-256-GCM envelope for ordinary (non-homomorphic)
encrypted data files.

A sibling of the safepy-he-v1 format (he.py) — deliberately boring. The owner
encrypts a csv/parquet file as-is; the ciphertext may live at any URL (the host
needs zero trust); the fingerprint is registered server-side so a swapped file
is refused. The JS twin (m2py/js/enc-crypto.js) is the production encryptor;
tests/fixtures_enc/ locks the wire format between the two.

Envelope: {"format": "safepy-enc-v1", "cipher": "AES-256-GCM",
           "payload_format": "csv"|"parquet", "iv": b64(12 bytes),
           "ciphertext": b64, "fingerprint": sha256hex(ciphertext bytes)}
Key: 32 bytes as base64url without padding (~43 chars).
"""
from __future__ import annotations

import base64
import hashlib
import os

FORMAT = "safepy-enc-v1"
_CIPHER = "AES-256-GCM"
_PAYLOAD_FORMATS = {"csv", "parquet"}


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def _key_bytes(key: str) -> bytes:
    s = (key or "").strip()
    try:
        raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except Exception:
        raise ValueError("ugyldig nøkkel (må være base64url)")
    if len(raw) != 32:
        raise ValueError("ugyldig nøkkel (må være 256 bit base64url)")
    return raw


def encrypt_bytes(data: bytes, payload_format: str, key: str | None = None):
    """Encrypt a whole file. Returns (envelope_dict, key_str)."""
    if payload_format not in _PAYLOAD_FORMATS:
        raise ValueError(f"payload_format må være en av {sorted(_PAYLOAD_FORMATS)}")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = key or generate_key()
    iv = os.urandom(12)
    ct = AESGCM(_key_bytes(key)).encrypt(iv, data, None)
    env = {
        "format": FORMAT,
        "cipher": _CIPHER,
        "payload_format": payload_format,
        "iv": _b64e(iv),
        "ciphertext": _b64e(ct),
        "fingerprint": hashlib.sha256(ct).hexdigest(),
    }
    return env, key


def is_envelope(obj) -> bool:
    return isinstance(obj, dict) and obj.get("format") == FORMAT


def envelope_fingerprint(env: dict) -> str:
    """sha256 hex RECOMPUTED from the ciphertext bytes (never trust the field)."""
    return hashlib.sha256(_b64d(env["ciphertext"])).hexdigest()


def decrypt_envelope(env: dict, key: str) -> bytes:
    """Envelope + key -> plaintext bytes. GCM guarantees no partial decrypt."""
    if not is_envelope(env):
        raise ValueError("ikke en safepy-enc-v1-fil")
    if env.get("cipher") != _CIPHER:
        raise ValueError(f"ukjent cipher: {env.get('cipher')!r}")
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        return AESGCM(_key_bytes(key)).decrypt(_b64d(env["iv"]), _b64d(env["ciphertext"]), None)
    except InvalidTag:
        raise ValueError("feil nøkkel eller ødelagt fil")
