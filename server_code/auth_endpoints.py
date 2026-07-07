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

import anvil.server
from anvil.server import HttpResponse

import auth
import utils
import http_utils

_json = http_utils.json_response
_load_body = http_utils.load_body


# ---------------------------------------------------------------------------
# Helpers


def _client_ip() -> str:
    req = anvil.server.request
    return getattr(req, "remote_address", "") or ""


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

    # Rate-limit BEFORE issuing/sending so this endpoint can't be used to
    # email-bomb an address or burn the Anvil send quota. Limit per requester IP
    # and (separately) per target email, both over a 1-hour window.
    # Per-email is the email-bomb guard; per-IP (coarser, loose enough for a
    # shared NAT / workshop room) caps cross-address spam from one source.
    ip = _client_ip()
    if not utils.check_rate_limit(f"authreq_email:{email}", max_calls=5, window_sec=3600):
        return _json({"error": "too many requests"}, status=429)
    if ip and not utils.check_rate_limit(f"authreq_ip:{ip}", max_calls=30, window_sec=3600):
        return _json({"error": "too many requests"}, status=429)

    # Issuance (a table write) and sending are split (2026-07-07 fix): the old
    # single try/except swallowed BOTH, so if issue_magic_code itself failed
    # (e.g. a Data Tables write error), the response was still {"ok": true}
    # with no code ever created — indistinguishable from "check your spam
    # folder" when the backend was actually broken. A 500 here doesn't leak
    # per-email info (unlike the invalid-email check above): it's the same
    # response regardless of whether the email is valid/registered/whitelisted.
    try:
        code = auth.issue_magic_code(email)
    except Exception as exc:
        try:
            print(f"[auth/email/request] issue_magic_code failed for {email}: {exc!r}")
        except Exception:
            pass
        return _json({"error": "server error"}, status=500)

    try:
        auth.send_magic_link_email(email, code, lang=lang)
    except Exception as exc:
        # Best-effort: the code IS issued and valid regardless of send
        # failure (Anvil's per-day send quota, transient downtime, etc.) —
        # log but don't reveal cause to caller.
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
    try:
        body = _load_body()
        code = (body.get("code") or "").strip()
        if not code:
            return _json({"error": "missing code"}, status=400)

        # Rate-limit code redemption per IP to throttle online brute-forcing of
        # the (multi-use, 30-day) magic codes. The code space is large, but the
        # codes are long-lived and many can be outstanding at once, so cap guesses.
        ip = _client_ip()
        if ip and not utils.check_rate_limit(f"authverify_ip:{ip}", max_calls=30, window_sec=600):
            return _json({"error": "too many attempts"}, status=429)

        result = auth.consume_magic_code(code)
        if result is None:
            return _json({"error": "invalid or expired code"}, status=400)

        request_meta = auth._request_meta(anvil.server.request)

        if result["kind"] == "magic":
            user = auth.find_or_create_user(result["email"], provider_kind="email_magic")
            token, expires = auth.issue_session_token(user, request_meta=request_meta)
            return _json({
                "token": token,
                "expires_at": expires.isoformat(),
                "user": _user_payload(user),
            })
        elif result["kind"] == "shared":
            # Anonymous shared session — no user, just a token
            token, expires = auth.issue_session_token(
                None, request_meta=request_meta, anonymous_label=result["label"]
            )
            return _json({
                "token": token,
                "expires_at": expires.isoformat(),
                "user": None,
                "anonymous": True,
                "label": result["label"],
            })
        else:
            return _json({"error": "unknown code kind"}, status=500)
    except Exception as exc:
        # Catch-all so the response always carries CORS headers. An uncaught
        # exception otherwise returns Anvil's default 500 (no CORS), which
        # the browser surfaces as "Failed to fetch".
        try:
            print(f"[auth/email/verify] failed: {exc!r}")
        except Exception:
            pass
        return _json({"error": "verify failed: " + str(exc)[:200]}, status=500)


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
    if user is not None:
        return _json({
            "principal_kind": "user",
            "user": _user_payload(user),
        })
    # principal is a string at this point
    if isinstance(principal, str) and principal.startswith("anonymous:"):
        label = principal[len("anonymous:"):]
        return _json({
            "user": None,
            "principal_kind": "anonymous",
            "label": label,
        })
    # Legacy X-API-Key path: no user account behind the alias
    return _json({
        "user": None,
        "principal_kind": "service_token",
        "alias": principal if isinstance(principal, str) else "",
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


# ---------------------------------------------------------------------------
# /admin/shared-codes/issue


@anvil.server.http_endpoint(
    "/admin/shared-codes/issue", methods=["POST"],
    cross_site_session=False, enable_cors=True,
)
def http_admin_issue_shared_code():
    """Admin-only: issue a shared access code that any number of users can
    use to log in anonymously. Returns the generated code + metadata.

    Body:
        {
            "label": "workshop-2026-06-15",
            "expires_days": 3,           // optional, default 3
            "max_uses": 25               // optional, null = unlimited
        }

    Response 200:
        {
            "code": "abacus-charity-twelve",
            "expires_at": "2026-05-28T12:00:00+00:00",
            "max_uses": 25,
            "label": "workshop-2026-06-15"
        }

    Response 401: not authenticated
    Response 403: authenticated but not admin
    Response 400: invalid input
    """
    try:
        principal, err = auth.authenticate_or_fail()
        if err:
            return err
        user = auth.principal_user(principal)
        if user is None or not user["is_admin"]:
            return _json({"error": "admin access required"}, status=403)

        body = _load_body()
        label = (body.get("label") or "").strip()
        if not label:
            return _json({"error": "missing label"}, status=400)
        if len(label) > 100:
            return _json({"error": "label too long (max 100 chars)"}, status=400)

        expires_days = body.get("expires_days", 3)
        if not isinstance(expires_days, int) or expires_days < 1 or expires_days > 365:
            return _json({"error": "expires_days must be int between 1 and 365"}, status=400)

        max_uses = body.get("max_uses")
        if max_uses is not None:
            if not isinstance(max_uses, int) or max_uses < 1 or max_uses > 10000:
                return _json({"error": "max_uses must be null or int between 1 and 10000"}, status=400)

        result = auth.issue_shared_code(label, expires_days=expires_days, max_uses=max_uses)
        # expires_at is datetime — serialize to ISO string
        return _json({
            "code": result["code"],
            "expires_at": result["expires_at"].isoformat(),
            "max_uses": result["max_uses"],
            "label": result["label"],
        })
    except Exception as exc:
        try:
            print(f"[admin/shared-codes/issue] failed: {exc!r}")
        except Exception:
            pass
        return _json({"error": "issue failed: " + str(exc)[:200]}, status=500)
