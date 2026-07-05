# microdata-api/server_code/source_access.py
"""Access decision for /source_access (spec 2026-07-05-encrypted-external-
sources-design.md §3): given a resolved source row and the caller's verified
email, decide denied / remote_only / grant — and what the grant contains.

Pure module (no Anvil imports at module level) so it is fully unit-testable.

Audience (who may use a source) is one axis of access_policy["audience"]:
  - "owner"          -> only the registering owner.
  - "listed"         -> owner + the emails/domains allowlist (default when a
                        self-registered policy has no explicit audience).
  - "authenticated"  -> any logged-in user (also the legacy default when a row
                        has no access_policy at all).
  - "anyone"         -> anybody, including callers with no login.

Key/location release rules (unchanged):
  - local_mode == "none"   -> remote_only: never location, never key.
  - local_mode != "none"   -> grant location (+ Fernet-unwrapped key when the
    owner stored one — mode 3 whitelist-only), tagged with local_profile
    ("open"|"strict") and level (the policy tier for the local engine).

Endpoint policy split (enforced by the endpoints, not here): keys and locations
are NEVER released to a caller with no login, even for an "anyone" source —
only server-mediated, suppressed/HE remote compute (/run_extended) accepts a
truly anonymous caller. caller_allowed() answers "may this identity use it";
the endpoints decide whether login itself is required.
"""
from __future__ import annotations

VALID_AUDIENCES = {"owner", "listed", "authenticated", "anyone"}


def audience_of(policy: dict | None) -> str:
    """Audience for a source's access_policy. None -> "authenticated" (legacy
    non-public rows: any logged-in user). A self-registered policy with no
    explicit audience -> "listed" (the emails/domains allowlist)."""
    if policy is None:
        return "authenticated"
    aud = policy.get("audience")
    return aud if aud in VALID_AUDIENCES else "listed"


def email_allowed(email: str | None, policy: dict | None, owner_email: str = "") -> bool:
    """Listed-allowlist check: owner, exact email, or @domain match."""
    if not email:
        return False
    email = email.strip().lower()
    if owner_email and email == owner_email.strip().lower():
        return True
    if policy is None:
        return False
    emails = [str(e).strip().lower() for e in (policy.get("emails") or [])]
    domains = [str(d).strip().lower().lstrip("@") for d in (policy.get("domains") or [])]
    if email in emails:
        return True
    return email.rsplit("@", 1)[-1] in domains


def caller_allowed(src: dict, email: str | None) -> bool:
    """May the caller with this verified email (None = not logged in) use src?
    Applies the audience rule; the owner always passes."""
    policy = src.get("access_policy")
    aud = audience_of(policy)
    owner = (src.get("owner_email") or "").strip().lower()
    e = (email or "").strip().lower()
    if aud == "anyone":
        return True
    if e and owner and e == owner:
        return True
    if aud == "authenticated":
        return bool(e)
    if aud == "owner":
        return bool(e) and e == owner
    return email_allowed(email, policy, owner)   # "listed"


def access_decision(src: dict, email: str | None):
    """-> (status, payload); status in {"denied", "remote_only", "grant"}.

    Grant table (spec 2026-07-05-browser-strict-execution §2): local_mode
    decides whether rows may reach the browser at all ("none" -> remote_only),
    and under which engine ("open" -> fri analyse, "strict" -> kun safepy-
    fasaden, med nivået fra registreringen som policy-tier)."""
    if not caller_allowed(src, email):
        return "denied", None
    level = src.get("level") or "protected"
    local_mode = src.get("local_mode") or ("open" if level == "public" else "none")
    if local_mode == "none":
        return "remote_only", {"remote_only": True, "default_exec": "remote"}
    out = {
        "remote_only": False,
        "location": src.get("location"),
        "payload_format": src.get("format") or "csv",
        "fingerprint": src.get("fingerprint"),
        "encrypted": src.get("kind") == "encrypted_url",
        "local_profile": "strict" if local_mode == "strict" else "open",
        "level": level,
    }
    if (src.get("kind") == "encrypted_url" and src.get("enc_key")
            and out["local_profile"] == "open"):
        from media_crypto import decrypt_bytes
        out["key"] = decrypt_bytes(src["enc_key"].encode("ascii")).decode("ascii")
    return "grant", out


_LEVEL_ORDER = {"public": 0, "protected": 1, "sensitive": 2}


def authorize_local_run(srcs: list, email: str | None):
    """Per-run gate for local strict runs (spec V3). Every source must allow
    the caller AND allow local execution; returns the per-source stored keys
    (Fernet-unwrapped) and the most restrictive level for the policy tier.
    -> (ok, source_keys, level). Pure; the endpoint logs the audit row."""
    keys, level = {}, "public"
    for src in srcs:
        if not caller_allowed(src, email):
            return False, {}, level
        local_mode = src.get("local_mode") or (
            "open" if (src.get("level") or "protected") == "public" else "none")
        if local_mode == "none":
            return False, {}, level
        if src.get("kind") == "encrypted_url" and src.get("enc_key"):
            from media_crypto import decrypt_bytes
            keys[src["source_id"]] = decrypt_bytes(
                src["enc_key"].encode("ascii")).decode("ascii")
        lv = src.get("level") or "protected"
        if _LEVEL_ORDER.get(lv, 1) > _LEVEL_ORDER[level]:
            level = lv
    return True, keys, level
