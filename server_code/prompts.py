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
- Every script begins with `require <databank> as <alias>` (see Databank
  cheat sheet below), then `create-dataset <name>` (or `use <name>`), then
  one or more `import` statements bringing variables in from the databank.
- Import syntax:
    - Cross-section:  `import db/VAR_NAME [YYYY-MM-DD] [as alias]`
    - Event:          `import db/VAR_NAME YYYY-MM-DD to YYYY-MM-DD [as alias]`
  (Replace `db` with whichever alias you set in `require ... as <alias>`.)
- Variable transformations: `generate <name> = <expression>`,
  `replace <name> = <expr> [if <cond>]`, `recode ...`.
- Analysis: `summarize`, `tabulate`, `correlate`, `regress`, `logit`,
  `anova`, `ci`, `normaltest`, `transitions-panel`, `ivregress`.
- Reshape: `reshape long ...`, `reshape wide ...`.
- Aggregation: `collapse (stat) var -> new_name [, by(...)]`.
- Filter: `keep if <cond>`, `drop if <cond>`.
- Loops: `for i in (a b c) { ... } end` or `for-each x in a b c { body }`.

Import aliases are recommended (`import db/INNTEKT_WLONN as inntekt`) — use
the alias for every downstream reference, but the raw UPPER_CASE name is
what is validated against the variable catalog.
"""


DATABANK_CHEATSHEET = """\
## Databank setup

Every script needs ONE `require` line up top, before any imports. Use the
short alias you choose (`as <alias>`) as the prefix in subsequent imports.

| Databank | `require` line | Conventional alias | Used for |
|---|---|---|---|
| SSB FDB | `require no.ssb.fdb:N as db` | `db` | All SSB register data (income, demographics, education, geography). N is the version (typical: 30+). |
| FHI NPR (hospital registry) | `require no.fhi.npr:DRAFT as fnpr` | `fnpr` | Norwegian Patient Registry — hospital admissions. |

**Imports use the alias as prefix:** `import db/BEFOLKNING_KJOENN as kjonn`.

**Variable temporality** (from the catalog metadata) tells you whether to
add a date to the import:
- `Fast` (Fixed) — no date. `import db/BEFOLKNING_KJOENN as kjonn`
- `Tverrsnitt` / `Akkumulert` / `Forløp` (Time-Varying) — needs a date.
  `import db/INNTEKT_WLONN 2022-01-01 as innt22`
- `Event` — needs a date range. `import db/UTDANNING_FULLFOERT 2020-01-01 to 2023-12-31 as utd`

If you import a Time-Varying variable without a date, the script will fail.
"""


PRIVACY_RULES = """\
## Privacy guardrails (microdata.no enforces these)

- **Never use:** `list`, `browse`, `print`, `head`, `tail`, `show`. These
  would expose individual rows and the platform forbids them.
- `tabulate` automatically hides cells where the count would identify
  individuals (the platform suppresses output if more than ~50% of cells
  have count < 5). Prefer aggregation over enumeration.
- For continuous variables, use `summarize` (returns mean/sd/quantiles, not
  individual values).
"""


DATE_QUIRKS = """\
## Date format quirks

- Many SSB date variables are stored as **integers**, not ISO date strings:
    - `BEFOLKNING_FOEDSELS_AAR_MND` is `YYYYMM` (e.g. `198403` = March 1984)
    - Some others are `YYYYMMDD` (e.g. `20220115`)
- Extract the year with `gen year = int(date_var/10000)` (for YYYYMMDD)
  or `gen year = int(date_var/100)` (for YYYYMM).
- NPR (`fnpr/`) date variables (e.g. `INNDATO`) are integers — days since
  1970-01-01.
- Catalog metadata `data_type` tells you the format: `date:yyyymm`,
  `date:yyyymmdd`, or `int` (with description noting the convention).
"""


NPR_CANONICAL_IMPORTS = """\
## NPR (Norsk Pasientregister) canonical imports

NPR data is fundamentally event-level (one row per hospital admission), with
multiple events per person. The microdata.no platform knows this from the
source metadata, so:

- **Import `NPRID` (the person id) first**, then per-event attributes
  (diagnoses, dates, level). This is the common, working pattern.
- **Do NOT also import `AGGRSHOPPID`** in the same dataset. It has a
  different `enhetstype` (`Behandlingsopphold`) than `NPRID` (`Person`), and
  mixing them in one dataset triggers a `unit_id` error. AGGRSHOPPID is
  rarely needed; the dataset's implicit event identifier suffices for most
  analyses.
- For **collapse**, **ALWAYS specify `by(<person-alias>)` explicitly**, e.g.
  `collapse (max) astma -> astma, by(pid)` or
  `collapse (count) icd1 -> n_dx, by(pid)`. Without an explicit `by()`, the
  collapse may not group by person as you'd expect. Use the alias you set
  in `import fnpr/NPRID as <alias>` (typically `pid`).

Recommended imports and aliases:

```microdata
require no.fhi.npr:DRAFT as fnpr
create-dataset npr_data
import fnpr/NPRID as pid                  // person id (also the cross-registry link key to SSB)
import fnpr/HOVEDTILSTAND1 as icd1        // main ICD-10 diagnosis
import fnpr/HOVEDTILSTAND2 as icd2        // secondary diagnosis (optional)
import fnpr/INNDATO as in_date            // admission date (int: days since 1970-01-01)
import fnpr/UTDATO as out_date            // discharge date
import fnpr/INNTID as in_time             // admission time (optional)
import fnpr/UTTID as out_time             // discharge time (optional)
import fnpr/OMSORGSNIVA as level          // inpatient / outpatient / day-visit
import fnpr/NIVA as isf_level             // ISF funding level
// Skip AGGRSHOPPID unless you specifically need a unique event identifier;
// in that case put it in a separate dataset to avoid the unit_id mismatch.
```
"""


MERGE_CHEATSHEET = """\
## Merging datasets

The microdata.no platform supports ONE merge syntax:

    merge <var-list> into <target_dataset> [on <key_variable>]

It pushes variables FROM the active dataset INTO the named target. So before
calling `merge ... into Y`, you must `use X` to make the source dataset
active. Common pattern:

    use npr_astma                       // make source active
    merge astma into personer on pid    // push astma into personer

The `on <var>` clause names the join key. The variable must exist in BOTH
datasets (typically the person-id alias, e.g. `pid`). When you collapsed the
source by a NPR/FNR person-ref alias (e.g. `collapse ... by(pid)`), the
platform also auto-detects the link to PERSONID_1 in the target — but it is
clearer to always pass `on <alias>` explicitly.

**Common mistakes — DO NOT WRITE:**

- `merge astma from npr_astma on pid` — the `from` syntax does not exist
- `merge ... into personer` while `personer` is the active dataset — you'd
  be merging from itself; switch with `use <other_name>` first

**Canonical NPR→person merge pattern:**

```microdata
// ... build npr_astma with imports + collapse (max) astma -> astma, by(pid)
// ... build personer with create-dataset + several db imports

use npr_astma
merge astma into personer on pid
use personer
replace astma = 0 if astma == .   // unmatched persons → no admission
```
"""


def build_entity_links_block() -> str:
    """Render the auto-derived entity→person-ref mapping + conventions.

    Derived from m2py.py's _ENTITY_PERSON_REF_COL at seed time. Content:
    a table of entity-types that store multi-record-per-person data, and
    the column each uses to link back to the person.
    """
    corpus = retrieval.get_corpus()
    links = corpus.get("entity_links") or []
    if not links:
        return ""
    lines = [
        "## Cross-dataset entity linking",
        "",
        "Several microdata.no datasets hold events or items that belong to a person"
        " (one person has many rows). Each uses a specific column to link back to"
        " the person. The table below lists those person-ref columns.",
        "",
        "| Entity type | Person-ref column | Databank | Notes |",
        "|---|---|---|---|",
    ]
    for e in links:
        ent = e.get("entity", "")
        pref = e.get("person_ref", "")
        bank = e.get("databank", "")
        ehtp = e.get("enhetstype", "")
        title = e.get("short_title", "")
        lines.append(f"| `{ent}` | `{pref}` | `{bank}` | {ehtp} — {title} |")
    lines.extend([
        "",
        "**Three conventions to follow:**",
        "",
        "1. **Always include the person-ref column** when importing variables from"
        " one of these entity types. Without it you can't link events back to the"
        " person or join to other datasets.",
        "2. **To aggregate events → person-level**, group by the person-ref column:"
        " `collapse (count) <any_event_var>, by(<person_ref>)`. E.g. for NPR:"
        " `collapse (count) AGGRSHOPPID, by(NPRID)` gives number of hospital"
        " admissions per person.",
        "3. **The person-ref column is the cross-dataset join key.** `NPRID` on"
        " NPR data matches the implicit person id on SSB person-level variables"
        " (same encrypted PID across registries). Once you have both a person-ref"
        " column and a SSB person variable in the same dataset, rows line up.",
    ])
    return "\n".join(lines)


def build_top_variables_block() -> str:
    """Render the auto-derived top-N most-used variables list."""
    corpus = retrieval.get_corpus()
    rows = corpus.get("top_variables") or []
    if not rows:
        return ""
    lines = [
        "## Common variables (high-frequency — prefer these without lookup)",
        "",
        "These are the variables most often used in existing microdata.no scripts.",
        "Use them directly without calling the `lookup_variable` tool unless the",
        "user clearly asks for something not covered here.",
        "",
    ]
    for v in rows:
        temp = v.get("temporalitet", "")
        date_hint = ""
        if temp in ("Tverrsnitt", "Akkumulert", "Forløp"):
            date_hint = "  *(needs date)*"
        elif temp == "Event":
            date_hint = "  *(needs date range)*"
        lines.append(
            f"- `{v['name']}` — {v.get('short_title','')} "
            f"`[{v.get('data_type','')}, {temp}, {v.get('enhetstype','')}]`{date_hint}"
        )
    return "\n".join(lines)


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
            filter(
                None,
                [
                    GRAMMAR_CHEATSHEET,
                    DATABANK_CHEATSHEET,
                    build_entity_links_block(),
                    NPR_CANONICAL_IMPORTS,
                    MERGE_CHEATSHEET,
                    DATE_QUIRKS,
                    PRIVACY_RULES,
                    build_top_variables_block(),
                    build_commands_reference(),
                    build_canonical_examples(),
                ],
            )
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
