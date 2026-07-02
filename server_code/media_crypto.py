# microdata-api/server_code/media_crypto.py
"""Encryption at rest for uploaded dataset Media (design D6, plan-integration.md).

Non-public uploads are Fernet-encrypted before the BlobMedia is stored and
decrypted in source_registry.load_dataframe. The key lives in the Anvil secret
`media_at_rest_key` (generate once with Fernet.generate_key()).

Honest threat model: this does NOT survive full Anvil-account compromise (key
and ciphertext share one trust domain) — it protects against Data Table/Media
exports, app clones, and storage-level exposure without Secrets. The future
external-per-run-key variant changes only where the key comes from.

Local (non-Anvil) test runs may set MEDIA_AT_REST_KEY in the environment.
"""
from __future__ import annotations

import os

_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
    key = None
    try:
        import anvil.secrets
        key = anvil.secrets.get_secret("media_at_rest_key")
    except Exception:
        key = None                      # non-Anvil test run
    key = key or os.environ.get("MEDIA_AT_REST_KEY")
    if not key:
        raise RuntimeError(
            "media_at_rest_key is not configured (Anvil secret, or "
            "MEDIA_AT_REST_KEY env var for local tests). Generate one with "
            "cryptography.fernet.Fernet.generate_key()."
        )
    from cryptography.fernet import Fernet
    _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt_bytes(data: bytes) -> bytes:
    return _get_fernet().encrypt(data)


def decrypt_bytes(data: bytes) -> bytes:
    return _get_fernet().decrypt(data)
