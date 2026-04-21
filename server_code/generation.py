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


def _run_tool_loop(
    client: Anthropic,
    model: str,
    system: str,
    messages: list[dict],
    max_tool_turns: int = 10,
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
    parsed, usage, _raw = _run_tool_loop(
        client, DEFAULT_MODEL, prompts.SYSTEM_PROMPT, messages
    )

    if parsed is None:
        return {
            "script": "",
            "rationale": "Model did not return valid JSON.",
            "variables_used": [],
            "commands_used": [],
            "validation": {"passed": False, "tier_ran": "static", "errors": [
                {"kind": "parse", "message": "Empty or non-JSON model response."}
            ]},
            "model": DEFAULT_MODEL,
            "cache_stats": usage,
        }

    script = parsed.get("script", "") or ""
    vr = validation.validate_static(script)

    # Repair loop.
    attempts = 0
    current_model = DEFAULT_MODEL
    while not vr.passed and attempts < max_repair:
        attempts += 1
        if attempts == 2:
            current_model = REPAIR_MODEL
        messages.append({"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)})
        messages.append(
            {
                "role": "user",
                "content": (
                    prompts.REPAIR_INSTRUCTION
                    + "\n\nErrors:\n"
                    + json.dumps([e.__dict__ for e in vr.errors], ensure_ascii=False)
                    + "\n\n"
                    + prompts.GENERATE_OUTPUT_CONTRACT
                ),
            }
        )
        parsed, usage, _raw = _run_tool_loop(
            client, current_model, prompts.SYSTEM_PROMPT, messages
        )
        if parsed is None:
            break
        script = parsed.get("script", "") or ""
        vr = validation.validate_static(script)

    return {
        "script": script,
        "rationale": (parsed or {}).get("rationale", ""),
        "variables_used": (parsed or {}).get("variables_used", []) or vr.variables_used,
        "commands_used": (parsed or {}).get("commands_used", []) or vr.commands_used,
        "validation": vr.to_dict(),
        "model": current_model,
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
    parsed, usage, _raw = _run_tool_loop(
        client, DEFAULT_MODEL, prompts.SYSTEM_PROMPT, messages
    )

    if parsed is None:
        return {
            "script": script,
            "rationale": "Model did not return valid JSON; revision not applied.",
            "variables_used": existing_vars,
            "commands_used": [],
            "validation": {
                "passed": False,
                "tier_ran": "static",
                "errors": [{"kind": "parse", "message": "Empty or non-JSON model response."}],
            },
            "model": DEFAULT_MODEL,
            "cache_stats": usage,
            "revision_applied": False,
        }

    new_script = parsed.get("script", "") or ""
    vr = validation.validate_static(new_script)

    attempts = 0
    current_model = DEFAULT_MODEL
    while not vr.passed and attempts < max_repair:
        attempts += 1
        if attempts == 2:
            current_model = REPAIR_MODEL
        messages.append({"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)})
        messages.append(
            {
                "role": "user",
                "content": (
                    prompts.REPAIR_INSTRUCTION
                    + "\n\nErrors:\n"
                    + json.dumps([e.__dict__ for e in vr.errors], ensure_ascii=False)
                    + "\n\n"
                    + prompts.GENERATE_OUTPUT_CONTRACT
                ),
            }
        )
        parsed, usage, _raw = _run_tool_loop(
            client, current_model, prompts.SYSTEM_PROMPT, messages
        )
        if parsed is None:
            break
        new_script = parsed.get("script", "") or ""
        vr = validation.validate_static(new_script)

    return {
        "script": new_script,
        "rationale": (parsed or {}).get("rationale", ""),
        "variables_used": (parsed or {}).get("variables_used", []) or vr.variables_used,
        "commands_used": (parsed or {}).get("commands_used", []) or vr.commands_used,
        "validation": vr.to_dict(),
        "model": current_model,
        "repair_attempts": attempts,
        "cache_stats": usage,
        "revision_applied": True,
    }


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
