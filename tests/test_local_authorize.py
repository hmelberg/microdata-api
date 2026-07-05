"""Per-run authorization for local strict runs (spec V3): every run is
policy-checked and logged; keys flow ONLY through this path for strict."""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("MEDIA_AT_REST_KEY", Fernet.generate_key().decode())

import media_crypto
import source_access


def _src(**kw):
    base = {"source_id": "s", "kind": "encrypted_url",
            "location": "https://x.example/e.json", "format": "csv",
            "level": "protected", "local_mode": "strict",
            "fingerprint": "abc", "enc_key": None,
            "access_policy": {"emails": ["ana@fhi.no"], "domains": []},
            "owner_email": "eier@fhi.no", "status": "active"}
    base.update(kw)
    return base


def test_authorize_denied_wrong_email():
    ok, keys, level = source_access.authorize_local_run([_src()], "x@y.no")
    assert not ok and keys == {}


def test_authorize_denied_local_mode_none():
    ok, _, _ = source_access.authorize_local_run(
        [_src(local_mode="none")], "ana@fhi.no")
    assert not ok


def test_authorize_releases_stored_keys_and_level():
    wrapped = media_crypto.encrypt_bytes(b"K1").decode("ascii")
    ok, keys, level = source_access.authorize_local_run(
        [_src(enc_key=wrapped)], "ana@fhi.no")
    assert ok and keys == {"s": "K1"} and level == "protected"


def test_authorize_mode2_no_stored_key_still_ok():
    ok, keys, level = source_access.authorize_local_run([_src()], "ana@fhi.no")
    assert ok and keys == {}          # analytikeren har nøkkelen selv (mode 2)


def test_authorize_mixed_level_most_restrictive():
    wrapped = media_crypto.encrypt_bytes(b"K1").decode("ascii")
    ok, _, level = source_access.authorize_local_run(
        [_src(enc_key=wrapped), _src(source_id="p", level="sensitive")],
        "ana@fhi.no")
    assert ok and level == "sensitive"
