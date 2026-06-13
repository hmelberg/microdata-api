"""Cross-cutting helpers: API-key auth, rate limit, request logging."""

from __future__ import annotations

import datetime as dt
import hmac
import uuid
from typing import Optional

import anvil.secrets
import anvil.server
import anvil.tables.query as q
from anvil.tables import app_tables

RATE_LIMIT_WINDOW_SEC = 60
RATE_LIMIT_MAX_CALLS = 30  # per key per minute

# eval_runs logging: cap stored free-text and keep a bounded retention window.
# This is a data-minimization tool — we should not hoard full questions/scripts.
EVAL_RUNS_MAX_CHARS = 4000
EVAL_RUNS_RETENTION_DAYS = 90


# ---------------------------------------------------------------------------
# API keys


def _all_api_keys() -> dict[str, str]:
    """Return {alias: key_value} for every secret named API_KEY_*."""
    out: dict[str, str] = {}
    # Anvil has no enumerate-secrets API, so we follow a naming convention
    # and look up a fixed list (edit here when you add a new key alias).
    try:
        aliases_str = anvil.secrets.get_secret("API_KEY_ALIASES") or ""
    except Exception:
        # If the alias-list secret is missing, downgrade to "no keys configured"
        # so the request gets a clean 401 rather than a 500.
        return out
    for alias in aliases_str.split(","):
        alias = alias.strip()
        if not alias:
            continue
        try:
            out[alias] = anvil.secrets.get_secret(f"API_KEY_{alias}")
        except Exception:
            continue
    return out


def authenticate(request) -> Optional[str]:
    """Return the caller's alias if the X-API-Key header matches a
    configured key; else None.
    """
    header_key = None
    headers = getattr(request, "headers", None) or {}
    # Anvil request.headers is a case-insensitive dict-like.
    for h in ("X-API-Key", "x-api-key", "X-Api-Key"):
        if headers.get(h):
            header_key = headers.get(h)
            break
    if not header_key:
        return None

    for alias, value in _all_api_keys().items():
        # Constant-time comparison so a timing side-channel can't be used to
        # recover a configured key byte by byte.
        if value and hmac.compare_digest(str(value), str(header_key)):
            return alias
    return None


# ---------------------------------------------------------------------------
# Rate limit (simple token bucket per key per minute)


def _window_start(now: dt.datetime, window_sec: int) -> dt.datetime:
    """Floor `now` to the start of its fixed window. Pure (no I/O) so it can be
    unit-tested. window_sec should divide evenly into a day (60/300/600/3600…),
    which keeps windows aligned and avoids the naive-utc .timestamp() pitfall
    (which would interpret the value as local time)."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    secs = (now - midnight).total_seconds()
    floored = int(secs - (secs % window_sec))
    return midnight + dt.timedelta(seconds=floored)


def check_rate_limit(
    alias: str,
    max_calls: int = RATE_LIMIT_MAX_CALLS,
    window_sec: int = RATE_LIMIT_WINDOW_SEC,
) -> bool:
    """Return True if the call is allowed, False if it is over the limit for the
    given `alias` within the current window.

    Defensive: a freshly-created `api_usage` table has no columns until the
    first add_row(); a get() against missing columns raises. We treat any
    failure as "no row yet" and then default to True so rate-limit accounting
    never 500s a real request — but we LOG the failure (it was silent before)
    so a broken table doesn't quietly disable rate limiting.
    """
    window_start = _window_start(dt.datetime.utcnow(), window_sec)
    try:
        row = app_tables.api_usage.get(key_alias=alias, window_start=window_start)
    except Exception as exc:
        # column-less table on first run is normal; other errors are not.
        print(f"[rate_limit] api_usage.get failed for {alias!r}: {exc!r}")
        row = None
    if row is None:
        try:
            app_tables.api_usage.add_row(
                key_alias=alias, window_start=window_start, count=1
            )
        except Exception as exc:
            print(f"[rate_limit] api_usage.add_row failed for {alias!r}: {exc!r}")
        return True
    try:
        count = (row["count"] or 0) + 1
        row["count"] = count
        return count <= max_calls
    except Exception as exc:
        print(f"[rate_limit] increment failed for {alias!r} (failing open): {exc!r}")
        return True


# ---------------------------------------------------------------------------
# eval_runs logging


def log_request(
    *,
    endpoint: str,
    question: str,
    lang: str,
    model: str,
    script: str = "",
    variables_used: list[str] | None = None,
    commands_used: list[str] | None = None,
    validation_passed: bool = True,
    validation_tier: str = "static",
    errors: list[dict] | None = None,
    latency_ms: int = 0,
    cache_stats: dict | None = None,
    api_key_alias: str = "",
) -> str:
    request_id = uuid.uuid4().hex
    try:
        app_tables.eval_runs.add_row(
            ts=dt.datetime.utcnow(),
            request_id=request_id,
            endpoint=endpoint,
            # Cap stored free-text; we don't need the whole question/script to
            # debug usage, and this is a data-minimization service.
            question=(question or "")[:EVAL_RUNS_MAX_CHARS],
            lang=lang,
            model=model,
            script=(script or "")[:EVAL_RUNS_MAX_CHARS],
            variables_used=variables_used or [],
            commands_used=commands_used or [],
            validation_passed=validation_passed,
            validation_tier=validation_tier,
            errors=errors or [],
            latency_ms=latency_ms,
            cache_stats=cache_stats or {},
            api_key_alias=api_key_alias,
        )
    except Exception:
        # Best-effort logging — never fail the user request because the
        # log row couldn't be written.
        pass
    return request_id


# ---------------------------------------------------------------------------
# eval_runs retention


def purge_old_eval_runs(retention_days: int = EVAL_RUNS_RETENTION_DAYS) -> int:
    """Delete eval_runs rows older than `retention_days`. Returns the number
    deleted. Wire this to an Anvil Scheduled Task (e.g. daily) in the IDE —
    Anvil schedules are configured there, not in code. Intentionally NOT
    @anvil.server.callable: a destructive purge should not be client-reachable.
    """
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=retention_days)
    deleted = 0
    try:
        for row in app_tables.eval_runs.search(ts=q.less_than(cutoff)):
            row.delete()
            deleted += 1
    except Exception as exc:
        print(f"[purge_old_eval_runs] failed after {deleted} rows: {exc!r}")
    return deleted
