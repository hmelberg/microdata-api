"""Authentication and authorization helpers.

Phase 1: accepts both `Authorization: Bearer <token>` (user accounts via
magic-link or Microsoft SSO) and `X-API-Key` (legacy service-tokens). The
returned principal is either a `users` row (Bearer) or an alias string
(legacy). Endpoint handlers pass it through opaquely.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import secrets

import anvil.email
import anvil.secrets
import anvil.server
from anvil.server import HttpResponse
from anvil.tables import app_tables

import utils


# ---------------------------------------------------------------------------
# HTTP helper


def _json(body: dict, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status=status,
        body=json.dumps(body, ensure_ascii=False),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


# ---------------------------------------------------------------------------
# Token generation / lookup

SESSION_TOKEN_TTL_DAYS = 30
MAGIC_TOKEN_TTL_MINUTES = 15
SESSION_PREFIX = "mdapi_"


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_get(table, **kwargs):
    """Anvil .get() raises NoSuchColumnError on a column-less table; treat
    that as 'no row' so first-run code paths don't crash.
    """
    try:
        return table.get(**kwargs)
    except Exception:
        return None


def issue_session_token(user, request_meta: dict | None = None) -> tuple[str, dt.datetime]:
    raw = SESSION_PREFIX + secrets.token_urlsafe(32)
    expires = dt.datetime.utcnow() + dt.timedelta(days=SESSION_TOKEN_TTL_DAYS)
    request_meta = request_meta or {}
    app_tables.auth_tokens.add_row(
        user=user,
        token_hash=_hash_token(raw),
        kind="session",
        created=dt.datetime.utcnow(),
        expires=expires,
        last_used=dt.datetime.utcnow(),
        user_agent=request_meta.get("user_agent", "")[:500],
        ip=request_meta.get("ip", ""),
        revoked=False,
    )
    return raw, expires


def issue_magic_code(email: str) -> str:
    """Issue a single-use code tied to an email. Stored without a user link
    yet — verify_magic_code creates/finds the user row at exchange time so
    the whitelist match happens on confirmed email.
    """
    raw = secrets.token_urlsafe(24)
    expires = dt.datetime.utcnow() + dt.timedelta(minutes=MAGIC_TOKEN_TTL_MINUTES)
    app_tables.auth_tokens.add_row(
        user=None,
        token_hash=_hash_token(raw),
        kind="magic",
        created=dt.datetime.utcnow(),
        expires=expires,
        last_used=None,
        user_agent="",
        ip="",
        revoked=False,
        magic_email=email.lower().strip(),  # auto-create column
    )
    return raw


def consume_magic_code(raw: str) -> str | None:
    """Validate a magic code; return the email it was issued for, or None.
    Marks the row revoked (single-use)."""
    row = _safe_get(app_tables.auth_tokens, token_hash=_hash_token(raw), kind="magic")
    if row is None:
        return None
    if row["revoked"]:
        return None
    if row["expires"] and row["expires"] < dt.datetime.utcnow():
        return None
    email = row["magic_email"]
    row["revoked"] = True
    row["last_used"] = dt.datetime.utcnow()
    return email


def lookup_session_token(raw: str):
    """Return (user_row, token_row) for a valid session token, or (None, None)."""
    row = _safe_get(app_tables.auth_tokens, token_hash=_hash_token(raw), kind="session")
    if row is None or row["revoked"]:
        return None, None
    if row["expires"] and row["expires"] < dt.datetime.utcnow():
        return None, None
    user = row["user"]
    if user is None or user.get("deleted_at"):
        return None, None
    row["last_used"] = dt.datetime.utcnow()
    return user, row


def revoke_session_token(raw: str) -> bool:
    row = _safe_get(app_tables.auth_tokens, token_hash=_hash_token(raw), kind="session")
    if row is None:
        return False
    row["revoked"] = True
    return True


# ---------------------------------------------------------------------------
# Whitelist + user creation


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _is_valid_email(s: str) -> bool:
    return bool(EMAIL_RE.match((s or "").strip()))


def _bootstrap_admin_emails() -> set[str]:
    try:
        raw = anvil.secrets.get_secret("BOOTSTRAP_ADMIN_EMAILS") or ""
    except Exception:
        return set()
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def match_whitelist(email: str) -> dict:
    """Resolve the default account settings for an email.

    Tries an exact email match first, falls back to a domain pattern
    starting with '@'. Returns {category, is_superuser, initial_credits,
    expires_at}. If no match, returns category='free' (no access).
    """
    email_lc = email.lower().strip()
    exact = _safe_get(app_tables.email_whitelist, pattern=email_lc)
    if exact is None:
        domain = "@" + email_lc.split("@", 1)[1] if "@" in email_lc else None
        domain_match = (
            _safe_get(app_tables.email_whitelist, pattern=domain) if domain else None
        )
        match = domain_match
    else:
        match = exact
    if match is None:
        return {
            "category": "free",
            "is_superuser": False,
            "initial_credits": 0,
            "expires_at": None,
        }
    return {
        "category": match["default_category"] or "free",
        "is_superuser": bool(match["default_is_superuser"]),
        "initial_credits": match["initial_credits"] or 0,
        "expires_at": match["default_expires_at"],
    }


def find_or_create_user(email: str, *, provider_kind: str = "email_magic"):
    email_lc = email.lower().strip()
    user = _safe_get(app_tables.users, email=email_lc)
    now = dt.datetime.utcnow()
    bootstrap_admins = _bootstrap_admin_emails()
    if user is None:
        whitelist = match_whitelist(email_lc)
        user = app_tables.users.add_row(
            email=email_lc,
            category=whitelist["category"],
            is_admin=email_lc in bootstrap_admins,
            is_superuser=whitelist["is_superuser"],
            credits=whitelist["initial_credits"],
            last_credit_renewal=None,
            expires_at=whitelist["expires_at"],
            microsoft_oid=None,
            microsoft_tid=None,
            display_name=email_lc.split("@")[0],
            provider_kind=provider_kind,
            created=now,
            last_login=now,
            deleted_at=None,
            notes="",
        )
    else:
        # Existing user — refresh last_login, ensure bootstrap-admin flag is on
        user["last_login"] = now
        if email_lc in bootstrap_admins and not user["is_admin"]:
            user["is_admin"] = True
    return user


# ---------------------------------------------------------------------------
# Auth gate used by every protected endpoint


def _request_meta(req) -> dict:
    headers = getattr(req, "headers", None) or {}
    return {
        "user_agent": headers.get("User-Agent") or headers.get("user-agent") or "",
        "ip": getattr(req, "remote_address", "") or "",
    }


def authenticate_or_fail():
    """Return (principal, err_response).

    Principal is either:
      - a `users` row (Bearer auth)
      - an alias string (legacy X-API-Key)

    Endpoint handlers treat it as opaque; use principal_alias() / principal_user()
    to extract the right value for logging / row-linking.
    """
    req = anvil.server.request
    headers = getattr(req, "headers", None) or {}

    # 1) Bearer token (preferred, user-account path)
    auth_h = headers.get("Authorization") or headers.get("authorization") or ""
    if auth_h.startswith("Bearer "):
        token = auth_h[7:].strip()
        user, _row = lookup_session_token(token)
        if user is None:
            return None, _json({"error": "invalid or expired token"}, status=401)
        # Per-user rate limit reuses the existing alias-based bucket, keyed by email
        if not utils.check_rate_limit(f"user:{user['email']}"):
            return None, _json({"error": "rate limit exceeded"}, status=429)
        return user, None

    # 2) Legacy X-API-Key (service-token path)
    alias = utils.authenticate(req)
    if alias:
        if not utils.check_rate_limit(alias):
            return None, _json({"error": "rate limit exceeded"}, status=429)
        return alias, None

    return None, _json({"error": "auth required: Bearer token or X-API-Key"}, status=401)


def principal_alias(principal) -> str:
    """String form of a principal, used for `log_request(api_key_alias=...)`."""
    if principal is None:
        return ""
    if isinstance(principal, str):
        return principal
    # users row
    try:
        return f"user:{principal['email']}"
    except Exception:
        return "user:?"


def principal_user(principal):
    """Return the users row if the principal is a user, else None."""
    if principal is None or isinstance(principal, str):
        return None
    return principal


# ---------------------------------------------------------------------------
# Magic-link email


def send_magic_link_email(email: str, code: str, *, lang: str = "no") -> None:
    """Send the magic-link email. Anvil's free tier limits sends per day —
    keep it short and obvious."""
    base = "https://micro.fhi.dev/?login="
    url = base + code
    if lang == "en":
        subject = "Sign in to Microdata Script Runner"
        html = (
            "<p>Hi,</p>"
            "<p>Click the link below to sign in. The link expires in 15 minutes "
            "and can only be used once.</p>"
            f"<p><a href=\"{url}\">Sign in to Microdata Script Runner</a></p>"
            f"<p>Or paste this URL: <code>{url}</code></p>"
            "<p>If you did not request this, you can ignore this email.</p>"
        )
    else:
        subject = "Logg inn til Microdata Script Runner"
        html = (
            "<p>Hei,</p>"
            "<p>Klikk lenken under for å logge inn. Lenken utløper om 15 "
            "minutter og kan bare brukes én gang.</p>"
            f"<p><a href=\"{url}\">Logg inn til Microdata Script Runner</a></p>"
            f"<p>Eller lim inn URL-en: <code>{url}</code></p>"
            "<p>Hvis du ikke ba om dette, kan du ignorere denne e-posten.</p>"
        )
    anvil.email.send(to=email, subject=subject, html=html)
