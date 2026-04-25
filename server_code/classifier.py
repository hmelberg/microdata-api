"""One-shot intent classifier for the unified /query endpoint.

Uses Claude Haiku 4.5 — small, fast, cheap. The classifier prompt is tiny
and fully cacheable, so the per-call cost is ~tens of input tokens in the
hot path. On a classification failure we fall back to `qa` in the
supplied default language so the router never hard-errors.
"""

from __future__ import annotations

import json

import anvil.secrets
from anthropic import Anthropic


CLASSIFIER_MODEL = "claude-haiku-4-5"


SYSTEM_PROMPT = """\
You are a router for a microdata.no research assistant. For each user
question, decide what kind of response is needed.

Three intents:

- `script_gen` — the user wants a runnable microdata.no script to actually
  perform an analysis. Examples:
    • "analyze the factors that affect crime"
    • "regress income on age and gender"
    • "show descriptive statistics for women, by region"
    • "lag en tabell over sysselsetting per kommune"

- `qa` — the user wants an explanation, definition, how-to, or concept
  clarification. Examples:
    • "what does reshape long do?"
    • "how do I import panel data?"
    • "what does temporalitet mean?"
    • "hva betyr enhetstype?"

- `variable_search` — the user wants to find or list variables matching
  some topic. Examples:
    • "which variables relate to education?"
    • "list income variables"
    • "finnes det en variabel for uførhet?"

Also detect the user's language (`no` or `en`) and extract the main
topical terms (2-6 short keywords) that would be useful for retrieval.

Respond with a JSON object on a single line. No markdown fencing, no
extra prose:

{"intent": "script_gen" | "qa" | "variable_search", "lang": "no" | "en", "terms": ["...", "..."]}
"""


_VALID_INTENTS = {"script_gen", "qa", "variable_search"}
_VALID_LANGS = {"no", "en"}


def _client() -> Anthropic:
    return Anthropic(api_key=anvil.secrets.get_secret("ANTHROPIC_API_KEY"))


def _parse(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        nl = text.find("\n")
        if nl > 0:
            text = text[nl + 1 :]
        if text.endswith("```"):
            text = text[:-3]
    try:
        return json.loads(text)
    except Exception:
        return None


def classify(question: str, default_lang: str = "no") -> dict:
    """Return {'intent', 'lang', 'terms', 'model', 'usage'}.

    `intent` is always one of script_gen | qa | variable_search.
    On any parse or network failure we default to qa / default_lang.
    """
    try:
        client = _client()
        resp = client.messages.create(
            model=CLASSIFIER_MODEL,
            max_tokens=200,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
            ],
            messages=[{"role": "user", "content": question}],
        )
    except Exception:
        return {
            "intent": "qa",
            "lang": default_lang,
            "terms": [],
            "model": CLASSIFIER_MODEL,
            "usage": {},
            "fallback": "network_error",
        }

    text = ""
    for block in resp.content:
        if getattr(block, "type", "") == "text":
            text = block.text
            break

    parsed = _parse(text) or {}
    intent = parsed.get("intent") if parsed.get("intent") in _VALID_INTENTS else "qa"
    lang = parsed.get("lang") if parsed.get("lang") in _VALID_LANGS else default_lang
    terms = parsed.get("terms") or []
    if not isinstance(terms, list):
        terms = []

    usage = resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else dict(resp.usage)
    return {
        "intent": intent,
        "lang": lang,
        "terms": [str(t) for t in terms],
        "model": CLASSIFIER_MODEL,
        "usage": usage,
    }
