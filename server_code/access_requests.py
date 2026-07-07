# microdata-api/server_code/access_requests.py
"""Access-request / grant workflow (roadmap §2a): a caller denied by
source_access.caller_allowed() can ask the owner for access instead of
hitting a dead end. The owner reviews pending requests in deldata.html and
approves/denies; approval appends the requester's email to the source's
access_policy.emails (source_access.py's existing "listed" allowlist — no
new access-control mechanism, just a UI/workflow around the one that
already exists).

Pending requests are stored as a plain list on the sources row itself
(pending_requests: [{"email": ..., "requested_at": iso-string}]) — an
auto-created column (same pattern as auth.py's anonymous_label/shared_label),
not a new table. Kept here rather than in owner_sources.py because this
module is about the OTHER side of registration: a non-owner asking in, not
the owner registering a source.

/access_request is intentionally answer-alike whether the source exists,
already allows the caller, or doesn't exist at all — /source_access and
/source_info both avoid leaking existence for denied/unknown sources, and
a request endpoint that behaved differently per case would leak the same
thing through a side door.
"""
from __future__ import annotations

import datetime as dt
import re

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _utcnow():
    return dt.datetime.now(dt.timezone.utc)


def normalize_email(email: str) -> str:
    """-> lowercased, trimmed email. Raises ValueError (norsk) if not
    plausibly an email address."""
    e = (email or "").strip().lower()
    if not _EMAIL_RE.match(e):
        raise ValueError("ugyldig e-postadresse")
    return e


def already_pending(pending: list, email: str) -> bool:
    """True if email already has an unresolved request in pending (dedupe —
    a double-click or repeat visit shouldn't pile up duplicate rows or
    re-notify the owner every time)."""
    email = email.strip().lower()
    return any((str(p.get("email") or "").strip().lower() == email) for p in (pending or []))


def add_pending(pending: list, email: str) -> tuple[list, bool]:
    """-> (new_pending_list, was_added). was_added is False if email was
    already pending (caller should skip the notification email in that case)."""
    pending = list(pending or [])
    if already_pending(pending, email):
        return pending, False
    pending.append({"email": email, "requested_at": _utcnow().isoformat()})
    return pending, True


def resolve_pending(pending: list, email: str) -> list:
    """-> pending list with every entry for email removed (approved or denied
    — either way it's no longer pending)."""
    email = email.strip().lower()
    return [p for p in (pending or [])
            if str(p.get("email") or "").strip().lower() != email]


def grant_email(access_policy: dict | None, email: str) -> dict:
    """-> a new access_policy dict with email added to the emails allowlist
    (deduped). Preserves domains/audience; creates the policy if absent."""
    email = str(email or "").strip().lower()
    policy = dict(access_policy or {})
    emails = [str(e).strip().lower() for e in (policy.get("emails") or [])]
    if email not in emails:
        emails.append(email)
    policy["emails"] = emails
    return policy


# ---------------------------------------------------------------------------
# HTTP endpoints (Anvil). Kept below the pure logic so tests never import anvil.

try:
    import anvil.email
    import anvil.server
    import anvil.tables as tables
    from anvil.tables import app_tables
    import auth
    _ANVIL = True
except Exception:            # pure test run
    _ANVIL = False


if _ANVIL:

    def _json(body, status=200):
        return anvil.server.HttpResponse(
            status=status,
            body=__import__("json").dumps(body, ensure_ascii=False),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    def _load_body() -> dict:
        req = anvil.server.request
        body = req.body_json
        if body is None and req.body:
            try:
                body = __import__("json").loads(req.body.get_bytes().decode("utf-8"))
            except Exception:
                body = None
        return body or {}

    def _cell(row, name, default=None):
        try:
            return row[name]
        except Exception:
            return default

    def _audit(email, action, detail):
        try:
            app_tables.audit_log.add_row(when=_utcnow(), who=email,
                                         action=action, detail=detail)
        except Exception:
            pass  # auditing must never block the operation itself

    # Generic response for /access_request — identical regardless of whether
    # the source exists, is already open to the caller, or the email is
    # malformed-but-plausible-looking, so a denied caller can't use this
    # endpoint to learn anything /source_access wouldn't already tell them.
    _GENERIC_OK = {"ok": True,
                   "message": "Hvis kilden finnes og krever godkjenning, "
                              "er eieren varslet."}

    def _notify_owner(owner_email, source_name, source_id, requester_email):
        try:
            html = (
                f"<p>Hei,</p>"
                f"<p><strong>{requester_email}</strong> har bedt om tilgang til "
                f"kilden din <strong>{source_name}</strong> ({source_id}) i "
                f"Microdata Script Runner.</p>"
                f"<p>Gå til <a href=\"https://micro.fhi.dev/deldata.html\">"
                f"Del data</a> for å godkjenne eller avslå.</p>"
            )
            anvil.email.send(to=owner_email, subject="Forespørsel om datatilgang",
                             html=html)
        except Exception:
            pass  # best-effort — Anvil's free tier limits sends/day; never block the request

    @tables.in_transaction
    def _add_pending_request(sid, email):
        """Atomic read-check-write (2026-07-07 fix): re-fetches the row inside
        the transaction so a retry — triggered by a conflict with a concurrent
        /access_request for a DIFFERENT email, or a concurrent /decide on the
        SAME source — sees fresh pending_requests, not a stale snapshot from
        before the endpoint call started. Without this, one of two concurrent
        requests could silently overwrite the other's pending entry, and a
        request landing mid-approve could be clobbered by the approve's
        now-stale write. Returns (row_or_None, added)."""
        row = app_tables.sources.get(source_id=sid)
        if row is None or row["status"] == "deleted":
            return None, False
        pending, added = add_pending(_cell(row, "pending_requests") or [], email)
        if added:
            row["pending_requests"] = pending   # auto-creates the column on first use
        return row, added

    @anvil.server.http_endpoint("/access_request", methods=["POST"],
                                cross_site_session=False, enable_cors=True)
    def http_access_request():
        body = _load_body()
        try:
            email = normalize_email(body.get("email"))
        except ValueError:
            return _json(_GENERIC_OK)   # still generic — don't confirm/deny format issues either
        sid = (body.get("source_id") or "").strip()
        if not sid:
            return _json(_GENERIC_OK)
        row, added = _add_pending_request(sid, email)
        if row is None:
            return _json(_GENERIC_OK)
        if added:
            owner_email = _cell(row, "owner_email") or ""
            if owner_email:
                _notify_owner(owner_email, _cell(row, "name") or sid, sid, email)
            _audit(email, "access_request", sid)
        return _json(_GENERIC_OK)

    @tables.in_transaction
    def _apply_decision(sid, owner_email, email, decision):
        """Atomic read-check-write (2026-07-07 fix, same reasoning as
        _add_pending_request above): re-fetches the row inside the
        transaction so this can't clobber — or be clobbered by — a
        concurrent /access_request or a second /decide call landing between
        this call's read and write. Returns the row on success, None if
        unknown/not-owner (caller maps that to the 404)."""
        row = app_tables.sources.get(source_id=sid)
        if row is None or (_cell(row, "owner_email") or "") != owner_email:
            return None
        pending = _cell(row, "pending_requests") or []
        row["pending_requests"] = resolve_pending(pending, email)
        if decision == "approve":
            row["access_policy"] = grant_email(_cell(row, "access_policy"), email)
        return row

    @anvil.server.http_endpoint("/access_request/decide", methods=["POST"],
                                cross_site_session=False, enable_cors=True)
    def http_access_request_decide():
        principal, err = auth.authenticate_or_fail()
        if err:
            return err
        user = auth.principal_user(principal)
        if user is None:
            return _json({"error": "krever innlogget bruker"}, status=403)
        body = _load_body()
        sid = (body.get("source_id") or "").strip()
        try:
            email = normalize_email(body.get("email"))
        except ValueError:
            return _json({"error": "ugyldig e-postadresse"}, status=400)
        decision = (body.get("decision") or "").strip()
        if decision not in ("approve", "deny"):
            return _json({"error": "decision må være approve eller deny"}, status=400)
        row = _apply_decision(sid, user["email"], email, decision)
        if row is None:
            return _json({"error": f"ukjent kilde: {sid}"}, status=404)
        _audit(user["email"], "access_request_" + decision, f"{sid}:{email}")
        return _json({"ok": True})
