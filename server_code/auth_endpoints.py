import anvil.email
import anvil.users
"""HTTP endpoints for user authentication.

Magic-link flow (Phase 1, no Microsoft SSO yet):
  POST /auth/email/request  → emails a single-use sign-in link
  POST /auth/email/verify   → exchanges code for a 30-day session token

  GET  /auth/me             → returns the logged-in user (Bearer required)
  POST /auth/logout         → revokes the current session token

All responses are JSON. /auth/email/request always returns 200 to avoid
leaking which addresses are registered.
"""

from __future__ import annotations

import json

import anvil.server
from anvil.server import HttpResponse

import auth


# ---------------------------------------------------------------------------
# Helpers


def _json(body: dict, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status=status,
        body=json.dumps(body, ensure_ascii=False),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


def _load_body() -> dict:
    req = anvil.server.request
    body = req.body_json
    if body is None and req.body:
        try:
            body = json.loads(req.body.get_bytes().decode("utf-8"))
        except Exception:
            body = None
    return body or {}


def _user_payload(user) -> dict:
    """Shape of the user object returned to the client."""
    return {
        "email": user["email"],
        "display_name": user["display_name"] or user["email"].split("@")[0],
        "category": user["category"],
        "is_admin": bool(user["is_admin"]),
        "is_superuser": bool(user["is_superuser"]),
        "credits": user["credits"] or 0,
        "expires_at": (
            user["expires_at"].isoformat() if user["expires_at"] else None
        ),
    }


# ---------------------------------------------------------------------------
# /auth/email/request


@anvil.server.http_endpoint(
    "/auth/email/request", methods=["POST"],
    cross_site_session=False, enable_cors=True,
)
def http_auth_email_request():
    body = _load_body()
    email = (body.get("email") or "").strip().lower()
    lang = body.get("lang") or "no"

    # Always return ok=true so we don't leak which emails are registered or
    # whitelisted. Validation failures still return 400 — bad input is fine
    # to surface, just not "is this email recognized?".
    if not auth._is_valid_email(email):
        return _json({"error": "invalid email"}, status=400)

    try:
        code = auth.issue_magic_code(email)
        auth.send_magic_link_email(email, code, lang=lang)
    except Exception as exc:
        # Log but don't reveal cause to caller (could be email-quota,
        # Anvil downtime, etc.)
        try:
            print(f"[auth/email/request] send failed for {email}: {exc!r}")
        except Exception:
            pass

    return _json({"ok": True})


# ---------------------------------------------------------------------------
# /auth/email/verify


@anvil.server.http_endpoint(
    "/auth/email/verify", methods=["POST"],
    cross_site_session=False, enable_cors=True,
)
def http_auth_email_verify():
    body = _load_body()
    code = (body.get("code") or "").strip()
    if not code:
        return _json({"error": "missing code"}, status=400)

    email = auth.consume_magic_code(code)
    if email is None:
        return _json({"error": "invalid or expired code"}, status=400)

    user = auth.find_or_create_user(email, provider_kind="email_magic")
    token, expires = auth.issue_session_token(
        user, request_meta=auth._request_meta(anvil.server.request)
    )

    return _json({
        "token": token,
        "expires_at": expires.isoformat(),
        "user": _user_payload(user),
    })


# ---------------------------------------------------------------------------
# /auth/me


@anvil.server.http_endpoint(
    "/auth/me", methods=["GET"],
    cross_site_session=False, enable_cors=True,
)
def http_auth_me():
    principal, err = auth.authenticate_or_fail()
    if err:
        return err
    user = auth.principal_user(principal)
    if user is None:
        # Legacy X-API-Key path: no user account behind the alias
        return _json({
            "user": None,
            "principal_kind": "service_token",
            "alias": principal if isinstance(principal, str) else "",
        })
    return _json({
        "principal_kind": "user",
        "user": _user_payload(user),
    })


# ---------------------------------------------------------------------------
# /auth/logout


@anvil.server.http_endpoint(
    "/auth/logout", methods=["POST"],
    cross_site_session=False, enable_cors=True,
)
def http_auth_logout():
    req = anvil.server.request
    headers = getattr(req, "headers", None) or {}
    auth_h = headers.get("Authorization") or headers.get("authorization") or ""
    if auth_h.startswith("Bearer "):
        token = auth_h[7:].strip()
        auth.revoke_session_token(token)
    # Always return ok — logout is idempotent; an invalid token still
    # results in "you are not logged in" from the client's perspective.
    return _json({"ok": True})
