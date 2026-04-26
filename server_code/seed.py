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


@anvil.server.callable
def seed_phase0():
    """Seed limits_config and email_whitelist with default rows.

    Returns a summary dict of how many rows were added vs already present.
    """
    limits_added = 0
    for cat, grant, init, bytes_, files, kurs_days in DEFAULT_LIMITS:
        if app_tables.limits_config.get(category=cat) is not None:
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
        if app_tables.email_whitelist.get(pattern=pattern) is not None:
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
        "limits_added": limits_added,
        "whitelist_added": whitelist_added,
        "limits_total": len(list(app_tables.limits_config.search())),
        "whitelist_total": len(list(app_tables.email_whitelist.search())),
    }
