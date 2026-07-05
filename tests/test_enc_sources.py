"""Offline tests for safepy-enc-v1 sources (kind="encrypted_url"): the
load_dataframe decrypt path and safepy_shim per-run key passthrough.
No Anvil, no network — _raw_bytes is monkeypatched."""
import json
import os

import pandas as pd
import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("MEDIA_AT_REST_KEY", Fernet.generate_key().decode())

import media_crypto
import source_registry
from safepy import encfile


def _envelope():
    df = pd.DataFrame({"region": ["A"] * 30 + ["B"] * 30,
                       "salary": [30000 + i * 100 for i in range(60)]})
    env, key = encfile.encrypt_bytes(df.to_csv(index=False).encode(), "csv")
    return df, env, key


@pytest.fixture()
def enc_source(monkeypatch):
    df, env, key = _envelope()
    src = {
        "source_id": "enc_test",
        "kind": "encrypted_url",
        "location": "https://example.org/data.enc.json",
        "file": None,
        "format": "csv",
        "level": "public",
        "default_exec": "local",
        "encrypted": True,
        "fingerprint": encfile.envelope_fingerprint(env),
        "enc_key": media_crypto.encrypt_bytes(key.encode()).decode("ascii"),
        "access_policy": {"emails": ["ana@fhi.no"], "domains": []},
        "owner_email": "eier@fhi.no",
        "status": "active",
    }
    monkeypatch.setattr(source_registry, "_raw_bytes",
                        lambda s: json.dumps(env).encode())
    return df, env, key, src


def test_load_dataframe_stored_key(enc_source):
    df, _, _, src = enc_source
    out = source_registry.load_dataframe(src)
    assert list(out.columns) == ["region", "salary"]
    assert len(out) == 60


def test_load_dataframe_run_key_overrides(enc_source):
    _, _, key, src = enc_source
    out = source_registry.load_dataframe(dict(src, enc_key=None, _run_key=key))
    assert len(out) == 60


def test_load_dataframe_missing_key(enc_source):
    _, _, _, src = enc_source
    with pytest.raises(ValueError, match="nøkkel"):
        source_registry.load_dataframe(dict(src, enc_key=None))


def test_load_dataframe_wrong_run_key(enc_source):
    _, _, _, src = enc_source
    with pytest.raises(ValueError, match="feil nøkkel eller ødelagt fil"):
        source_registry.load_dataframe(
            dict(src, enc_key=None, _run_key=encfile.generate_key()))


def test_load_dataframe_refuses_swapped_file(enc_source, monkeypatch):
    _, _, key, src = enc_source
    env2, _ = encfile.encrypt_bytes(b"a,b\n1,2\n", "csv", key)
    monkeypatch.setattr(source_registry, "_raw_bytes",
                        lambda s: json.dumps(env2).encode())
    with pytest.raises(ValueError, match="fingerprint"):
        source_registry.load_dataframe(src)


def test_load_dataframe_not_an_envelope(enc_source, monkeypatch):
    _, _, _, src = enc_source
    monkeypatch.setattr(source_registry, "_raw_bytes", lambda s: b"a,b\n1,2\n")
    with pytest.raises(ValueError, match="safepy-enc-v1"):
        source_registry.load_dataframe(src)
