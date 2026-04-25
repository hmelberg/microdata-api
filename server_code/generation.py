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
JUDGE_MODEL = "claude-opus-4-7"

JUDGE_SYSTEM_TEMPLATE = """You are evaluating microdata.no script-generation quality.

You will see a user question (Norwegian or English), a generated microdata.no
DSL script, and optionally a reference script that solves the same task.

IMPORTANT — TRUST THE SYNTAX AND THE CATALOG. The script has already been
validated against microdata.no's grammar and against the live variable +
command catalog. Every command, function, and variable name in it has been
verified to exist on the real platform.

The COMPLETE list of valid microdata.no commands is:

{COMMAND_LIST}

If the script uses any name from that list, treat it as valid no matter
how unfamiliar it looks. The same applies to functions like `sysmiss(x)`,
import syntax like `require <db> as <alias>`, merge syntax like
`merge <vars> into <ds> on <key>`, interaction syntax like `i.var`, and
SSB-style variable names with year suffixes like `ARBLONN_2022`. None
of these are "invented" — they are platform-specific conventions that
differ from Stata, R, SQL or pandas.

Your job is to evaluate **semantic correctness** — does the script answer
the user's question correctly? Real reasons to lower the score:
- Wrong analytical method for the question
- Wrong aggregation level (e.g. event-level when person-level was asked)
- Wrong join direction
- Silently substituting different data than the user asked for (e.g.
  hardcoding 2023 when the user asked for 2015)
- Missing key steps the user explicitly requested (e.g. user asked for
  sorting by gap size and the script never sorts)
- Dead code / abandoned datasets that show the script is not coherent
- Off-topic output

Unfamiliar-looking but valid syntax is NOT a reason to lower the score.

Score the generated script 1-5:
- 5: Correctly and completely answers the question. Variables, syntax, and analytical approach are all sound. The user could run it and get exactly what they asked for.
- 4: Substantially correct. Minor issues (missing optional flag, slightly suboptimal variable choice, tabulate where collapse would have been cleaner) but the user gets a meaningful answer.
- 3: Partially correct. Right direction but missing key steps, wrong aggregation level, suboptimal variables, or a bug that produces misleading output.
- 2: Addresses the right topic but the approach is wrong (wrong analytical method, wrong join direction, mixes incompatible entity types, silently substituted different data than asked, or left dead code).
- 1: Doesn't answer the question at all (empty, off-topic, or fundamentally broken).

When a reference script is provided it is one valid solution — equivalent
approaches that answer the question correctly should still score 5. Do NOT
require character-for-character match.

When no reference is provided, score on the script's own merits.

Return STRICT JSON, no prose, no code fence:
{"score": <1-5>, "rationale": "<one or two sentences>"}"""


def _build_judge_system() -> str:
    return JUDGE_SYSTEM_TEMPLATE.replace("{COMMAND_LIST}", prompts.build_judge_command_list())


def _client() -> Anthropic:
    api_key = anvil.secrets.get_secret("ANTHROPIC_API_KEY")
    return Anthropic(api_key=api_key)


TOOLS = [
    {
        "name": "lookup_variable",
        "description": (
            "Drill into one or more microdata.no variables. Use ONLY to fetch "
            "details the cached catalog above does not carry — specifically: "
            "enum labels (code → meaning for categorical variables), codelist "
            "reference, available-years range, or the full long description. "
            "Do NOT use this to search for a name — every platform variable is "
            "already listed in the catalog above. The typical usage is passing "
            "the exact UPPERCASE name of a variable you already chose."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The exact UPPERCASE name of a variable (preferred) or a term/phrase."},
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
    # 1h TTL (vs the default 5 min) suits a bursty usage pattern: an active
    # session of 1-2 hours with multi-minute think-time gaps between queries.
    # Write costs 2× base instead of 1.25×, but cuts writes-per-session from
    # ~8 to 1, which is a net ~5× saving on the cached portion.
    return {
        "type": "text",
        "text": prompts.cached_prefix(),
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
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


def _validate(script: str, deep_validate: bool):
    """Static by default, dry-run (MockDataEngine) when opt-in.

    Putting this behind the repair loop means runtime errors from a mock
    execution (merge direction, entity-type mixing, missing date, etc.)
    become structured errors the model sees and can fix, not silent
    post-hoc warnings. Gated on `deep_validate` because the mock engine
    adds 1-5 s per attempt and callers on the sync /generate endpoint
    live under a 30 s cap.
    """
    if deep_validate:
        return validation.validate_dry_run(script)
    return validation.validate_static(script)


def _run_tool_loop(
    client: Anthropic,
    model: str,
    system: str,
    messages: list[dict],
    max_tool_turns: int = 15,
    max_tokens: int = 8192,
) -> tuple[dict | None, dict, str]:
    """Return (parsed_json, usage, raw_text).

    max_tokens=8192 covers complex scripts where the model reasons
    through a 5-factor regression or a merge-heavy pipeline (several
    hundred tokens of internal reasoning before emitting the final JSON
    wrapping a ~1500-char script + rationale). Prior 2048 cap was
    clipping the response mid-reason on exactly these cases, producing
    empty-script failures at 100% useful_output rate loss.
    """
    for _ in range(max_tool_turns):
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
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
    examples: list[dict],
    manual_sections: list[dict],
) -> list[dict]:
    # Variable candidates intentionally omitted: the full catalog is already
    # in the cached prefix, so a per-request top-k block is pure duplication
    # and adds prompt noise without adding information.
    dynamic = "\n\n".join(
        filter(
            None,
            [
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
    deep_validate: bool = False,
) -> dict:
    client = _client()

    # Retrieval. Variables now live in the cached prefix in full, so we only
    # retrieve examples and manual sections per request.
    cmd_keywords = []
    examples = retrieval.search_examples(question, lang=lang, k=3, boost_commands=cmd_keywords)
    manual_sections = retrieval.search_manual(question, lang=lang, k=2)

    messages: list[dict] = [
        {
            "role": "user",
            "content": _assemble_generate_user_turn(
                question, lang, examples, manual_sections
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
    vr = _validate(script, deep_validate)

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
        new_vr = _validate(new_script, deep_validate)
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
    examples: list[dict],
    manual_sections: list[dict],
) -> list[dict]:
    existing_block = ""
    if existing_vars:
        existing_block = (
            "## Variables already imported in the existing script\n\n"
            + "\n".join(f"- `{v}`" for v in existing_vars)
        )

    # Variable candidates intentionally omitted: full catalog is in the
    # cached prefix.
    dynamic = "\n\n".join(
        filter(
            None,
            [
                existing_block,
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
    deep_validate: bool = False,
) -> dict:
    """Apply a natural-language revision to an existing microdata script.

    Returns the same envelope shape as generate_script(). The revised
    script is in the `script` field — always the full text, not a diff.
    """
    client = _client()

    existing_vars = validation._extract_variables(script)
    examples = retrieval.search_examples(revision, lang=lang, k=3)
    manual_sections = retrieval.search_manual(revision, lang=lang, k=2)

    messages: list[dict] = [
        {
            "role": "user",
            "content": _assemble_revise_user_turn(
                script, revision, lang, existing_vars, examples, manual_sections
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
    vr = _validate(new_script, deep_validate)

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
        new_vr = _validate(new_script_attempt, deep_validate)
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
            question=question,
            lang=resolved_lang,
            max_repair=max_repair,
            deep_validate=deep_validate,
        )
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

    # Variable candidates intentionally omitted: full catalog is in the
    # cached prefix.
    dynamic = "\n\n".join(
        filter(
            None,
            [
                prompts.render_manual_sections(manual_sections),
                prompts.render_retrieved_examples(examples),
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


# ---------------------------------------------------------------------------
# LLM-judge (Opus 4.7) — used by the eval harness to score generated scripts.


def judge_script(
    question: str,
    generated_script: str,
    reference_script: str = "",
    lang: str = "no",
) -> dict:
    """Return {score: 1-5, rationale: str, model, cache_stats}.

    Server-side judge so the eval harness never needs ANTHROPIC_API_KEY
    locally. The judge prompt is cached with 1h TTL for cheap re-use across
    a full eval pass.
    """
    if not (generated_script or "").strip():
        return {
            "score": 1,
            "rationale": "Generated script is empty.",
            "model": JUDGE_MODEL,
            "cache_stats": {},
        }
    user_parts = [
        f"# Question ({lang})\n\n{question}",
        f"# Generated script\n\n```microdata\n{generated_script.strip()}\n```",
    ]
    if (reference_script or "").strip():
        user_parts.append(
            f"# Reference script (one valid solution; not the only one)\n\n"
            f"```microdata\n{reference_script.strip()}\n```"
        )

    client = _client()
    try:
        resp = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=400,
            system=[
                {
                    "type": "text",
                    "text": _build_judge_system(),
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
            ],
            messages=[{"role": "user", "content": "\n\n".join(user_parts)}],
        )
        text = ""
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text = block.text
                break
        usage = resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else dict(resp.usage)
    except Exception as exc:
        return {
            "score": 0,
            "rationale": f"Judge error: {type(exc).__name__}: {exc}",
            "model": JUDGE_MODEL,
            "cache_stats": {},
        }

    parsed = _parse_json_response(text) or _recover_partial_json(text)
    if not isinstance(parsed, dict):
        return {
            "score": 0,
            "rationale": f"Could not parse judge output: {(text or '')[:200]}",
            "model": JUDGE_MODEL,
            "cache_stats": usage,
        }
    try:
        score = int(parsed.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    return {
        "score": max(0, min(5, score)),
        "rationale": str(parsed.get("rationale", ""))[:500],
        "model": JUDGE_MODEL,
        "cache_stats": usage,
    }
