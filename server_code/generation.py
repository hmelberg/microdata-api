"""Claude call with prompt caching + tool use.

Two entry points:
    generate_script(question, lang, max_repair) -> dict
    answer_question(question, lang)              -> dict

Both use the same cached prefix (commands reference + grammar + canonical
examples) so requests across workers share the Anthropic cache as long as
the prefix hash is stable.
"""

from __future__ import annotations

import json
from typing import Any

import anvil.secrets
import anvil.server
from anthropic import Anthropic

import classifier
import prompts
import retrieval
import validation

DEFAULT_MODEL = "claude-sonnet-4-6"
REPAIR_MODEL = "claude-opus-4-7"  # used only if the second repair also fails


def _client() -> Anthropic:
    api_key = anvil.secrets.get_secret("ANTHROPIC_API_KEY")
    return Anthropic(api_key=api_key)


TOOLS = [
    {
        "name": "lookup_variable",
        "description": (
            "Resolve a term or phrase to one or more microdata.no variables. "
            "ONLY use this when the candidate list in the user turn does not "
            "contain a variable that fits the request. The candidates are "
            "already ranked by relevance — prefer them. Make at most one "
            "tool call per request. Supports Norwegian and English queries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A term, phrase, or variable name."},
                "lang": {"type": "string", "enum": ["no", "en"], "default": "no"},
                "k": {"type": "integer", "default": 8, "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
        },
    }
]


def _dispatch_tool(name: str, args: dict) -> Any:
    if name == "lookup_variable":
        return retrieval.lookup_variable(
            query=args.get("query", ""),
            lang=args.get("lang", "no"),
            k=int(args.get("k", 8)),
        )
    return {"error": f"unknown tool: {name}"}


def _cached_prefix_block() -> dict:
    return {
        "type": "text",
        "text": prompts.cached_prefix(),
        "cache_control": {"type": "ephemeral"},
    }


def _parse_json_response(text: str) -> dict | None:
    text = text.strip()
    # Tolerate a leading/trailing code fence even though the contract says plain JSON.
    if text.startswith("```"):
        text = text.strip("`")
        first_nl = text.find("\n")
        if first_nl > 0:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[:-3]
    try:
        return json.loads(text)
    except Exception:
        return None


import re as _re

_JSON_OBJ_RE = _re.compile(r"\{(?:[^{}]|(?:\{[^{}]*\}))*\}", _re.DOTALL)


def _recover_partial_json(raw: str) -> dict | None:
    """Last-ditch JSON recovery from raw model output that didn't parse
    cleanly (e.g. extra prose, partial markdown). Finds the largest JSON
    object literal in the text and tries to load it. Returns None if
    nothing survives.
    """
    if not raw:
        return None
    candidates = _JSON_OBJ_RE.findall(raw)
    if not candidates:
        return None
    # Try largest first — most likely to be the intended payload.
    candidates.sort(key=len, reverse=True)
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def _extract_script_from_raw(raw: str) -> str:
    """If the model wrote a fenced code block but the wrapper JSON is
    malformed, salvage the script body so the user gets *something*.
    """
    if not raw:
        return ""
    # Look for ```microdata ... ``` fence
    m = _re.search(r"```(?:microdata)?\s*\n(.*?)```", raw, _re.DOTALL)
    if m:
        return m.group(1).strip()
    # Look for any fenced block
    m = _re.search(r"```\s*\n(.*?)```", raw, _re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


def _run_tool_loop(
    client: Anthropic,
    model: str,
    system: str,
    messages: list[dict],
    max_tool_turns: int = 15,
) -> tuple[dict | None, dict, str]:
    """Return (parsed_json, usage, raw_text)."""
    for _ in range(max_tool_turns):
        resp = client.messages.create(
            model=model,
            max_tokens=2048,
            system=system,
            tools=TOOLS,
            messages=messages,
        )
        if resp.stop_reason == "tool_use":
            tool_results = []
            assistant_blocks = []
            for block in resp.content:
                if block.type == "tool_use":
                    out = _dispatch_tool(block.name, block.input or {})
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(out, ensure_ascii=False),
                        }
                    )
                assistant_blocks.append(block.model_dump())
            messages.append({"role": "assistant", "content": assistant_blocks})
            messages.append({"role": "user", "content": tool_results})
            continue
        # Terminal: pick the first text block.
        text_out = ""
        for block in resp.content:
            if block.type == "text":
                text_out = block.text
                break
        parsed = _parse_json_response(text_out)
        usage = resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else dict(resp.usage)
        return parsed, usage, text_out
    return None, {}, ""


# ---------------------------------------------------------------------------
# Script generation


def _assemble_generate_user_turn(
    question: str,
    lang: str,
    candidates: list[dict],
    examples: list[dict],
    manual_sections: list[dict],
) -> list[dict]:
    dynamic = "\n\n".join(
        filter(
            None,
            [
                prompts.render_variable_candidates(candidates),
                prompts.render_retrieved_examples(examples),
                prompts.render_manual_sections(manual_sections),
            ],
        )
    )

    return [
        _cached_prefix_block(),
        {
            "type": "text",
            "text": (
                f"# User request\n\n**Language:** {lang}\n\n"
                f"**Question:** {question}\n\n"
                f"{dynamic}\n\n"
                f"{prompts.GENERATE_OUTPUT_CONTRACT}"
            ),
        },
    ]


def generate_script(
    question: str,
    lang: str = "no",
    max_repair: int = 1,
) -> dict:
    client = _client()

    # Retrieval. Wider candidate set so the model rarely needs lookup_variable
    # mid-generation, which keeps total latency under Anvil's 30s execution cap.
    cmd_keywords = []
    candidates = retrieval.search_variables(question, lang=lang, k=25)
    examples = retrieval.search_examples(question, lang=lang, k=3, boost_commands=cmd_keywords)
    manual_sections = retrieval.search_manual(question, lang=lang, k=2)

    messages: list[dict] = [
        {
            "role": "user",
            "content": _assemble_generate_user_turn(
                question, lang, candidates, examples, manual_sections
            ),
        }
    ]
    parsed, usage, raw = _run_tool_loop(
        client, DEFAULT_MODEL, prompts.SYSTEM_PROMPT, messages
    )

    # Recovery for empty/non-JSON model output: try partial JSON extraction,
    # then fall back to salvaging any fenced code block as the script body.
    recovery_note = ""
    if parsed is None:
        parsed = _recover_partial_json(raw)
        if parsed is not None:
            recovery_note = "Output was partially malformed; recovered via JSON extraction."
        else:
            salvaged = _extract_script_from_raw(raw)
            if salvaged:
                parsed = {
                    "script": salvaged,
                    "rationale": "Output was unstructured; salvaged code block as script.",
                    "variables_used": [],
                    "commands_used": [],
                }
                recovery_note = "Output had no JSON; salvaged a fenced code block."
            else:
                # Truly nothing usable. Return raw text as rationale so user sees something.
                return {
                    "script": "",
                    "rationale": ("Modellen returnerte ikke et brukbart svar. Rå-output:\n\n"
                                  + (raw or "(tomt)")[:1500]),
                    "variables_used": [],
                    "commands_used": [],
                    "validation": {"passed": False, "tier_ran": "static", "errors": [
                        {"kind": "parse", "message": "No script could be recovered from model output."}
                    ]},
                    "model": DEFAULT_MODEL,
                    "cache_stats": usage,
                }

    script = parsed.get("script", "") or ""
    vr = validation.validate_static(script)

    # Track best-so-far across repair attempts so a worse repair doesn't
    # discard a better earlier draft.
    best = {
        "parsed": parsed,
        "script": script,
        "vr": vr,
        "model": DEFAULT_MODEL,
    }

    def _is_better(new_vr, old_vr) -> bool:
        # Fewer errors → better. If counts equal, prefer the one with no
        # unknown_variable / unknown_command (only soft errors).
        if len(new_vr.errors) < len(old_vr.errors):
            return True
        if len(new_vr.errors) > len(old_vr.errors):
            return False
        soft_kinds = {"parse", "runtime"}
        new_hard = sum(1 for e in new_vr.errors if e.kind not in soft_kinds)
        old_hard = sum(1 for e in old_vr.errors if e.kind not in soft_kinds)
        return new_hard < old_hard

    attempts = 0
    current_model = DEFAULT_MODEL
    while not best["vr"].passed and attempts < max_repair:
        attempts += 1
        if attempts == 2:
            current_model = REPAIR_MODEL
        messages.append({"role": "assistant", "content": json.dumps(best["parsed"], ensure_ascii=False)})
        messages.append(
            {
                "role": "user",
                "content": (
                    prompts.REPAIR_INSTRUCTION
                    + "\n\nErrors:\n"
                    + json.dumps([e.__dict__ for e in best["vr"].errors], ensure_ascii=False)
                    + "\n\n"
                    + prompts.GENERATE_OUTPUT_CONTRACT
                ),
            }
        )
        new_parsed, usage, new_raw = _run_tool_loop(
            client, current_model, prompts.SYSTEM_PROMPT, messages
        )
        if new_parsed is None:
            new_parsed = _recover_partial_json(new_raw)
            if new_parsed is None:
                # Repair attempt produced nothing usable — keep best, stop.
                break
        new_script = new_parsed.get("script", "") or ""
        new_vr = validation.validate_static(new_script)
        if _is_better(new_vr, best["vr"]):
            best = {
                "parsed": new_parsed,
                "script": new_script,
                "vr": new_vr,
                "model": current_model,
            }

    final_rationale = (best["parsed"] or {}).get("rationale", "")
    if recovery_note:
        final_rationale = (recovery_note + " " + final_rationale).strip()

    return {
        "script": best["script"],
        "rationale": final_rationale,
        "variables_used": (best["parsed"] or {}).get("variables_used", []) or best["vr"].variables_used,
        "commands_used": (best["parsed"] or {}).get("commands_used", []) or best["vr"].commands_used,
        "validation": best["vr"].to_dict(),
        "model": best["model"],
        "repair_attempts": attempts,
        "cache_stats": usage,
    }


# ---------------------------------------------------------------------------
# Q&A


def _assemble_revise_user_turn(
    script: str,
    revision: str,
    lang: str,
    existing_vars: list[str],
    candidates: list[dict],
    examples: list[dict],
    manual_sections: list[dict],
) -> list[dict]:
    existing_block = ""
    if existing_vars:
        existing_block = (
            "## Variables already imported in the existing script\n\n"
            + "\n".join(f"- `{v}`" for v in existing_vars)
        )

    dynamic = "\n\n".join(
        filter(
            None,
            [
                existing_block,
                prompts.render_variable_candidates(candidates),
                prompts.render_retrieved_examples(examples),
                prompts.render_manual_sections(manual_sections),
            ],
        )
    )

    return [
        _cached_prefix_block(),
        {
            "type": "text",
            "text": (
                f"# User request\n\n**Mode:** revise\n\n**Language:** {lang}\n\n"
                f"**Existing script:**\n```microdata\n{script.strip()}\n```\n\n"
                f"**Revision request:** {revision}\n\n"
                f"{prompts.REVISION_INSTRUCTION}\n\n"
                f"{dynamic}\n\n"
                f"{prompts.GENERATE_OUTPUT_CONTRACT}"
            ),
        },
    ]


def revise_script(
    script: str,
    revision: str,
    lang: str = "no",
    max_repair: int = 1,
) -> dict:
    """Apply a natural-language revision to an existing microdata script.

    Returns the same envelope shape as generate_script(). The revised
    script is in the `script` field — always the full text, not a diff.
    """
    client = _client()

    existing_vars = validation._extract_variables(script)
    candidates = retrieval.search_variables(revision, lang=lang, k=15)
    examples = retrieval.search_examples(revision, lang=lang, k=3)
    manual_sections = retrieval.search_manual(revision, lang=lang, k=2)

    messages: list[dict] = [
        {
            "role": "user",
            "content": _assemble_revise_user_turn(
                script, revision, lang, existing_vars, candidates, examples, manual_sections
            ),
        }
    ]
    parsed, usage, raw = _run_tool_loop(
        client, DEFAULT_MODEL, prompts.SYSTEM_PROMPT, messages
    )

    recovery_note = ""
    if parsed is None:
        parsed = _recover_partial_json(raw)
        if parsed is not None:
            recovery_note = "Output was partially malformed; recovered via JSON extraction."
        else:
            salvaged = _extract_script_from_raw(raw)
            if salvaged:
                parsed = {
                    "script": salvaged,
                    "rationale": "Output was unstructured; salvaged code block as revised script.",
                    "variables_used": existing_vars,
                    "commands_used": [],
                }
                recovery_note = "Output had no JSON; salvaged a fenced code block."
            else:
                # Keep the original script — at least the user doesn't lose it.
                return {
                    "script": script,
                    "rationale": ("Modellen returnerte ikke et brukbart svar — beholder originalt skript. Rå-output:\n\n"
                                  + (raw or "(tomt)")[:1500]),
                    "variables_used": existing_vars,
                    "commands_used": [],
                    "validation": {
                        "passed": False,
                        "tier_ran": "static",
                        "errors": [{"kind": "parse", "message": "No revision could be recovered from model output."}],
                    },
                    "model": DEFAULT_MODEL,
                    "cache_stats": usage,
                    "revision_applied": False,
                }

    new_script = parsed.get("script", "") or ""
    vr = validation.validate_static(new_script)

    best = {
        "parsed": parsed,
        "script": new_script,
        "vr": vr,
        "model": DEFAULT_MODEL,
    }

    def _is_better(new_vr, old_vr) -> bool:
        if len(new_vr.errors) < len(old_vr.errors):
            return True
        if len(new_vr.errors) > len(old_vr.errors):
            return False
        soft_kinds = {"parse", "runtime"}
        new_hard = sum(1 for e in new_vr.errors if e.kind not in soft_kinds)
        old_hard = sum(1 for e in old_vr.errors if e.kind not in soft_kinds)
        return new_hard < old_hard

    attempts = 0
    current_model = DEFAULT_MODEL
    while not best["vr"].passed and attempts < max_repair:
        attempts += 1
        if attempts == 2:
            current_model = REPAIR_MODEL
        messages.append({"role": "assistant", "content": json.dumps(best["parsed"], ensure_ascii=False)})
        messages.append(
            {
                "role": "user",
                "content": (
                    prompts.REPAIR_INSTRUCTION
                    + "\n\nErrors:\n"
                    + json.dumps([e.__dict__ for e in best["vr"].errors], ensure_ascii=False)
                    + "\n\n"
                    + prompts.GENERATE_OUTPUT_CONTRACT
                ),
            }
        )
        new_parsed, usage, new_raw = _run_tool_loop(
            client, current_model, prompts.SYSTEM_PROMPT, messages
        )
        if new_parsed is None:
            new_parsed = _recover_partial_json(new_raw)
            if new_parsed is None:
                break
        new_script_attempt = new_parsed.get("script", "") or ""
        new_vr = validation.validate_static(new_script_attempt)
        if _is_better(new_vr, best["vr"]):
            best = {
                "parsed": new_parsed,
                "script": new_script_attempt,
                "vr": new_vr,
                "model": current_model,
            }

    final_rationale = (best["parsed"] or {}).get("rationale", "")
    if recovery_note:
        final_rationale = (recovery_note + " " + final_rationale).strip()

    return {
        "script": best["script"],
        "rationale": final_rationale,
        "variables_used": (best["parsed"] or {}).get("variables_used", []) or best["vr"].variables_used,
        "commands_used": (best["parsed"] or {}).get("commands_used", []) or best["vr"].commands_used,
        "validation": best["vr"].to_dict(),
        "model": best["model"],
        "repair_attempts": attempts,
        "cache_stats": usage,
        "revision_applied": True,
    }


@anvil.server.background_task
def bg_smart_query(question, lang="no", max_repair=2, deep_validate=False):
    """Background-task wrapper around smart_query.

    Used by /query for script_gen intent so that long generations are not
    cut off by Anvil's 30s HTTP execution cap. Returns the same envelope
    that smart_query returns.
    """
    return smart_query(
        question=question,
        lang=lang,
        max_repair=max_repair,
        deep_validate=deep_validate,
    )


def smart_query(
    question: str,
    lang: str = "no",
    max_repair: int = 1,
    deep_validate: bool = False,
) -> dict:
    """Classify intent with Haiku, then dispatch to the matching mode.

    Returns a discriminated-union envelope:
        {intent, lang, terms, classifier: {model, usage, fallback?}, result: ...}

    `result` shape depends on intent:
        script_gen      → full generate_script() output
        qa              → full answer_question() output
        variable_search → {"variables": [...]} (no LLM call)
    """
    cls = classifier.classify(question, default_lang=lang)
    intent = cls["intent"]
    resolved_lang = cls["lang"]

    envelope = {
        "intent": intent,
        "lang": resolved_lang,
        "terms": cls["terms"],
        "classifier": {
            "model": cls.get("model"),
            "usage": cls.get("usage") or {},
            "fallback": cls.get("fallback"),
        },
    }

    if intent == "script_gen":
        envelope["result"] = generate_script(
            question=question, lang=resolved_lang, max_repair=max_repair
        )
        if deep_validate and envelope["result"].get("script"):
            deep = validation.validate_dry_run(envelope["result"]["script"])
            envelope["result"]["validation"] = deep.to_dict()
    elif intent == "variable_search":
        hits = retrieval.server_variable_search(query=question, lang=resolved_lang, k=15)
        envelope["result"] = {"variables": hits}
    else:  # qa (default)
        envelope["result"] = answer_question(question=question, lang=resolved_lang)

    return envelope


def answer_question(question: str, lang: str = "no") -> dict:
    client = _client()
    manual_sections = retrieval.search_manual(question, lang=lang, k=3)
    examples = retrieval.search_examples(question, lang=lang, k=2)
    candidates = retrieval.search_variables(question, lang=lang, k=5)

    dynamic = "\n\n".join(
        filter(
            None,
            [
                prompts.render_manual_sections(manual_sections),
                prompts.render_retrieved_examples(examples),
                prompts.render_variable_candidates(candidates) if candidates else "",
            ],
        )
    )

    messages = [
        {
            "role": "user",
            "content": [
                _cached_prefix_block(),
                {
                    "type": "text",
                    "text": (
                        f"# User question\n\n**Language:** {lang}\n\n"
                        f"{question}\n\n{dynamic}\n\n{prompts.ASK_OUTPUT_CONTRACT}"
                    ),
                },
            ],
        }
    ]

    parsed, usage, raw = _run_tool_loop(
        client, DEFAULT_MODEL, prompts.SYSTEM_PROMPT, messages, max_tool_turns=3
    )
    if parsed is None:
        return {
            "answer": raw or "",
            "citations": [],
            "model": DEFAULT_MODEL,
            "cache_stats": usage,
        }
    return {
        "answer": parsed.get("answer", ""),
        "citations": parsed.get("citations", []),
        "model": DEFAULT_MODEL,
        "cache_stats": usage,
    }
