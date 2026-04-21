"""HTTP endpoints exposed by the Anvil app.

Anvil exposes server functions over HTTP via @anvil.server.http_endpoint.
The endpoints are reachable at:

    https://<app>.anvil.app/_/api<path>

All endpoints require a valid X-API-Key header. Responses are JSON; 2xx on
success, 4xx on client errors (bad auth, rate limit, bad body), 5xx on
unexpected server errors.
"""

from __future__ import annotations

import json
import time

import anvil.server
from anvil.server import HttpResponse

import generation
import retrieval
import utils
import validation


def _json(body: dict, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status=status,
        body=json.dumps(body, ensure_ascii=False),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


def _load_body() -> dict:
    req = anvil.server.request
    body = req.body_json
    if body is None and req.body:
        try:
            body = json.loads(req.body.get_bytes().decode("utf-8"))
        except Exception:
            body = None
    return body or {}


def _authenticate_or_fail():
    req = anvil.server.request
    alias = utils.authenticate(req)
    if not alias:
        return None, _json({"error": "invalid or missing X-API-Key"}, status=401)
    if not utils.check_rate_limit(alias):
        return None, _json({"error": "rate limit exceeded"}, status=429)
    return alias, None


# ---------------------------------------------------------------------------
# /query  (smart router: classifies intent, dispatches to the matching mode)


@anvil.server.http_endpoint("/query", methods=["POST"], cross_site_session=False)
def http_query():
    alias, err = _authenticate_or_fail()
    if err:
        return err

    body = _load_body()
    question = (body.get("question") or "").strip()
    if not question:
        return _json({"error": "missing 'question'"}, status=400)
    lang = body.get("lang") or "no"
    max_repair = int(body.get("max_repair", 1))
    deep_validate = bool(body.get("deep_validate", False))

    t0 = time.time()
    envelope = generation.smart_query(
        question=question,
        lang=lang,
        max_repair=max_repair,
        deep_validate=deep_validate,
    )
    latency_ms = int((time.time() - t0) * 1000)

    intent = envelope.get("intent", "qa")
    result = envelope.get("result") or {}

    # Pull mode-specific fields out for structured logging.
    if intent == "script_gen":
        utils.log_request(
            endpoint=f"/query:{intent}",
            question=question,
            lang=envelope.get("lang", lang),
            model=result.get("model", ""),
            script=result.get("script", ""),
            variables_used=result.get("variables_used", []),
            commands_used=result.get("commands_used", []),
            validation_passed=(result.get("validation") or {}).get("passed", False),
            validation_tier=(result.get("validation") or {}).get("tier_ran", "static"),
            errors=(result.get("validation") or {}).get("errors", []),
            latency_ms=latency_ms,
            cache_stats=result.get("cache_stats") or {},
            api_key_alias=alias,
        )
    else:
        utils.log_request(
            endpoint=f"/query:{intent}",
            question=question,
            lang=envelope.get("lang", lang),
            model=result.get("model", ""),
            latency_ms=latency_ms,
            cache_stats=result.get("cache_stats") or {},
            api_key_alias=alias,
        )

    envelope["latency_ms"] = latency_ms
    return _json(envelope)


# ---------------------------------------------------------------------------
# /generate


@anvil.server.http_endpoint("/generate", methods=["POST"], cross_site_session=False)
def http_generate():
    alias, err = _authenticate_or_fail()
    if err:
        return err

    body = _load_body()
    question = (body.get("question") or "").strip()
    if not question:
        return _json({"error": "missing 'question'"}, status=400)
    lang = body.get("lang") or "no"
    max_repair = int(body.get("max_repair", 1))
    deep_validate = bool(body.get("deep_validate", False))

    t0 = time.time()
    result = generation.generate_script(
        question=question, lang=lang, max_repair=max_repair
    )

    if deep_validate and result.get("script"):
        deep = validation.validate_dry_run(result["script"])
        result["validation"] = deep.to_dict()

    latency_ms = int((time.time() - t0) * 1000)
    utils.log_request(
        endpoint="/generate",
        question=question,
        lang=lang,
        model=result.get("model", ""),
        script=result.get("script", ""),
        variables_used=result.get("variables_used", []),
        commands_used=result.get("commands_used", []),
        validation_passed=(result.get("validation") or {}).get("passed", False),
        validation_tier=(result.get("validation") or {}).get("tier_ran", "static"),
        errors=(result.get("validation") or {}).get("errors", []),
        latency_ms=latency_ms,
        cache_stats=result.get("cache_stats") or {},
        api_key_alias=alias,
    )
    result["latency_ms"] = latency_ms
    return _json(result)


# ---------------------------------------------------------------------------
# /revise


@anvil.server.http_endpoint("/revise", methods=["POST"], cross_site_session=False)
def http_revise():
    alias, err = _authenticate_or_fail()
    if err:
        return err

    body = _load_body()
    script = body.get("script") or ""
    revision = (body.get("revision") or "").strip()
    if not script:
        return _json({"error": "missing 'script'"}, status=400)
    if not revision:
        return _json({"error": "missing 'revision'"}, status=400)
    lang = body.get("lang") or "no"
    max_repair = int(body.get("max_repair", 1))
    deep_validate = bool(body.get("deep_validate", False))

    t0 = time.time()
    result = generation.revise_script(
        script=script, revision=revision, lang=lang, max_repair=max_repair
    )

    if deep_validate and result.get("script"):
        deep = validation.validate_dry_run(result["script"])
        result["validation"] = deep.to_dict()

    latency_ms = int((time.time() - t0) * 1000)
    utils.log_request(
        endpoint="/revise",
        question=revision,
        lang=lang,
        model=result.get("model", ""),
        script=result.get("script", ""),
        variables_used=result.get("variables_used", []),
        commands_used=result.get("commands_used", []),
        validation_passed=(result.get("validation") or {}).get("passed", False),
        validation_tier=(result.get("validation") or {}).get("tier_ran", "static"),
        errors=(result.get("validation") or {}).get("errors", []),
        latency_ms=latency_ms,
        cache_stats=result.get("cache_stats") or {},
        api_key_alias=alias,
    )
    result["latency_ms"] = latency_ms
    return _json(result)


# ---------------------------------------------------------------------------
# /ask


@anvil.server.http_endpoint("/ask", methods=["POST"], cross_site_session=False)
def http_ask():
    alias, err = _authenticate_or_fail()
    if err:
        return err
    body = _load_body()
    question = (body.get("question") or "").strip()
    if not question:
        return _json({"error": "missing 'question'"}, status=400)
    lang = body.get("lang") or "no"

    t0 = time.time()
    result = generation.answer_question(question=question, lang=lang)
    latency_ms = int((time.time() - t0) * 1000)

    utils.log_request(
        endpoint="/ask",
        question=question,
        lang=lang,
        model=result.get("model", ""),
        latency_ms=latency_ms,
        cache_stats=result.get("cache_stats") or {},
        api_key_alias=alias,
    )
    result["latency_ms"] = latency_ms
    return _json(result)


# ---------------------------------------------------------------------------
# /validate


@anvil.server.http_endpoint("/validate", methods=["POST"], cross_site_session=False)
def http_validate():
    alias, err = _authenticate_or_fail()
    if err:
        return err
    body = _load_body()
    script = body.get("script") or ""
    deep = bool(body.get("deep", False))
    if not script:
        return _json({"error": "missing 'script'"}, status=400)

    t0 = time.time()
    result = (
        validation.validate_dry_run(script).to_dict()
        if deep
        else validation.validate_static(script).to_dict()
    )
    latency_ms = int((time.time() - t0) * 1000)
    utils.log_request(
        endpoint="/validate",
        question="",
        lang="",
        model="",
        script=script,
        variables_used=result.get("variables_used", []),
        commands_used=result.get("commands_used", []),
        validation_passed=result.get("passed", False),
        validation_tier=result.get("tier_ran", "static"),
        errors=result.get("errors", []),
        latency_ms=latency_ms,
        api_key_alias=alias,
    )
    result["latency_ms"] = latency_ms
    return _json(result)


# ---------------------------------------------------------------------------
# /variables/search


@anvil.server.http_endpoint("/variables/search", methods=["GET"], cross_site_session=False)
def http_variables_search():
    import traceback
    try:
        alias, err = _authenticate_or_fail()
        if err:
            return err
        req = anvil.server.request
        # Anvil's request.query_params behaviour varies by runtime version;
        # try the canonical attribute first, fall back to parsing the path.
        try:
            params = req.query_params or {}
        except Exception:
            params = {}
        if not params:
            try:
                from urllib.parse import urlparse, parse_qs
                qs = urlparse(req.path or "").query
                if not qs and "?" in (req.path or ""):
                    qs = (req.path or "").split("?", 1)[1]
                parsed = parse_qs(qs)
                params = {k: v[0] for k, v in parsed.items()}
            except Exception:
                pass

        q = (params.get("q") or "").strip()
        lang = params.get("lang") or "no"
        try:
            k = int(params.get("k", 15))
        except (TypeError, ValueError):
            k = 15
        if not q:
            return _json(
                {"error": "missing 'q'", "params_seen": list(params.keys())},
                status=400,
            )

        results = retrieval.server_variable_search(query=q, lang=lang, k=k)
        return _json({"results": results})
    except Exception as exc:
        # Surface the real cause so we can debug instead of staring at a generic 500.
        return _json(
            {
                "error": "internal error",
                "exception": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc().splitlines()[-6:],
            },
            status=500,
        )
