"""Offline tests for the HE (Plane B) source path: load_encrypted_source and the
safepy_shim 'he' dialect. No Anvil, no network — sources are injected and the
at-rest key comes from the MEDIA_AT_REST_KEY env fallback (media_crypto)."""
import json
import os

import numpy as np
import pandas as pd
import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("MEDIA_AT_REST_KEY", Fernet.generate_key().decode())

import media_crypto
import source_registry
import safepy_shim
from safepy import he

KEY_BITS = 256  # test-only


class FakeMedia:
    def __init__(self, data: bytes, name="data.he.json"):
        self._data, self.name = data, name

    def get_bytes(self):
        return self._data


def _frame():
    n = 50
    idx = np.arange(n)
    return pd.DataFrame({
        "sex": np.where(idx % 2 == 0, "F", "M"),
        "region": np.where(idx < 2, "Z", np.where(idx < 26, "A", "B")),
        "salary": 30000 + idx * 1000,
    })


@pytest.fixture(scope="module")
def he_source():
    # protected level -> "standard" preset demands winsorize (0.01, 0.99), so
    # the artifact must be pre-winsorized with the same limits.
    ds, priv = he.encrypt_dataframe(_frame(), value_cols=["salary"],
                                    group_cols=["region", "sex"],
                                    key_bits=KEY_BITS, winsorize=(0.01, 0.99))
    key_json = json.dumps(he.serialize_private_key(priv))
    enc_key = media_crypto.encrypt_bytes(key_json.encode()).decode("ascii")
    raw = json.dumps(ds).encode()
    src = {
        "source_id": "he_test",
        "kind": "media",
        "file": FakeMedia(media_crypto.encrypt_bytes(raw)),
        "encrypted": True,               # at-rest encryption on top (D6)
        "format": "he",
        "level": "protected",
        "fingerprint": he.dataset_fingerprint(ds),
        "he_key": enc_key,
        "status": "active",
    }
    return ds, src


def test_load_encrypted_source_roundtrip(he_source):
    ds, src = he_source
    enc = source_registry.load_encrypted_source(src)
    assert isinstance(enc, he.EncryptedSource)
    assert enc.dataset == ds
    assert enc.private_key.public_key.n == int(ds["public_key"]["n"], 16)


def test_load_encrypted_source_rejects_swapped_file(he_source):
    ds, src = he_source
    tampered = dict(ds, n_rows=51)
    bad = dict(src, file=FakeMedia(media_crypto.encrypt_bytes(json.dumps(tampered).encode())))
    with pytest.raises(ValueError, match="fingerprint"):
        source_registry.load_encrypted_source(bad)


def test_load_encrypted_source_requires_key(he_source):
    _, src = he_source
    with pytest.raises(ValueError, match="he_key"):
        source_registry.load_encrypted_source(dict(src, he_key=None))


def _patch_registry(monkeypatch, src):
    monkeypatch.setattr(source_registry, "resolve_source",
                        lambda sid: dict(src, source_id=sid))


def test_shim_he_dialect_group_agg(he_source, monkeypatch):
    _, src = he_source
    _patch_registry(monkeypatch, src)
    out = safepy_shim.run_extended(
        "df.group_agg('region', 'salary', 'sum')",
        [{"alias": "df", "source_id": "he_test"}], dialect="he")
    assert out["err"] is None
    assert len(out["results"]) == 1 and "output-table" in out["results"][0]
    assert "·" in out["results"][0]          # region Z (n=2) suppressed
    assert out["audit"]["backend"] == "paillier"
    assert out["_audit_level"] == "protected"
    assert out["_audit_releases"]            # fingerprints collected for audit log


def test_shim_he_dialect_refuses_disclosive_code(he_source, monkeypatch):
    _, src = he_source
    _patch_registry(monkeypatch, src)
    out = safepy_shim.run_extended(
        "df.head()", [{"alias": "df", "source_id": "he_test"}], dialect="he")
    assert out["err"]
    assert "30000" not in out["err"]         # no data value in the message


def test_shim_pandas_dialect_autoroutes_encrypted(he_source, monkeypatch):
    # a user in Python mode pointing at an encrypted source: the shim silently
    # switches to the homomorphic variant (pandas -> he)
    _, src = he_source
    _patch_registry(monkeypatch, src)
    out = safepy_shim.run_extended(
        "df.groupby('region')['salary'].sum()",
        [{"alias": "df", "source_id": "he_test"}], dialect="pandas")
    assert out["err"] is None
    assert out["audit"]["backend"] == "paillier"


def test_shim_r_dialect_autoroutes_encrypted(he_source, monkeypatch):
    # R mode over an encrypted source -> r-he
    _, src = he_source
    _patch_registry(monkeypatch, src)
    out = safepy_shim.run_extended(
        "aggregate(salary ~ region, data=df, FUN=mean)",
        [{"alias": "df", "source_id": "he_test"}], dialect="r")
    assert out["err"] is None
    assert out["audit"]["dialect"] == "r-he"
    assert out["audit"]["backend"] == "paillier"


def test_shim_polars_dialect_autoroutes_encrypted(he_source, monkeypatch):
    pytest.importorskip("polars")
    _, src = he_source
    _patch_registry(monkeypatch, src)
    out = safepy_shim.run_extended(
        "import polars as pl\ndf.group_by('region').agg(pl.col('salary').mean())",
        [{"alias": "df", "source_id": "he_test"}], dialect="polars")
    assert out["err"] is None
    assert out["audit"]["backend"] == "paillier"


def test_shim_encrypted_dialect_needs_encrypted_source(monkeypatch):
    plain = {"source_id": "p", "kind": "url", "location": "https://x/y.csv",
             "format": "csv", "level": "public", "status": "active"}
    _patch_registry(monkeypatch, plain)
    out = safepy_shim.run_extended(
        "df.value_counts('region')",
        [{"alias": "df", "source_id": "p"}], dialect="he")
    assert out["err"] and "ikke kryptert" in out["err"]
