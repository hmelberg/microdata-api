import anvil.email
import anvil.users
"""One-shot seeding for tables that need default rows.

Call from the Anvil server console:

    anvil.server.call("seed_phase0")

Safe to re-run — existing rows are left untouched.
"""

from __future__ import annotations

import datetime as dt

import anvil.server
from anvil.tables import app_tables


# Defaults from the rollout plan. Edit and re-run to bump.
# Tuple shape: (category, daily_credit_grant, initial_credits_default,
#               storage_max_bytes, storage_max_files, default_kurs_duration_days)
DEFAULT_LIMITS = [
    ("internal", 50,  50,  50 * 1024 * 1024, 100, None),
    ("kurs",     0,   200, 50 * 1024 * 1024, 100, 90),
    ("credits",  0,   0,   50 * 1024 * 1024, 100, None),
    ("free",     0,   0,   0,                0,   None),
]
SUPERUSER_MULTIPLIER = 5

# (pattern, default_category, default_is_superuser, initial_credits, notes)
DEFAULT_WHITELIST = [
    ("@fhi.no", "internal", False, 50, "Seeded at install. Auto-internal for FHI staff."),
]


REQUIRED_TABLES = [
    "users", "email_whitelist", "auth_tokens", "scripts",
    "ai_usage_daily", "limits_config", "audit_log", "signup_requests",
]


def _missing_tables() -> list[str]:
    """Return names of REQUIRED_TABLES that aren't on app_tables yet.

    Anvil materializes tables from anvil.yaml only when a schema-apply
    happens (IDE prompt) or when each table is created manually. Until
    then, app_tables won't have the attribute.
    """
    return [t for t in REQUIRED_TABLES if getattr(app_tables, t, None) is None]


@anvil.server.callable
def check_tables():
    """Diagnostic: list which expected tables exist vs are missing."""
    missing = _missing_tables()
    present = [t for t in REQUIRED_TABLES if t not in missing]
    return {"present": present, "missing": missing}


@anvil.server.callable
def seed_phase0():
    """Seed limits_config and email_whitelist with default rows.

    Bails out with a clear message if any required table is missing
    instead of crashing on AttributeError. Idempotent — re-running on a
    half-seeded state is safe.
    """
    missing = _missing_tables()
    if missing:
        return {
            "ok": False,
            "missing_tables": missing,
            "hint": (
                "Open the Anvil IDE → Data Tables. You'll see a 'Schema "
                "out of sync' banner; click 'Apply'. If that doesn't work, "
                "create each missing table manually (just give it the name "
                "above; columns auto-create on first row). Then re-run "
                "anvil.server.call('seed_phase0')."
            ),
        }

    # Idempotency check has to swallow NoSuchColumnError: on a freshly-
    # created table, auto_create_missing_columns only fires on add_row().
    # First seed run creates the columns; subsequent runs find existing rows.
    def _safe_get(table, **kwargs):
        try:
            return table.get(**kwargs)
        except Exception:
            return None

    limits_added = 0
    for cat, grant, init, bytes_, files, kurs_days in DEFAULT_LIMITS:
        if _safe_get(app_tables.limits_config, category=cat) is not None:
            continue
        app_tables.limits_config.add_row(
            category=cat,
            daily_credit_grant=grant,
            initial_credits_default=init,
            storage_max_bytes=bytes_,
            storage_max_files=files,
            superuser_multiplier=SUPERUSER_MULTIPLIER,
            default_kurs_duration_days=kurs_days,
            notes="seeded by seed_phase0()",
        )
        limits_added += 1

    whitelist_added = 0
    for pattern, cat, is_super, init_credits, notes in DEFAULT_WHITELIST:
        if _safe_get(app_tables.email_whitelist, pattern=pattern) is not None:
            continue
        app_tables.email_whitelist.add_row(
            pattern=pattern,
            default_category=cat,
            default_is_superuser=is_super,
            initial_credits=init_credits,
            default_expires_at=None,
            added_by=None,
            added_at=dt.datetime.utcnow(),
            notes=notes,
        )
        whitelist_added += 1

    return {
        "ok": True,
        "limits_added": limits_added,
        "whitelist_added": whitelist_added,
        "limits_total": len(list(app_tables.limits_config.search())),
        "whitelist_total": len(list(app_tables.email_whitelist.search())),
    }
