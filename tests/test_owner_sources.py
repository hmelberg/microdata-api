"""Pure tests for self-service registration validation (spec §3, deldata)."""
import os

import pandas as pd
import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("MEDIA_AT_REST_KEY", Fernet.generate_key().decode())

import owner_sources
from safepy import encfile

CSV = b"kommune,alder\nOslo,44\nBergen,37\n"


def _fields(**kw):
    base = {"source_id": "helse2025", "name": "Helse 2025",
            "location": "https://raw.githubusercontent.com/x/y/main/d.enc.json",
            "level": "public", "format": "csv",
            "emails": ["ana@fhi.no"], "domains": ["uio.no"],
            "key": None, "store_key": False}
    base.update(kw)
    return base


def _env_raw(key=None):
    import json
    env, k = encfile.encrypt_bytes(CSV, "csv", key)
    return json.dumps(env).encode(), env, k


def test_encrypted_registration_mode2():
    raw, env, _ = _env_raw()
    v = owner_sources.validate_registration(_fields(), raw)
    assert v["kind"] == "encrypted_url"
    assert v["fingerprint"] == encfile.envelope_fingerprint(env)
    assert v["access_policy"] == {"emails": ["ana@fhi.no"], "domains": ["uio.no"]}
    assert v["_store_key"] is None
    assert v["level"] == "public" and v["default_exec"] == "local"


def test_encrypted_registration_mode3_stores_key():
    raw, _, key = _env_raw()
    v = owner_sources.validate_registration(_fields(key=key, store_key=True), raw)
    assert v["_store_key"] == key


def test_supplied_key_is_verified_against_file():
    raw, _, _ = _env_raw()
    with pytest.raises(ValueError, match="feil nøkkel"):
        owner_sources.validate_registration(
            _fields(key=encfile.generate_key(), store_key=True), raw)


def test_plain_csv_registration_protected_level():
    v = owner_sources.validate_registration(
        _fields(level="protected", location="https://x.example/d.csv"), CSV)
    assert v["kind"] == "url" and v["fingerprint"] is None
    assert v["level"] == "protected" and v["default_exec"] == "remote"


def test_unreadable_plain_file_refused():
    with pytest.raises(ValueError, match="kunne ikke lese"):
        owner_sources.validate_registration(_fields(), b"\x00\x01ikke-data")


def test_bad_level_refused():
    raw, _, _ = _env_raw()
    with pytest.raises(ValueError, match="level"):
        owner_sources.validate_registration(_fields(level="hemmelig"), raw)


def test_bad_source_id_refused():
    raw, _, _ = _env_raw()
    with pytest.raises(ValueError, match="source_id"):
        owner_sources.validate_registration(_fields(source_id="x y!"), raw)


def test_http_url_required():
    raw, _, _ = _env_raw()
    with pytest.raises(ValueError, match="http"):
        owner_sources.validate_registration(_fields(location="ftp://x/d"), raw)


def test_policy_normalized_lowercase():
    raw, _, _ = _env_raw()
    v = owner_sources.validate_registration(
        _fields(emails=[" Ana@FHI.no "], domains=["@UiO.no"]), raw)
    assert v["access_policy"] == {"emails": ["ana@fhi.no"], "domains": ["uio.no"]}


def test_local_mode_default_by_level():
    raw, _, _ = _env_raw()
    assert owner_sources.validate_registration(_fields(), raw)["local_mode"] == "open"
    v = owner_sources.validate_registration(
        _fields(level="protected", location="https://x.example/d.csv"), CSV)
    assert v["local_mode"] == "none"


def test_local_mode_explicit_strict_on_protected():
    v = owner_sources.validate_registration(
        _fields(level="protected", local_mode="strict",
                location="https://x.example/d.csv"), CSV)
    assert v["local_mode"] == "strict"


def test_local_mode_invalid_refused():
    raw, _, _ = _env_raw()
    with pytest.raises(ValueError, match="local_mode"):
        owner_sources.validate_registration(_fields(local_mode="fri"), raw)
