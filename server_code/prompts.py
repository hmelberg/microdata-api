"""Static prompt content + assembly helpers.

The cached prefix — system prompt + commands reference + grammar cheat
sheet + canonical examples — is built once per worker from the in-memory
corpus and re-used across requests with Anthropic's cache_control. Per-
request dynamic content (candidate variables, retrieved examples, user
question) is appended outside the cache boundary.
"""

from __future__ import annotations

import retrieval


SYSTEM_PROMPT = """\
You are an expert assistant for the microdata.no analysis system — a Stata-
like DSL used by Norwegian researchers to analyse microdata from Statistics
Norway (SSB). You handle questions in Norwegian and English.

Two modes:

1. **script_gen** — the user wants a runnable microdata.no script. Produce a
   complete script that (a) creates a dataset, (b) imports only variables
   that exist in the provided candidate list, (c) performs the requested
   analysis. Never invent variable names.

2. **qa** — the user wants an explanation. Answer concisely, in the user's
   language. Cite the manual section or command name you drew from.

**Variable selection (script_gen):** the user's turn includes a ranked
list of candidate variables already retrieved for you. Prefer these — the
ranking is reliable. Only call the `lookup_variable` tool if NONE of the
candidates fit the request, and even then make at most ONE call. Do not
explore alternatives once you have an adequate variable. Latency budget
is tight; tool sprawl will cause your response to be cut off.

You must respond with a JSON object matching the contract shown in the
user's turn. No extra prose outside the JSON.
"""


GRAMMAR_CHEATSHEET = """\
## microdata.no DSL — minimal grammar

- Comments start with `//`.
- Every script begins with `create-dataset <name>` (or `use <name>`), then
  one or more `import` statements bringing variables in from a databank.
- Import syntax:
    - Cross-section:  `import fd/VAR_NAME [YYYY-MM-DD] [as alias]`
    - Event:          `import fd/VAR_NAME YYYY-MM-DD to YYYY-MM-DD [as alias]`
- Variable transformations: `generate <name> = <expression>`,
  `replace <name> = <expr> [if <cond>]`, `recode ...`.
- Analysis: `summarize`, `tabulate`, `correlate`, `regress`, `logit`,
  `anova`, `ci`, `normaltest`, `transitions-panel`, `ivregress`.
- Reshape: `reshape long ...`, `reshape wide ...`.
- Aggregation: `collapse (stat) var -> new_name [, by(...)]`.
- Filter: `keep if <cond>`, `drop if <cond>`.
- Loops: `for i in (a b c) { ... } end` or `for-each x in a b c { body }`.

Import aliases are recommended (`import fd/INNTEKT_WLONN as inntekt`) — use
the alias for every downstream reference, but the raw UPPER_CASE name is
what is validated against the variable catalog.
"""


REPAIR_INSTRUCTION = """\
The previous script failed validation. Below are the structured errors.
Produce a corrected version of the same script that fixes each error.
Never invent variable names; call the `lookup_variable` tool if you need to
find a correct substitute.

Return the same JSON contract as before.
"""


REVISION_INSTRUCTION = """\
You are revising an existing microdata.no script. Apply the requested
revision while preserving unchanged structure — make the smallest set of
edits needed. Do not rename existing aliases or drop imports that are still
referenced. If the revision references new concepts, call the
`lookup_variable` tool to ground any unfamiliar variable names before
using them.

Return the **full revised script** (not a diff) using the same JSON
contract as a fresh generation.
"""


CANONICAL_EXAMPLE_IDS = [
    "examples/01_beskrivende_statistikk",
    "examples/04_aggregat_og_generate",
    "examples/05_regresjon",
    "web_examples/02_sammensatte_operasjoner/02_restrukturere_datasett_fra_wide_til_long_format",
    "web_examples/04_regresjonsanalyser/02_enkel_logistisk_regresjon",
]


def _compact_command_line(row: dict) -> str:
    name = row.get("name") or ""
    syntax = row.get("syntax") or name
    desc = (row.get("description") or "").strip().replace("\n", " ")
    if len(desc) > 180:
        desc = desc[:177] + "..."
    return f"- `{syntax}` — {desc}"


def build_commands_reference() -> str:
    corpus = retrieval.get_corpus()
    commands = sorted(
        corpus.get("commands") or [],
        key=lambda r: (r.get("category") or "", r.get("name") or ""),
    )
    lines = ["## Command reference (syntax — description)"]
    current_cat: str | None = None
    for r in commands:
        cat = r.get("category") or "Øvrige"
        if cat != current_cat:
            lines.append(f"\n### {cat}")
            current_cat = cat
        lines.append(_compact_command_line(r))
    return "\n".join(lines)


def build_canonical_examples() -> str:
    ex_by_id = retrieval.examples_by_ext_id()
    parts = ["## Canonical example scripts\n"]
    for ext_id in CANONICAL_EXAMPLE_IDS:
        row = ex_by_id.get(ext_id)
        if row is None:
            continue
        parts.append(f"### {row.get('title','')} ({row.get('topic','')})")
        parts.append("```microdata")
        parts.append((row.get("source_text") or "").strip())
        parts.append("```")
    return "\n".join(parts)


_cached_prefix: str | None = None


def cached_prefix() -> str:
    global _cached_prefix
    if _cached_prefix is None:
        _cached_prefix = "\n\n".join(
            [
                GRAMMAR_CHEATSHEET,
                build_commands_reference(),
                build_canonical_examples(),
            ]
        )
    return _cached_prefix


def refresh_cached_prefix() -> None:
    """Call after retrieval.reload_data_files() to invalidate the assembled prefix."""
    global _cached_prefix
    _cached_prefix = None


# ---------------------------------------------------------------------------
# Dynamic per-request content


def render_variable_candidates(rows: list[dict]) -> str:
    if not rows:
        return "## Candidate variables\n\n(none returned by retriever)"
    lines = ["## Candidate variables (validated against the catalog)", ""]
    for r in rows:
        lines.append(
            f"- `{r['name']}` ({r.get('data_type', '')}, {r.get('temporalitet', '')}"
            f", {r.get('enhetstype', '')}) — {r.get('short_title', '')}"
        )
    return "\n".join(lines)


def render_retrieved_examples(rows: list[dict], max_chars: int = 1500) -> str:
    if not rows:
        return ""
    parts = ["## Retrieved example scripts", ""]
    for r in rows:
        parts.append(f"### {r.get('title','')} ({r.get('topic','')})")
        text = (r.get("source_text") or "").strip()
        if len(text) > max_chars:
            text = text[:max_chars] + "\n// ... (truncated)"
        parts.append("```microdata")
        parts.append(text)
        parts.append("```")
    return "\n".join(parts)


def render_manual_sections(rows: list[dict], max_chars: int = 1200) -> str:
    if not rows:
        return ""
    parts = ["## Relevant manual sections", ""]
    for r in rows:
        parts.append(f"### {r.get('heading','')}")
        text = (r.get("source_text") or "").strip()
        if len(text) > max_chars:
            text = text[:max_chars] + "\n..."
        parts.append(text)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Output contracts


GENERATE_OUTPUT_CONTRACT = """\
Respond with a JSON object on a single line with these keys (no markdown
fencing, no extra prose):

{
  "script": "full microdata.no script as a string, using \\n for newlines",
  "rationale": "1-3 sentences explaining your choices, in the user's language",
  "variables_used": ["UPPERCASE_NAME", ...],
  "commands_used": ["command", ...]
}
"""

ASK_OUTPUT_CONTRACT = """\
Respond with a JSON object on a single line (no markdown, no extra prose):

{
  "answer": "your answer, in the user's language",
  "citations": [
    {"kind": "manual" | "command" | "example", "ref_key": "...", "url": "..."}
  ]
}
"""
