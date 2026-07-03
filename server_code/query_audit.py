"""Multi-query audit layer v1: logging + quota for /run_extended.

Spec: safepy/docs/superpowers/specs/2026-07-04-query-audit-layer-design.md.
Pure logic in this module is Anvil-free (testable locally); everything that
touches app_tables imports anvil lazily inside the function.
"""
from datetime import datetime, timedelta, timezone

WINDOW = timedelta(hours=24)

# (level, principal_kind) -> runs per rolling 24h per principal x source.
# None = no quota (still logged). 0 = refuse outright.
BUDGETS = {
    ("public", "user"): None, ("public", "anonymous"): None, ("public", "api_key"): None,
    ("protected", "user"): 100, ("protected", "anonymous"): 25, ("protected", "api_key"): 25,
    ("sensitive", "user"): 30, ("sensitive", "anonymous"): 0, ("sensitive", "api_key"): 0,
}


def classify_principal(alias):
    if isinstance(alias, str) and alias.startswith("user:"):
        return "user"
    if isinstance(alias, str) and alias.startswith("anonymous:"):
        return "anonymous"
    return "api_key"


def check_budget(alias, level, source_ids, count_recent, now=None):
    """Quota gate. count_recent(alias, source_id, since) -> int counts only
    status='ok' rows (failed runs are free — otherwise users burn quota on
    syntax errors). Returns (allowed, norwegian_message_or_None)."""
    kind = classify_principal(alias)
    limit = BUDGETS.get((level or "public", kind))
    if limit is None or not source_ids:
        return True, None
    if limit == 0:
        return False, "Kjøring mot sensitive kilder krever innlogget bruker."
    since = (now or datetime.now(timezone.utc)) - WINDOW
    for sid in source_ids:
        if count_recent(alias, sid, since) >= limit:
            return False, ("Kvoten for kjøringer mot '%s' er brukt opp "
                           "(%d per døgn). Prøv igjen senere." % (sid, limit))
    return True, None


_FP_KEYS = ("verb", "min_n", "groups", "cells_suppressed", "groups_sig", "count_hist")


def collect_fingerprints(result_dict):
    """Release fingerprints from a SafeResult.as_dict(): leaf audits that carry
    groups_sig (stamped by safepy's release helpers). Never includes payloads."""
    out = []
    for leaf in (result_dict or {}).get("results") or []:
        a = leaf.get("audit") or {}
        if "groups_sig" in a:
            out.append({k: a[k] for k in _FP_KEYS if k in a})
    return out
