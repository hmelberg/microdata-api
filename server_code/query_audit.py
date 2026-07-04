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
    # Fail closed: an unrecognized (level, kind) combo — a typo, or a future
    # level not yet wired into BUDGETS — must not fall through to "unlimited"
    # via .get()'s None default. Fall back to the PROTECTED budget for that
    # principal kind rather than trusting an unknown level as public.
    limit = BUDGETS.get((level or "public", kind), BUDGETS[("protected", kind)])
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


_FP_KEYS = ("verb", "min_n", "groups", "cells_suppressed", "groups_sig", "count_hist",
           # grouping-column identity: column NAMES are schema, disclosure-free
           # (unlike the values inside those columns), and v2 needs them to
           # correlate fingerprints across a script's multiple releases.
           "by", "value", "agg", "col", "row", "index", "columns", "aggfunc")


def collect_fingerprints(result_dict):
    """Release fingerprints from a SafeResult.as_dict(): leaf audits that carry
    groups_sig (stamped by safepy's release helpers). Never includes payloads."""
    out = []
    for leaf in (result_dict or {}).get("results") or []:
        a = leaf.get("audit") or {}
        if "groups_sig" in a:
            out.append({k: a[k] for k in _FP_KEYS if k in a})
    return out


_LEVEL_ORDER = {"public": 0, "protected": 1, "sensitive": 2}


def resolve_run_levels(sources_req):
    """(source_ids, strictest_level) for a /run_extended request. Reuses the
    registry resolution the shims use (source_registry.resolve_source);
    unresolvable sources are treated conservatively here (level='protected')
    for budget purposes — the shim will fail them properly during the run."""
    import source_registry
    ids, worst = [], "public"
    for s in (sources_req or []):
        sid = (s.get("source_id") or "").strip() if isinstance(s, dict) else ""
        if not sid:
            continue
        ids.append(sid)
        try:
            src = source_registry.resolve_source(sid)
            lvl = src.get("level") or "public"
        except Exception:
            lvl = "protected"        # unknown/unresolvable -> conservative
        if _LEVEL_ORDER.get(lvl, 1) > _LEVEL_ORDER.get(worst, 0):
            worst = lvl
    return ids, (worst if ids else None)


def count_recent_anvil(alias, source_id, since):
    """count_recent for check_budget: successful runs by `alias` against
    `source_id` since `since`. Anvil-only (lazy import)."""
    from anvil.tables import app_tables
    import anvil.tables.query as q
    n = 0
    for row in app_tables.audit_log.search(principal=alias, status="ok",
                                           ts=q.greater_than(since)):
        if source_id in (row["source_ids"] or []):
            n += 1
    return n


def pop_audit_info(out):
    """Pop internal _audit_* keys from a run result dict (None-safe).
    Returns (releases, level). MUST be called before the result reaches the client."""
    if not isinstance(out, dict):
        return [], None
    return out.pop("_audit_releases", []), out.pop("_audit_level", None)


def build_log_row(alias, request_id, source_ids, level, dialect, script,
                  status, error, releases, latency_ms) -> dict:
    """Pure construction of one audit_log row (the 12 columns of that table).
    Truncates script_head to 20000 chars and error to 1000 so a runaway script
    or traceback can't blow up storage; classifies the principal kind.

    script_head was capped at 2000 chars by the original audit-layer spec
    (2026-07-04-query-audit-layer-design.md); the owner's audit-browsing
    feature (2026-07-04) needs admins to read whole scripts in the CSV
    export, so it's bumped to 20000 here."""
    return {
        "ts": datetime.now(timezone.utc), "request_id": request_id,
        "principal": alias or "", "principal_kind": classify_principal(alias or ""),
        "source_ids": list(source_ids or []), "level": level, "dialect": dialect,
        "script_head": (script or "")[:20000], "status": status,
        "error": (str(error)[:1000] if error else None),
        "releases": list(releases or []), "latency_ms": latency_ms,
    }


def log_run(alias, request_id, source_ids, level, dialect, script,
            status, error, releases, latency_ms):
    """One audit row per run. Never raises (logging must not break runs)."""
    try:
        from anvil.tables import app_tables
        app_tables.audit_log.add_row(**build_log_row(
            alias, request_id, source_ids, level, dialect, script,
            status, error, releases, latency_ms))
    except Exception as exc:            # noqa: BLE001
        print(f"query_audit.log_run failed: {exc}")
