"""Admin-only HTTP endpoint: export the audit_log table as CSV.

  GET /admin/audit_export?days=N  → CSV attachment of audit_log rows with
  ts > now - N days (default 90, 1..365). Client does the analysis; this
  endpoint just streams the raw rows so no server-side aggregation logic
  needs to be maintained here.

Pure logic (audit_rows_to_csv, validate_days) is Anvil-free and testable
locally, following query_audit.py's lazy-import convention — anything that
touches app_tables imports anvil lazily inside the function.
"""
from __future__ import annotations

import csv
import io
import json

CSV_COLUMNS = ["ts", "request_id", "principal", "principal_kind", "source_ids",
               "level", "dialect", "script_head", "status", "error", "releases",
               "latency_ms"]


def validate_days(raw):
    """Parse+clamp the `days` query param. Returns an int in [1, 365], or
    None if `raw` is invalid (not an int, or out of range). None (missing
    param) defaults to 90."""
    if raw is None or raw == "":
        return 90
    try:
        days = int(str(raw))
    except (TypeError, ValueError):
        return None
    if days < 1 or days > 365:
        return None
    return days


def audit_rows_to_csv(rows):
    """rows: iterable of dict-like with the audit_log columns. Returns a CSV
    string (header + one line per row). source_ids joins with ';', releases
    serializes as JSON, ts as ISO-8601; None -> empty string. The csv module
    handles quoting of scripts with newlines/commas/quotes."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        ts = row.get("ts")
        if hasattr(ts, "isoformat"):
            ts = ts.isoformat()
        elif ts is None:
            ts = ""

        source_ids = row.get("source_ids") or []
        releases = row.get("releases") or []

        writer.writerow([
            ts,
            row.get("request_id") or "",
            row.get("principal") or "",
            row.get("principal_kind") or "",
            ";".join(source_ids),
            row.get("level") or "",
            row.get("dialect") or "",
            row.get("script_head") or "",
            row.get("status") or "",
            row.get("error") or "",
            json.dumps(releases, ensure_ascii=False),
            row.get("latency_ms") if row.get("latency_ms") is not None else "",
        ])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# HTTP endpoint (Anvil)
#
# admin_audit.py must stay importable in the local (anvil-free) pytest
# environment so audit_rows_to_csv/validate_days are directly testable, so —
# like query_audit.py — anvil is never imported at module top. Unlike
# query_audit.py this module also registers an http_endpoint, which needs
# `anvil.server` bound at decoration time; the try/except below makes that
# registration a no-op locally instead of an ImportError, while behaving
# exactly like auth_endpoints.py's top-level `import anvil.server` in the
# real Anvil app (where the import always succeeds).

try:
    import anvil.server
    from anvil.server import HttpResponse
    import http_utils
    _json = http_utils.json_response
except ImportError:
    anvil = None
    HttpResponse = None

    def _json(body: dict, status: int = 200):
        return HttpResponse(
            status=status,
            body=json.dumps(body, ensure_ascii=False),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )


if anvil is not None:

    @anvil.server.http_endpoint(
        "/admin/audit_export", methods=["GET"],
        cross_site_session=False, enable_cors=True,
    )
    def http_admin_audit_export(**kwargs):
        """Admin-only: export the audit_log table as a CSV attachment.

        Query params:
            days: int, 1..365, default 90. Rows with ts > now - days are
            included.

        Response 200: text/csv attachment (audit_log.csv)
        Response 401: not authenticated
        Response 403: authenticated but not admin
        Response 400: invalid `days`
        """
        try:
            import auth

            principal, err = auth.authenticate_or_fail()
            if err:
                return err
            user = auth.principal_user(principal)
            if user is None or not user["is_admin"]:
                return _json({"error": "admin access required"}, status=403)

            days = validate_days(kwargs.get("days"))
            if days is None:
                return _json({"error": "days must be int between 1 and 365"}, status=400)

            from datetime import datetime, timedelta, timezone
            from anvil.tables import app_tables
            import anvil.tables.query as q

            since = datetime.now(timezone.utc) - timedelta(days=days)
            rows = app_tables.audit_log.search(ts=q.greater_than(since))
            csv_text = audit_rows_to_csv(rows)

            return HttpResponse(
                status=200,
                body=csv_text,
                headers={
                    "Content-Type": "text/csv; charset=utf-8",
                    "Content-Disposition": 'attachment; filename="audit_log.csv"',
                },
            )
        except Exception as exc:
            try:
                print(f"[admin/audit_export] failed: {exc!r}")
            except Exception:
                pass
            return _json({"error": "audit export failed: " + str(exc)[:200]}, status=500)
