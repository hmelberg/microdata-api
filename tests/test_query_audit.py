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


def test_unknown_level_fails_closed_to_protected_budget():
    # An unrecognized level string (e.g. a typo, or a future level not yet
    # wired into BUDGETS) must not fall through to "unlimited" via .get()'s
    # None default — it should fail closed to the PROTECTED budget for that
    # principal kind (protected/user == 100).
    ok, _ = qa.check_budget("user:a@b.no", "hemmelig", ["s1"], _counter(99))
    assert ok
    ok, msg = qa.check_budget("user:a@b.no", "hemmelig", ["s1"], _counter(100))
    assert not ok and "s1" in msg


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
                                 "groups": 3, "cells_suppressed": 0, "by": "region"}},
        {"payload": 2, "audit": {"verb": "ols"}},
    ]}
    fps = qa.collect_fingerprints(d)
    assert len(fps) == 1 and fps[0]["groups_sig"] == "ab" * 8
    # grouping-column identity (v2 need): column NAMES are schema, disclosure
    # free, and pass through when present — only the fingerprint's own
    # disclosure-sensitive fields (raw payloads/top-level audit) are excluded.
    assert fps[0]["by"] == "region"
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


# pop_audit_info / build_log_row: the strip/log wiring in bg_run_extended
# (api_endpoints.py) is untested and imports anvil (unavailable locally).
# These pure helpers let it be refactored for testability without changing
# behavior — pop_audit_info strips the internal _audit_* keys from a run
# result before it reaches the client, and build_log_row is the pure
# construction of one audit_log row that log_run's anvil call now uses.

def test_pop_audit_info_removes_both_keys_and_returns_them():
    out = {"data": [1, 2], "_audit_releases": [{"verb": "group_agg"}],
           "_audit_level": "protected"}
    releases, level = qa.pop_audit_info(out)
    assert releases == [{"verb": "group_agg"}]
    assert level == "protected"
    assert "_audit_releases" not in out and "_audit_level" not in out
    assert out == {"data": [1, 2]}


def test_pop_audit_info_defaults_when_keys_absent():
    out = {"data": [1, 2]}
    assert qa.pop_audit_info(out) == ([], None)


def test_pop_audit_info_none_safe():
    assert qa.pop_audit_info(None) == ([], None)
    assert qa.pop_audit_info("not a dict") == ([], None)
    assert qa.pop_audit_info([1, 2, 3]) == ([], None)


def test_build_log_row_has_exactly_the_12_audit_log_columns():
    row = qa.build_log_row("user:a@b.no", "req-1", ["s1", "s2"], "protected",
                           "pandas", "df.groupby('g')['x'].sum()", "ok", None,
                           [{"verb": "group_agg"}], 42)
    assert set(row) == {"ts", "request_id", "principal", "principal_kind",
                        "source_ids", "level", "dialect", "script_head",
                        "status", "error", "releases", "latency_ms"}
    assert row["principal"] == "user:a@b.no"
    assert row["principal_kind"] == "user"
    assert row["source_ids"] == ["s1", "s2"]
    assert row["level"] == "protected"
    assert row["dialect"] == "pandas"
    assert row["script_head"] == "df.groupby('g')['x'].sum()"
    assert row["status"] == "ok"
    assert row["error"] is None
    assert row["releases"] == [{"verb": "group_agg"}]
    assert row["latency_ms"] == 42
    assert isinstance(row["ts"], datetime)


def test_build_log_row_truncates_script_and_error():
    row = qa.build_log_row(None, "req-2", [], None, "m2py", "x" * 3000,
                           "error", "e" * 2000, [], 0)
    assert len(row["script_head"]) == 2000
    assert len(row["error"]) == 1000
    assert row["principal"] == ""
    assert row["principal_kind"] == "api_key"
