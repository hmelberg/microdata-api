# microdata-api/server_code/http_utils.py
"""Shared helpers for Anvil HTTP endpoints: JSON responses, request-body
parsing, safe table-cell reads, and audit-log writes.

Consolidated 2026-07-07 (docs/superpowers/2026-07-07-code-review.md §5) —
_json/_load_body/_cell/_audit were copy-pasted near-verbatim across
owner_sources.py, access_requests.py, api_endpoints.py, auth_endpoints.py,
auth.py, admin_sources.py and admin_audit.py, risking one copy silently
drifting from the rest.

Imports anvil unconditionally (no _ANVIL guard) — this module is only ever
imported from inside another module's own `if _ANVIL:`/`try: import anvil`
block, never from the pure-function half above it, so it never needs to be
importable in a bare pytest run itself.
"""
from __future__ import annotations

import datetime as dt
import json

import anvil.server
from anvil.server import HttpResponse
from anvil.tables import app_tables


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def json_response(body: dict, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status=status,
        body=json.dumps(body, ensure_ascii=False),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


def load_body() -> dict:
    req = anvil.server.request
    body = req.body_json
    if body is None and req.body:
        try:
            body = json.loads(req.body.get_bytes().decode("utf-8"))
        except Exception:
            body = None
    return body or {}


def cell(row, name, default=None):
    """Read a column that may not exist yet on older rows (Anvil only adds
    columns on write, and reading a missing column raises)."""
    try:
        return row[name]
    except Exception:
        return default


def audit(email: str, action: str, detail: str) -> None:
    try:
        app_tables.audit_log.add_row(when=utcnow(), who=email,
                                     action=action, detail=detail)
    except Exception:
        pass  # auditing must never block the operation itself
