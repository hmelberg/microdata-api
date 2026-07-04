from datetime import datetime, timezone
import csv
import io
import json

import admin_audit as aa


def test_csv_header_is_exactly_csv_columns():
    csv_text = aa.audit_rows_to_csv([])
    reader = csv.reader(io.StringIO(csv_text))
    header = next(reader)
    assert header == aa.CSV_COLUMNS


def test_datetime_ts_becomes_iso_string():
    ts = datetime(2026, 7, 4, 12, 30, tzinfo=timezone.utc)
    row = {"ts": ts, "request_id": "req-1", "principal": "user:a@b.no",
           "principal_kind": "user", "source_ids": ["a", "b"], "level": "protected",
           "dialect": "pandas", "script_head": "df.head()", "status": "ok",
           "error": None, "releases": [], "latency_ms": 10}
    csv_text = aa.audit_rows_to_csv([row])
    reader = csv.reader(io.StringIO(csv_text))
    header = next(reader)
    data = next(reader)
    parsed = dict(zip(header, data))
    assert parsed["ts"] == ts.isoformat()


def test_string_ts_passes_through_unchanged():
    row = {"ts": "2026-07-04T12:30:00+00:00", "request_id": "req-1",
           "principal": "", "principal_kind": "api_key", "source_ids": [],
           "level": None, "dialect": "m2py", "script_head": "", "status": "ok",
           "error": None, "releases": [], "latency_ms": 0}
    csv_text = aa.audit_rows_to_csv([row])
    reader = csv.reader(io.StringIO(csv_text))
    next(reader)
    data = next(reader)
    parsed = dict(zip(aa.CSV_COLUMNS, data))
    assert parsed["ts"] == "2026-07-04T12:30:00+00:00"


def test_source_ids_join_with_semicolon():
    row = _base_row(source_ids=["a", "b"])
    parsed = _parse_one(aa.audit_rows_to_csv([row]))
    assert parsed["source_ids"] == "a;b"


def test_source_ids_empty_list_is_empty_string():
    row = _base_row(source_ids=[])
    parsed = _parse_one(aa.audit_rows_to_csv([row]))
    assert parsed["source_ids"] == ""


def test_releases_serializes_as_json_that_round_trips():
    releases = [{"verb": "x"}]
    row = _base_row(releases=releases)
    parsed = _parse_one(aa.audit_rows_to_csv([row]))
    assert json.loads(parsed["releases"]) == releases


def test_none_error_becomes_empty_string():
    row = _base_row(error=None)
    parsed = _parse_one(aa.audit_rows_to_csv([row]))
    assert parsed["error"] == ""


def test_none_level_becomes_empty_string():
    row = _base_row(level=None)
    parsed = _parse_one(aa.audit_rows_to_csv([row]))
    assert parsed["level"] == ""


def test_script_with_newline_comma_quote_survives_round_trip():
    tricky = 'line1\nline2, with "quotes"'
    row = _base_row(script_head=tricky)
    csv_text = aa.audit_rows_to_csv([row])
    reader = csv.reader(io.StringIO(csv_text))
    header = next(reader)
    data = next(reader)
    parsed = dict(zip(header, data))
    assert parsed["script_head"] == tricky


def test_multiple_rows_produce_header_plus_n_lines():
    rows = [_base_row(request_id="req-1"), _base_row(request_id="req-2")]
    csv_text = aa.audit_rows_to_csv(rows)
    reader = list(csv.reader(io.StringIO(csv_text)))
    assert len(reader) == 3  # header + 2 rows
    assert reader[1][1] == "req-1"
    assert reader[2][1] == "req-2"


def test_validate_days_defaults_to_90():
    assert aa.validate_days(None) == 90


def test_validate_days_accepts_valid_int_strings():
    assert aa.validate_days("30") == 30
    assert aa.validate_days("1") == 1
    assert aa.validate_days("365") == 365


def test_validate_days_rejects_out_of_range():
    assert aa.validate_days("0") is None
    assert aa.validate_days("366") is None
    assert aa.validate_days("-5") is None


def test_validate_days_rejects_non_integer():
    assert aa.validate_days("abc") is None
    assert aa.validate_days("1.5") is None


# --- helpers ---

def _base_row(**overrides):
    row = {"ts": datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc),
           "request_id": "req-1", "principal": "user:a@b.no",
           "principal_kind": "user", "source_ids": ["s1"], "level": "protected",
           "dialect": "pandas", "script_head": "df.head()", "status": "ok",
           "error": None, "releases": [], "latency_ms": 5}
    row.update(overrides)
    return row


def _parse_one(csv_text):
    reader = csv.reader(io.StringIO(csv_text))
    header = next(reader)
    data = next(reader)
    return dict(zip(header, data))
