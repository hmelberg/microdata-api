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
import anvil.tables as tables
import anvil.users
from anvil.server import HttpResponse
from anvil.tables import app_tables

import utils
from . import eff_wordlist


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
MAGIC_TOKEN_TTL_MINUTES = 15  # kept for reference; new codes use MAGIC_CODE_TTL_DAYS
MAGIC_CODE_TTL_DAYS = 30      # multi-use codes valid for 30 days
SESSION_PREFIX = "mdapi_"


def _utcnow() -> dt.datetime:
    """Timezone-aware UTC now. Anvil's data tables store datetimes WITH
    tzinfo, so reads come back aware. Mixing naive (datetime.utcnow()) with
    aware values raises TypeError on comparison. Use this everywhere.
    """
    return dt.datetime.now(dt.timezone.utc)


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


def issue_session_token(
    user,
    request_meta: dict | None = None,
    anonymous_label: str | None = None,
) -> tuple[str, dt.datetime]:
    """Issue a session token. If user is None, this is an anonymous shared-
    code session — token has no user link, identified by anonymous_label."""
    raw = SESSION_PREFIX + secrets.token_urlsafe(32)
    expires = _utcnow() + dt.timedelta(days=SESSION_TOKEN_TTL_DAYS)
    request_meta = request_meta or {}
    extra = {}
    if user is None and anonymous_label:
        extra["anonymous_label"] = anonymous_label  # auto-create column
    app_tables.auth_tokens.add_row(
        user=user,
        token_hash=_hash_token(raw),
        kind="session",
        created=_utcnow(),
        expires=expires,
        last_used=_utcnow(),
        user_agent=request_meta.get("user_agent", "")[:500],
        ip=request_meta.get("ip", ""),
        revoked=False,
        **extra,
    )
    return raw, expires


def issue_magic_code(email: str) -> str:
    """Issue a multi-use magic code tied to an email. Returns a 3-word
    code (EFF Large Wordlist, hyphen-separated, ~39 bits entropy).

    Codes are valid for 30 days and can be used multiple times. Each use
    updates last_used. Codes are revoked manually via revoke_session_token
    or naturally expire.

    Whitelist match happens on confirmed email at first redemption time
    (in find_or_create_user).
    """
    rng = secrets.SystemRandom()
    code = "-".join(rng.choice(eff_wordlist.EFF_WORDS) for _ in range(3))
    expires = _utcnow() + dt.timedelta(days=MAGIC_CODE_TTL_DAYS)
    app_tables.auth_tokens.add_row(
        user=None,
        token_hash=_hash_token(code),
        kind="magic",
        created=_utcnow(),
        expires=expires,
        last_used=None,
        user_agent="",
        ip="",
        revoked=False,
        magic_email=email.lower().strip(),
    )
    return code


def issue_shared_code(
    label: str,
    expires_days: int = 3,
    max_uses: int | None = None,
) -> dict:
    """Issue a shared access code that any number of users can use to log in
    anonymously (no email, no user record). Returns the code and metadata.

    Codes are stored with kind='shared'. The `label` is for admin's internal
    tracking (e.g., 'workshop-2026-06-15'). max_uses=None means unlimited.
    """
    rng = secrets.SystemRandom()
    code = "-".join(rng.choice(eff_wordlist.EFF_WORDS) for _ in range(3))
    expires = _utcnow() + dt.timedelta(days=expires_days)
    app_tables.auth_tokens.add_row(
        user=None,
        token_hash=_hash_token(code),
        kind="shared",
        created=_utcnow(),
        expires=expires,
        last_used=None,
        user_agent="",
        ip="",
        revoked=False,
        magic_email=None,
        shared_label=label.strip(),       # auto-create column
        shared_max_uses=max_uses,          # auto-create column; None means unlimited
        shared_use_count=0,               # auto-create column
    )
    return {
        "code": code,
        "expires_at": expires,
        "max_uses": max_uses,
        "label": label.strip(),
    }


def _normalize_magic_code(raw: str) -> str:
    """Normalize a magic code for verification: lowercase, replace any run of
    non-alpha characters with a single hyphen.

    So 'Abacus Charity Twelve' → 'abacus-charity-twelve'
    and 'abacus-charity-twelve' → 'abacus-charity-twelve' (unchanged).
    """
    s = raw.lower().strip()
    s = re.sub(r"[^a-z]+", "-", s)
    s = s.strip("-")
    return s


@tables.in_transaction
def consume_magic_code(raw: str) -> dict | None:
    """Validate a magic or shared code; return info about the redemption.

    BREAKING SIGNATURE CHANGE: previously returned str | None (the email).
    Now returns dict | None. auth_endpoints.py /auth/email/verify must be
    updated to use result['kind'] / result['email'] instead of treating the
    return value as a plain email string (tracked in M5-T3).

    @in_transaction (2026-07-07 fix): the shared-code branch below is a
    read-check-increment on shared_use_count with no locking — two
    concurrent redemptions could each read the same use_count and both
    proceed, letting actual logins exceed the admin-configured max_uses (a
    quota bypass, exactly the workshop-shared-code scenario this field
    exists for). Anvil retries the whole function body on a write conflict,
    so this makes the read-check-increment atomic across concurrent callers.

    Returns:
        {'kind': 'magic', 'email': str}      — regular per-user magic code
        {'kind': 'shared', 'label': str}     — admin-issued shared code
        None                                  — invalid/expired/revoked
    """
    normalized = _normalize_magic_code(raw)
    if not normalized:
        return None
    token_hash = _hash_token(normalized)

    # Try magic kind first (more common)
    row = _safe_get(app_tables.auth_tokens, token_hash=token_hash, kind="magic")
    if row is not None:
        if row["revoked"]:
            return None
        if row["expires"] and row["expires"] < _utcnow():
            return None
        row["last_used"] = _utcnow()
        return {"kind": "magic", "email": row["magic_email"]}

    # Try shared kind
    row = _safe_get(app_tables.auth_tokens, token_hash=token_hash, kind="shared")
    if row is not None:
        if row["revoked"]:
            return None
        if row["expires"] and row["expires"] < _utcnow():
            return None
        max_uses = row.get("shared_max_uses")
        use_count = row.get("shared_use_count") or 0
        if max_uses is not None and use_count >= max_uses:
            return None  # used up
        row["shared_use_count"] = use_count + 1
        row["last_used"] = _utcnow()
        return {"kind": "shared", "label": row.get("shared_label") or "(uten etikett)"}

    return None


def lookup_session_token(raw: str):
    """Return (user_row, token_row) for a valid session token, or (None, None).
    For anonymous shared-code sessions: user_row is None but token_row is set."""
    row = _safe_get(app_tables.auth_tokens, token_hash=_hash_token(raw), kind="session")
    if row is None or row["revoked"]:
        return None, None
    if row["expires"] and row["expires"] < _utcnow():
        return None, None
    user = row["user"]
    # Anonymous sessions: user is None but token is still valid
    if user is None:
        if row.get("anonymous_label"):
            row["last_used"] = _utcnow()
            return None, row  # token valid, no user
        return None, None  # neither user nor anonymous: invalid
    if user.get("deleted_at"):
        return None, None
    row["last_used"] = _utcnow()
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
    """Read the comma-separated bootstrap admin list from Anvil Secrets.

    Tolerates a common misspelling ("BOOTRSTRAP_…") so a typo in the IDE
    doesn't silently lock us out of admin access.
    """
    raw = ""
    for name in ("BOOTSTRAP_ADMIN_EMAILS", "BOOTRSTRAP_ADMIN_EMAILS"):
        try:
            raw = anvil.secrets.get_secret(name) or ""
            if raw:
                break
        except Exception:
            continue
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


def _lookup_user(email_lc: str):
    """Find a user row by email. Prefer anvil.users.get_user (canonical) and
    fall back to a direct table read so we work even if the Users service is
    misconfigured.
    """
    try:
        user = anvil.users.get_user(email_address=email_lc)
        if user is not None:
            return user
    except Exception:
        pass
    return _safe_get(app_tables.users, email=email_lc)


def find_or_create_user(email: str, *, provider_kind: str = "email_magic"):
    """Find or create a row that's compatible with both Anvil Users and our
    custom domain logic. Anvil Users service watches the `users` table; we
    populate its required columns (enabled, confirmed_email, signed_up,
    password_hash, n_password_failures) alongside our own (category, credits,
    is_admin, …) so future password/Google/MFA flows slot in cleanly.
    """
    email_lc = email.lower().strip()
    user = _lookup_user(email_lc)
    now = _utcnow()
    bootstrap_admins = _bootstrap_admin_emails()

    if user is None:
        whitelist = match_whitelist(email_lc)
        user = app_tables.users.add_row(
            # --- Anvil Users service columns ---
            email=email_lc,
            enabled=True,
            confirmed_email=True,           # implicit confirmation: clicked the magic-link
            password_hash=None,             # set later if user opts into password login
            n_password_failures=0,
            # --- Our custom domain columns ---
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
        # Existing user — refresh last_login, fill in Anvil Users fields if a
        # legacy row pre-existed without them, and re-check bootstrap admin.
        user["last_login"] = now
        if not user["confirmed_email"]:
            user["confirmed_email"] = True
        if not user["enabled"]:
            user["enabled"] = True
        if user["n_password_failures"] is None:
            user["n_password_failures"] = 0
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
        user, row = lookup_session_token(token)
        if user is None and row is None:
            return None, _json({"error": "invalid or expired token"}, status=401)
        if user is not None:
            # Normal user session
            # Per-user rate limit reuses the existing alias-based bucket, keyed by email
            if not utils.check_rate_limit(f"user:{user['email']}"):
                return None, _json({"error": "rate limit exceeded"}, status=429)
            return user, None
        else:
            # Anonymous shared-code session (user is None, row has anonymous_label)
            label = row.get("anonymous_label") or ""
            principal = f"anonymous:{label}"
            if not utils.check_rate_limit(f"anonymous:{label}"):
                return None, _json({"error": "rate limit exceeded"}, status=429)
            return principal, None

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
            "<p>Your sign-in code:</p>"
            f"<p style=\"font-size: 18px; font-family: monospace; padding: 12px; "
            f"background: #f4f4f4; border-radius: 4px;\"><strong>{code}</strong></p>"
            "<p>Paste it in the login dialog in Microdata Script Runner. "
            "The code is valid for 30 days and works on any device — paste it "
            "on each machine you want to log in on.</p>"
            f"<p>Or click here to sign in directly on this device: "
            f"<a href=\"{url}\">Sign in</a></p>"
            "<p>If you did not request this, you can ignore this email.</p>"
        )
    else:
        subject = "Logg inn til Microdata Script Runner"
        html = (
            "<p>Hei,</p>"
            "<p>Din pålogginskode:</p>"
            f"<p style=\"font-size: 18px; font-family: monospace; padding: 12px; "
            f"background: #f4f4f4; border-radius: 4px;\"><strong>{code}</strong></p>"
            "<p>Lim den inn i pålogginsdialogen i Microdata Script Runner. "
            "Koden er gyldig i 30 dager og fungerer på hvilken som helst enhet — "
            "lim den inn på hver maskin du vil logge inn på.</p>"
            f"<p>Eller klikk her for å logge inn direkte på denne enheten: "
            f"<a href=\"{url}\">Logg inn</a></p>"
            "<p>Hvis du ikke ba om dette, kan du ignorere denne e-posten.</p>"
        )
    anvil.email.send(to=email, subject=subject, html=html)
