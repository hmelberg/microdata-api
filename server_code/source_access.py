# microdata-api/server_code/source_access.py
"""Access decision for /source_access (spec 2026-07-05-encrypted-external-
sources-design.md §3): given a resolved source row and the caller's verified
email, decide denied / remote_only / grant — and what the grant contains.

Pure module (no Anvil imports at module level) so it is fully unit-testable.
Key release rules:
  - access_policy present  -> caller email must match (exact, @domain, or owner).
  - access_policy absent   -> legacy behavior: any logged-in caller passes
    (matches /source_info visibility for non-public sources).
  - local_mode == "none"   -> remote_only: never location, never key.
  - local_mode != "none"   -> grant location (+ Fernet-unwrapped key when the
    owner stored one — mode 3 whitelist-only), tagged with local_profile
    ("open"|"strict") and level (the policy tier for the local engine).
"""
from __future__ import annotations


def email_allowed(email: str | None, policy: dict | None, owner_email: str = "") -> bool:
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


def access_decision(src: dict, email: str | None):
    """-> (status, payload); status in {"denied", "remote_only", "grant"}.

    Grant table (spec 2026-07-05-browser-strict-execution §2): local_mode
    decides whether rows may reach the browser at all ("none" -> remote_only),
    and under which engine ("open" -> fri analyse, "strict" -> kun safepy-
    fasaden, med nivået fra registreringen som policy-tier)."""
    policy = src.get("access_policy")
    if policy is not None and not email_allowed(email, policy, src.get("owner_email") or ""):
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
    if src.get("kind") == "encrypted_url" and src.get("enc_key"):
        from media_crypto import decrypt_bytes
        out["key"] = decrypt_bytes(src["enc_key"].encode("ascii")).decode("ascii")
    return "grant", out
