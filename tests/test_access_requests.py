"""Pure tests for the access-request/grant workflow (roadmap §2a)."""
import pytest

import access_requests as ar


def test_normalize_email_lowercases_and_trims():
    assert ar.normalize_email("  Ana@FHI.no ") == "ana@fhi.no"


def test_normalize_email_rejects_garbage():
    with pytest.raises(ValueError, match="ugyldig"):
        ar.normalize_email("not-an-email")
    with pytest.raises(ValueError, match="ugyldig"):
        ar.normalize_email("")


def test_add_pending_appends_new_email():
    pending, added = ar.add_pending([], "ana@fhi.no")
    assert added is True
    assert len(pending) == 1
    assert pending[0]["email"] == "ana@fhi.no"
    assert "requested_at" in pending[0]


def test_add_pending_dedupes_same_email():
    pending, _ = ar.add_pending([], "ana@fhi.no")
    pending2, added2 = ar.add_pending(pending, "Ana@FHI.no")
    assert added2 is False
    assert len(pending2) == 1


def test_add_pending_keeps_other_entries():
    pending, _ = ar.add_pending([], "a@x.no")
    pending, _ = ar.add_pending(pending, "b@x.no")
    assert len(pending) == 2


def test_already_pending():
    pending, _ = ar.add_pending([], "ana@fhi.no")
    assert ar.already_pending(pending, "ANA@fhi.no") is True
    assert ar.already_pending(pending, "other@fhi.no") is False


def test_resolve_pending_removes_matching_email_only():
    pending, _ = ar.add_pending([], "a@x.no")
    pending, _ = ar.add_pending(pending, "b@x.no")
    remaining = ar.resolve_pending(pending, "a@x.no")
    assert len(remaining) == 1
    assert remaining[0]["email"] == "b@x.no"


def test_resolve_pending_on_empty_list():
    assert ar.resolve_pending([], "a@x.no") == []


def test_grant_email_creates_policy_when_absent():
    policy = ar.grant_email(None, "ana@fhi.no")
    assert policy == {"emails": ["ana@fhi.no"]}


def test_grant_email_appends_to_existing_policy():
    policy = ar.grant_email({"emails": ["x@y.no"], "domains": ["uio.no"]}, "ana@fhi.no")
    assert set(policy["emails"]) == {"x@y.no", "ana@fhi.no"}
    assert policy["domains"] == ["uio.no"]   # untouched


def test_grant_email_dedupes():
    policy = ar.grant_email({"emails": ["ana@fhi.no"]}, "Ana@FHI.no")
    assert policy["emails"] == ["ana@fhi.no"]


def test_grant_email_preserves_audience():
    policy = ar.grant_email({"emails": [], "audience": "listed"}, "ana@fhi.no")
    assert policy["audience"] == "listed"
    assert policy["emails"] == ["ana@fhi.no"]
