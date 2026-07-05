"""Pure tests for the /source_access decision (spec §3): who gets what.
No Anvil — media_crypto uses the MEDIA_AT_REST_KEY env fallback."""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("MEDIA_AT_REST_KEY", Fernet.generate_key().decode())

import media_crypto
import source_access


def _src(**kw):
    base = {"source_id": "s", "kind": "encrypted_url",
            "location": "https://x.example/e.json", "format": "csv",
            "level": "public", "fingerprint": "abc123", "enc_key": None,
            "access_policy": {"emails": ["ana@fhi.no"], "domains": ["uio.no"]},
            "owner_email": "eier@fhi.no", "status": "active"}
    base.update(kw)
    return base


def test_denied_wrong_email():
    assert source_access.access_decision(_src(), "x@y.no")[0] == "denied"


def test_denied_no_email():
    assert source_access.access_decision(_src(), None)[0] == "denied"


def test_allowed_exact_email_case_insensitive():
    assert source_access.access_decision(_src(), "Ana@FHI.no")[0] == "grant"


def test_allowed_domain():
    assert source_access.access_decision(_src(), "per@uio.no")[0] == "grant"


def test_owner_always_allowed():
    assert source_access.access_decision(_src(), "eier@fhi.no")[0] == "grant"


def test_empty_policy_means_owner_only():
    src = _src(access_policy={"emails": [], "domains": []})
    assert source_access.access_decision(src, "ana@fhi.no")[0] == "denied"
    assert source_access.access_decision(src, "eier@fhi.no")[0] == "grant"


def test_remote_only_for_protected_never_location_never_key():
    wrapped = media_crypto.encrypt_bytes(b"K1").decode("ascii")
    st, p = source_access.access_decision(
        _src(level="protected", enc_key=wrapped), "ana@fhi.no")
    assert st == "remote_only"
    assert p == {"remote_only": True, "default_exec": "remote"}


def test_grant_mode2_no_stored_key():
    st, p = source_access.access_decision(_src(), "ana@fhi.no")
    assert st == "grant" and "key" not in p
    assert p["location"] and p["fingerprint"] == "abc123"
    assert p["payload_format"] == "csv" and p["encrypted"] is True


def test_grant_mode3_releases_unwrapped_key():
    wrapped = media_crypto.encrypt_bytes(b"K1").decode("ascii")
    st, p = source_access.access_decision(_src(enc_key=wrapped), "ana@fhi.no")
    assert st == "grant" and p["key"] == "K1"


def test_strict_grant_never_includes_key():
    wrapped = media_crypto.encrypt_bytes(b"K1").decode("ascii")
    st, p = source_access.access_decision(
        _src(enc_key=wrapped, local_mode="strict"), "ana@fhi.no")
    assert st == "grant" and "key" not in p


def test_no_policy_legacy_source_grants_any_login():
    st, p = source_access.access_decision(
        _src(access_policy=None, kind="url", encrypted=False), "hvem@somhelst.no")
    assert st == "grant" and p["encrypted"] is False


def test_grant_carries_local_profile_and_level():
    st, p = source_access.access_decision(_src(), "ana@fhi.no")
    assert st == "grant" and p["local_profile"] == "open" and p["level"] == "public"


def test_public_strict_grants_strict_profile():
    st, p = source_access.access_decision(_src(local_mode="strict"), "ana@fhi.no")
    assert st == "grant" and p["local_profile"] == "strict"


def test_public_local_none_is_remote_only():
    st, p = source_access.access_decision(_src(local_mode="none"), "ana@fhi.no")
    assert st == "remote_only"


def test_protected_strict_grants_locally_with_level():
    st, p = source_access.access_decision(
        _src(level="protected", local_mode="strict"), "ana@fhi.no")
    assert st == "grant" and p["local_profile"] == "strict"
    assert p["level"] == "protected" and p["location"]


def test_protected_default_still_remote_only():
    st, p = source_access.access_decision(_src(level="protected"), "ana@fhi.no")
    assert st == "remote_only"


# ---- audience model (2026-07-05 follow-up) --------------------------------

def test_audience_of_defaults():
    assert source_access.audience_of(None) == "authenticated"        # legacy row
    assert source_access.audience_of({"emails": []}) == "listed"     # self-reg, no audience
    assert source_access.audience_of({"audience": "anyone"}) == "anyone"
    assert source_access.audience_of({"audience": "tull"}) == "listed"  # ugyldig → listed


def test_caller_allowed_owner_audience():
    src = _src(access_policy={"audience": "owner", "emails": [], "domains": []})
    assert source_access.caller_allowed(src, "eier@fhi.no")       # owner
    assert not source_access.caller_allowed(src, "ana@fhi.no")    # listed ignoreres
    assert not source_access.caller_allowed(src, None)


def test_caller_allowed_authenticated_audience():
    src = _src(access_policy={"audience": "authenticated"})
    assert source_access.caller_allowed(src, "hvemsomhelst@x.no")  # enhver innlogget
    assert not source_access.caller_allowed(src, None)             # men ikke anonym


def test_caller_allowed_anyone_audience():
    src = _src(access_policy={"audience": "anyone"})
    assert source_access.caller_allowed(src, None)                 # også anonym
    assert source_access.caller_allowed(src, "x@y.no")


def test_caller_allowed_listed_audience_still_works():
    src = _src(access_policy={"audience": "listed", "emails": ["ana@fhi.no"], "domains": ["uio.no"]})
    assert source_access.caller_allowed(src, "ana@fhi.no")
    assert source_access.caller_allowed(src, "per@uio.no")
    assert not source_access.caller_allowed(src, "x@y.no")


def test_access_decision_denies_by_audience():
    src = _src(access_policy={"audience": "authenticated"})
    assert source_access.access_decision(src, None)[0] == "denied"        # anonym nektes lokalt
    assert source_access.access_decision(src, "x@y.no")[0] == "grant"     # innlogget ok


def test_authorize_local_run_honors_audience():
    src = _src(level="protected", local_mode="strict",
               access_policy={"audience": "authenticated"})
    ok, _, _ = source_access.authorize_local_run([src], None)
    assert not ok                                                          # anonym
    ok, _, _ = source_access.authorize_local_run([src], "hvem@somhelst.no")
    assert ok
