from datetime import datetime, timezone
import query_audit as qa


def test_classify_principal():
    assert qa.classify_principal("user:a@b.no") == "user"
    assert qa.classify_principal("anonymous:kurs42") == "anonymous"
    assert qa.classify_principal("nokkel-alias") == "api_key"


def _counter(used):
    return lambda alias, sid, since: used


def test_public_and_sourceless_runs_have_no_quota():
    assert qa.check_budget("user:a@b.no", "public", ["s1"], _counter(10**6)) == (True, None)
    assert qa.check_budget("user:a@b.no", "protected", [], _counter(10**6)) == (True, None)


def test_protected_user_under_and_over_limit():
    ok, _ = qa.check_budget("user:a@b.no", "protected", ["s1"], _counter(99))
    assert ok
    ok, msg = qa.check_budget("user:a@b.no", "protected", ["s1"], _counter(100))
    assert not ok and "s1" in msg


def test_protected_anonymous_stricter():
    ok, _ = qa.check_budget("anonymous:x", "protected", ["s1"], _counter(24))
    assert ok
    ok, _ = qa.check_budget("anonymous:x", "protected", ["s1"], _counter(25))
    assert not ok


def test_sensitive_requires_user():
    ok, msg = qa.check_budget("anonymous:x", "sensitive", ["s1"], _counter(0))
    assert not ok and "innlogget" in msg
    ok, _ = qa.check_budget("user:a@b.no", "sensitive", ["s1"], _counter(29))
    assert ok


def test_window_passed_to_counter():
    seen = {}
    def counter(alias, sid, since):
        seen["since"] = since
        return 0
    now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
    qa.check_budget("user:a@b.no", "protected", ["s1"], counter, now=now)
    assert (now - seen["since"]) == qa.WINDOW


def test_collect_fingerprints_pulls_leaf_audits_only():
    d = {"audit": {"top": True}, "results": [
        {"payload": 1, "audit": {"verb": "group_agg", "groups_sig": "ab" * 8,
                                 "count_hist": {"lt_min_n": 0}, "min_n": 5,
                                 "groups": 3, "cells_suppressed": 0}},
        {"payload": 2, "audit": {"verb": "ols"}},
    ]}
    fps = qa.collect_fingerprints(d)
    assert len(fps) == 1 and fps[0]["groups_sig"] == "ab" * 8
    assert "top" not in str(fps)


# resolve_run_levels: exercised against source_registry's real fixtures
# (resolve_source falls back to them when anvil.tables isn't importable, so
# this stays a pure, anvil-free test). demo_public_csv is public;
# hospital_public_csv is protected despite the name.

def test_resolve_run_levels_strictest_wins():
    ids, level = qa.resolve_run_levels([
        {"alias": "a", "source_id": "demo_public_csv"},
        {"alias": "b", "source_id": "hospital_public_csv"},
    ])
    assert ids == ["demo_public_csv", "hospital_public_csv"]
    assert level == "protected"


def test_resolve_run_levels_all_public():
    ids, level = qa.resolve_run_levels([
        {"alias": "a", "source_id": "demo_public_csv"},
    ])
    assert ids == ["demo_public_csv"]
    assert level == "public"


def test_resolve_run_levels_unknown_source_is_conservative():
    ids, level = qa.resolve_run_levels([
        {"alias": "a", "source_id": "demo_public_csv"},
        {"alias": "b", "source_id": "does-not-exist"},
    ])
    assert ids == ["demo_public_csv", "does-not-exist"]
    assert level == "protected"


def test_resolve_run_levels_empty_request():
    assert qa.resolve_run_levels([]) == ([], None)
    assert qa.resolve_run_levels(None) == ([], None)


def test_resolve_run_levels_skips_blank_source_id():
    ids, level = qa.resolve_run_levels([{"alias": "a", "source_id": "  "}])
    assert ids == [] and level is None
