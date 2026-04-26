import anvil.email
import anvil.users
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
import classifier
import retrieval
import utils
import validation
import auth


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


# Auth lives in auth.py so future phases (Bearer tokens, role checks,
# credit enforcement) can extend it without churning this module. Phase 0
# behavior is unchanged: X-API-Key only.
_authenticate_or_fail = auth.authenticate_or_fail


# ---------------------------------------------------------------------------
# /query  (smart router: classifies intent, dispatches to the matching mode)


@anvil.server.http_endpoint("/query", methods=["POST"], cross_site_session=False, enable_cors=True)
def http_query():
    """Smart router. Fast intents (qa, variable_search) run synchronously
    and return the full envelope. Slow intent (script_gen) runs as a
    background task — response carries `task_id` + `mode: "async"` and the
    client polls /task_status until completion.
    """
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

    # Cheap classifier call up front so we can decide sync vs async.
    cls = classifier.classify(question, default_lang=lang)
    intent = cls.get("intent", "qa")
    resolved_lang = cls.get("lang", lang)

    classifier_block = {
        "model": cls.get("model"),
        "usage": cls.get("usage") or {},
        "fallback": cls.get("fallback"),
    }

    if intent == "script_gen":
        # Async — Anvil's 30s HTTP cap doesn't apply to background tasks.
        task = anvil.server.launch_background_task(
            "bg_smart_query",
            question, resolved_lang, max_repair, deep_validate,
        )
        task_id = task.get_id()
        utils.log_request(
            endpoint="/query:script_gen:launched",
            question=question,
            lang=resolved_lang,
            model="",
            api_key_alias=auth.principal_alias(alias),
        )
        return _json({
            "intent": intent,
            "lang": resolved_lang,
            "terms": cls.get("terms") or [],
            "classifier": classifier_block,
            "task_id": task_id,
            "mode": "async",
        })

    # Sync paths — qa, variable_search.
    t0 = time.time()
    if intent == "variable_search":
        hits = retrieval.server_variable_search(query=question, lang=resolved_lang, k=15)
        result = {"variables": hits}
    else:  # qa or unknown
        result = generation.answer_question(question=question, lang=resolved_lang)
    latency_ms = int((time.time() - t0) * 1000)

    utils.log_request(
        endpoint=f"/query:{intent}",
        question=question,
        lang=resolved_lang,
        model=result.get("model", ""),
        latency_ms=latency_ms,
        cache_stats=result.get("cache_stats") or {},
        api_key_alias=auth.principal_alias(alias),
    )

    return _json({
        "intent": intent,
        "lang": resolved_lang,
        "terms": cls.get("terms") or [],
        "classifier": classifier_block,
        "result": result,
        "mode": "sync",
        "latency_ms": latency_ms,
    })


# ---------------------------------------------------------------------------
# /task_status  (poll a background task launched by /query)


@anvil.server.http_endpoint("/task_status", methods=["GET"], cross_site_session=False, enable_cors=True)
def http_task_status(**kwargs):
    alias, err = _authenticate_or_fail()
    if err:
        return err
    task_id = (kwargs.get("task_id") or "").strip()
    if not task_id:
        return _json({"error": "missing 'task_id'"}, status=400)

    try:
        task = anvil.server.get_background_task(task_id)
    except Exception as exc:
        return _json({"error": f"task lookup failed: {exc}"}, status=404)

    if task is None:
        return _json({"error": "task not found"}, status=404)

    if not task.is_completed():
        return _json({"status": "running"})

    term = task.get_termination_status()
    if term == "completed":
        result = task.get_return_value()  # full smart_query envelope
        # Log the completed run with full structured fields.
        try:
            r = (result or {}).get("result") or {}
            utils.log_request(
                endpoint=f"/query:{result.get('intent','script_gen')}:completed",
                question="",  # original question already logged at launch
                lang=(result or {}).get("lang", ""),
                model=r.get("model", ""),
                script=r.get("script", ""),
                variables_used=r.get("variables_used", []),
                commands_used=r.get("commands_used", []),
                validation_passed=(r.get("validation") or {}).get("passed", False),
                validation_tier=(r.get("validation") or {}).get("tier_ran", "static"),
                errors=(r.get("validation") or {}).get("errors", []),
                cache_stats=r.get("cache_stats") or {},
                api_key_alias=auth.principal_alias(alias),
            )
        except Exception:
            pass
        return _json({"status": "completed", "result": result})

    # killed / failed
    err_obj = task.get_error()
    err_msg = getattr(err_obj, "message", None) or str(err_obj)
    return _json({"status": term or "failed", "error": err_msg})


# ---------------------------------------------------------------------------
# /generate


@anvil.server.http_endpoint("/generate", methods=["POST"], cross_site_session=False, enable_cors=True)
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
        question=question, lang=lang, max_repair=max_repair,
        deep_validate=deep_validate,
    )

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
        api_key_alias=auth.principal_alias(alias),
    )
    result["latency_ms"] = latency_ms
    return _json(result)


# ---------------------------------------------------------------------------
# /revise


@anvil.server.http_endpoint("/revise", methods=["POST"], cross_site_session=False, enable_cors=True)
def http_revise():
    """Async — revisions can exceed Anvil's 30s HTTP cap, so we launch a
    background task and let the client poll /task_status. The completed
    envelope is shaped like bg_smart_query's: {intent, lang, result: {...}}.
    """
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

    task = anvil.server.launch_background_task(
        "bg_revise_script",
        script, revision, lang, max_repair, deep_validate,
    )
    task_id = task.get_id()
    utils.log_request(
        endpoint="/revise:launched",
        question=revision,
        lang=lang,
        model="",
        api_key_alias=auth.principal_alias(alias),
    )
    return _json({
        "intent": "revise",
        "lang": lang,
        "task_id": task_id,
        "mode": "async",
    })


# ---------------------------------------------------------------------------
# /ask


@anvil.server.http_endpoint("/ask", methods=["POST"], cross_site_session=False, enable_cors=True)
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
        api_key_alias=auth.principal_alias(alias),
    )
    result["latency_ms"] = latency_ms
    return _json(result)


# ---------------------------------------------------------------------------
# /validate


@anvil.server.http_endpoint("/validate", methods=["POST"], cross_site_session=False, enable_cors=True)
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
        api_key_alias=auth.principal_alias(alias),
    )
    result["latency_ms"] = latency_ms
    return _json(result)


# ---------------------------------------------------------------------------
# /variables/search


@anvil.server.http_endpoint("/variables/search", methods=["GET"], cross_site_session=False, enable_cors=True)
def http_variables_search(**kwargs):
    # Anvil passes GET query-string params as function kwargs.
    alias, err = _authenticate_or_fail()
    if err:
        return err
    q = (kwargs.get("q") or "").strip()
    lang = kwargs.get("lang") or "no"
    try:
        k = int(kwargs.get("k", 15))
    except (TypeError, ValueError):
        k = 15
    if not q:
        return _json({"error": "missing 'q'"}, status=400)
    results = retrieval.server_variable_search(query=q, lang=lang, k=k)
    return _json({"results": results})


# ---------------------------------------------------------------------------
# /judge — LLM-judge for the eval harness. Lives server-side so the local
# eval script can stay free of ANTHROPIC_API_KEY.


@anvil.server.http_endpoint("/judge", methods=["POST"], cross_site_session=False, enable_cors=True)
def http_judge():
    alias, err = _authenticate_or_fail()
    if err:
        return err
    body = _load_body()
    question = (body.get("question") or "").strip()
    generated = body.get("generated_script") or ""
    reference = body.get("reference_script") or ""
    lang = body.get("lang") or "no"
    if not question:
        return _json({"error": "missing 'question'"}, status=400)

    t0 = time.time()
    result = generation.judge_script(
        question=question,
        generated_script=generated,
        reference_script=reference,
        lang=lang,
    )
    latency_ms = int((time.time() - t0) * 1000)

    utils.log_request(
        endpoint="/judge",
        question=question,
        lang=lang,
        model=result.get("model", ""),
        latency_ms=latency_ms,
        cache_stats=result.get("cache_stats") or {},
        api_key_alias=auth.principal_alias(alias),
    )
    result["latency_ms"] = latency_ms
    return _json(result)
