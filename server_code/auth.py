"""Authentication and authorization helpers.

Phase 0 (current): X-API-Key only. Same behavior as the previous
`_authenticate_or_fail` in api_endpoints.py, just relocated so future phases
can extend it (Bearer tokens, role checks, credit enforcement) without
touching the endpoint module.

Phase 1+ will accept `Authorization: Bearer <token>` for user-issued tokens
backed by the auth_tokens table, alongside the legacy X-API-Key path which
remains valid for service-tokens / automation.
"""

from __future__ import annotations

import json

import anvil.server
from anvil.server import HttpResponse

import utils


def _json(body: dict, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status=status,
        body=json.dumps(body, ensure_ascii=False),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


def authenticate_or_fail():
    """Return (principal, err_response).

    `principal`: API-key alias (str) on success. In Phase 1 this will become
    a User row when Bearer tokens are introduced, with the alias path kept
    for service-tokens. Endpoint handlers should treat the value as opaque
    and pass it through to `utils.log_request(api_key_alias=...)`.

    `err_response`: an HttpResponse on failure (401 / 429), None on success.
    """
    req = anvil.server.request
    alias = utils.authenticate(req)
    if not alias:
        return None, _json({"error": "invalid or missing X-API-Key"}, status=401)
    if not utils.check_rate_limit(alias):
        return None, _json({"error": "rate limit exceeded"}, status=429)
    return alias, None
