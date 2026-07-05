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
    raw, _, _ = _env_raw()
    v = owner_sources.validate_registration(
        _fields(level="protected", local_mode="strict"), raw)
    assert v["local_mode"] == "strict" and v["kind"] == "encrypted_url"


def test_local_mode_strict_requires_encrypted_source():
    # V4-garantiene gjelder bare konvolutter — strict på ukryptert fil nektes
    with pytest.raises(ValueError, match="krever en kryptert kilde"):
        owner_sources.validate_registration(
            _fields(level="protected", local_mode="strict",
                    location="https://x.example/d.csv"), CSV)


def test_local_mode_invalid_refused():
    raw, _, _ = _env_raw()
    with pytest.raises(ValueError, match="local_mode"):
        owner_sources.validate_registration(_fields(local_mode="fri"), raw)


# ---- audience + HE registration (2026-07-05 follow-up) --------------------

def test_audience_field_stored_in_policy():
    raw, _, _ = _env_raw()
    v = owner_sources.validate_registration(_fields(audience="anyone"), raw)
    assert v["access_policy"]["audience"] == "anyone"
    # utelatt audience → ingen nøkkel i policy (audience_of → "listed")
    v2 = owner_sources.validate_registration(_fields(), raw)
    assert "audience" not in v2["access_policy"]


def test_audience_invalid_refused():
    raw, _, _ = _env_raw()
    with pytest.raises(ValueError, match="audience"):
        owner_sources.validate_registration(_fields(audience="alle"), raw)


def _he_artifact():
    import numpy as np
    import pandas as pd
    from safepy import he
    df = pd.DataFrame({"region": np.where(np.arange(50) < 25, "A", "B"),
                       "salary": 30000 + np.arange(50) * 100})
    ds, priv = he.encrypt_dataframe(df, value_cols=["salary"],
                                    group_cols=["region"], key_bits=256,
                                    winsorize=(0.01, 0.99))
    import json as _j
    return _j.dumps(ds).encode(), ds, _j.dumps(he.serialize_private_key(priv))


def test_he_registration_stores_wrapped_key_and_forces_remote():
    from safepy import he
    raw, ds, key_json = _he_artifact()
    v = owner_sources.validate_registration(
        _fields(level="protected", audience="anyone", he_private_key=key_json), raw)
    assert v["format"] == "he" and v["kind"] == "url"
    assert v["local_mode"] == "none" and v["default_exec"] == "remote"
    assert v["fingerprint"] == he.dataset_fingerprint(ds)
    assert v["_store_he_key"] == key_json      # ren nøkkel returneres for innpakking
    assert v["_store_key"] is None
    assert v["access_policy"]["audience"] == "anyone"


def test_he_registration_requires_key():
    raw, _, _ = _he_artifact()
    with pytest.raises(ValueError, match="he_private_key"):
        owner_sources.validate_registration(_fields(level="protected"), raw)


def test_he_registration_rejects_wrong_key():
    from safepy import he
    raw, _, _ = _he_artifact()
    _, _, other_key = _he_artifact()          # nøkkel til en annen artefakt
    with pytest.raises(ValueError, match="offentlige nøkkel"):
        owner_sources.validate_registration(
            _fields(level="protected", he_private_key=other_key), raw)
