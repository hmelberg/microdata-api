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

**Variable selection (script_gen):** the cached context above contains
the FULL variable catalog (every variable in the platform). The user's
turn additionally surfaces a ranked subset that looks relevant to the
question — treat it as a hint, not a constraint. Any variable from the
full catalog is valid. Call the `lookup_variable` tool only when you
need details the catalog doesn't carry: enum labels for a categorical
variable (value → meaning), the codelist reference, available-years
range, or the full long description. Do not use it to search for a
name — the catalog above is exhaustive. Never invent variable names.

**WORKFLOW HYGIENE (script_gen) — these rules are non-negotiable. A
script that violates any of them is a failed response, regardless of how
sophisticated the analysis looks.**

1. **Use the exact year the user asked for.** The year goes in the
   import statement, not in the variable name:
   - ✅ `import db/INNTEKT_WLONN 2022-01-01 as innt22`
   - ❌ `import db/INNTEKT_WLONN_2022 as innt22`  (variable doesn't exist)
   - ❌ `import db/INNTEKT_WLONN 2023-01-01 as innt22`  (wrong year — user asked 2022)

   If the asked year is outside what a databank covers, do NOT silently
   use the latest available — pick the closest year and note the
   substitution in the `rationale` field.

2. **NO DEAD CODE, NO ABANDONED DATASETS.** If you decide partway
   through that an approach won't work, REWRITE the script — do NOT
   leave the first attempt's `create-dataset`, `import`, or `merge`
   lines behind. The final emitted script must be the single coherent
   path you'd actually run, not a journey of attempts. A second
   `generate <name> = ...` of the same name is a bug. A merge that
   references a dataset you never built is a bug.

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
- Reshape: `reshape long ...`, `reshape wide ...`,
  `reshape-to-panel <var-prefix> [<var-prefix> ...]` (turns wide imports like
  `ledig18 ledig19 ledig20` into a long panel; auto-creates a column
  literally named `panel@date` — note the `panel@` prefix, NOT `date@panel`).
  After reshape-to-panel you may use `tabulate-panel`, `regress-panel`,
  `transitions-panel`.
- Aggregation: `collapse (stat) var -> new_name [, by(...)]`.
- Filter: `keep if <cond>`, `drop if <cond>`.
- Loops: `for i in (a b c) { ... } end` or `for-each x in a b c { body }`.
- **Missing values**: use `sysmiss(x)` (returns 1 if x is system-missing,
  else 0) — NOT `x == .` (Stata syntax, not supported). Negate with
  `!sysmiss(x)`. Examples: `drop if sysmiss(income)`,
  `replace flag = 0 if sysmiss(flag)`.

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
| SSB FDB | `require no.ssb.fdb:52 as db` | `db` | All SSB register data (income, demographics, education, geography). The current version is **52**. Older versions (30, 40, etc.) still exist but are stale — always use the latest unless the user explicitly asks for a specific older version. SSB releases new versions periodically (53, 54, …). |
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
## NPR (Norsk Pasientregister) — gotchas

- **Do NOT import `AGGRSHOPPID` alongside `NPRID` in the same dataset.**
  They have different `enhetstype` (Behandlingsopphold vs Person) and
  mixing them triggers a `unit_id` error. AGGRSHOPPID is rarely needed —
  the dataset's implicit event identifier covers most analyses. If you
  do need AGGRSHOPPID, build a separate dataset for it.
- **In `collapse`, always pass `by(<person-alias>)` explicitly** — e.g.
  `collapse (count) icd1 -> n_dx, by(pid)`. Without an explicit `by()`,
  the grouping is not what you'd expect.
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
replace astma = 0 if sysmiss(astma)   // unmatched persons → no admission
```
"""


DATASET_STRUCTURE = """\
## Dataset structures and how to combine them

Different microdata.no datasets store **different units of observation**.
Knowing what a row represents is the key to choosing the right import,
collapse, and merge strategy.

**Person-level** (one row per person)
- Most SSB FDB variables (`BEFOLKNING_*`, `INNTEKT_*`, `NUDB_*`,
  `SIVSTANDFDT_*`).
- Implicit row id: `PERSONID_1` (encrypted PID, same across SSB registers).

**Multi-row-per-person** (one row per event / job / vehicle / course / …)
- Each such dataset has a **person-ref column** that points back to the
  person — see the table in the next section. Examples:
    - NPR (hospital admissions): one row per admission, person-ref = `NPRID`
    - A-ordningen (jobb): one row per employment relationship,
      person-ref = `ARBEIDSFORHOLD_PERSON`
    - Kjøretøyregisteret: one row per vehicle, person-ref =
      `KJORETOY_KJORETOYID_FNR`
    - NUDB-kurs: one row per course completion, person-ref = `NUDB_KURS_FNR`
- The same person can appear in many rows (or zero).

### Three import modes

1. **Cross-section** — `import db/VAR YYYY-MM-DD [as alias]`. One value
   per person at one date. Most common.
2. **Event / forløp** — `import-event db/VAR YYYY-MM-DD to YYYY-MM-DD
   [as alias]`. Use when you need the full history of how a status
   changed inside a window (e.g. all sivilstand changes 2018→2022).
   Each person gets multiple rows, one per change. Useful for
   detecting transitions (e.g. "did this person change marital status?").
   Cannot be mixed with cross-section imports in the same dataset.
3. **Panel** — `import-panel db/VAR1 db/VAR2 YYYY-MM-DD YYYY-MM-DD
   [YYYY-MM-DD ...]`. Long-format dataset with one row per
   (person, time-point). Use for repeated cross-sections you want to
   analyse together (e.g. wage in 2018, 2019, 2020). Must be the FIRST
   thing in an empty dataset.

### Combining multi-row-per-person data with person-level data

**Pattern A — collapse, then merge into person dataset.**
Use when you want a person-level summary of events. Build the event
dataset, collapse to person-level using the person-ref column, then
push into the person dataset:

```microdata
// Build NPR event dataset, collapse to person, merge into population
use npr_data
collapse (count) HOVEDTILSTAND1 -> n_admissions, by(NPRID)
merge n_admissions into personer on pid   // pid is alias for NPRID
use personer
replace n_admissions = 0 if sysmiss(n_admissions)  // unmatched = 0 events
```

**Pattern B — merge person attribute INTO event dataset (one-to-many).**
Use when you want to analyse events stratified by a person attribute
(e.g. "how many hospital admissions are male vs female"). The merge
goes the other direction: each person row gets duplicated across all
their events.

```microdata
// First build personer with the attribute you want to add
create-dataset personer
import db/BEFOLKNING_KJOENN as kjonn

// Then merge kjonn INTO the event dataset
use personer
merge kjonn into npr_data on pid
use npr_data
tabulate kjonn HOVEDTILSTAND1   // events grouped by sex
```

Choose A when the analysis unit is the person (e.g. regression of
income on number of admissions). Choose B when the analysis unit is
the event (e.g. tabulate admissions by sex).
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
        "## Common variables (high-frequency — start here for standard analyses)",
        "",
        "These are the variables most often used in existing microdata.no scripts.",
        "They're a subset of the full catalog below; use them as a starting point",
        "when the question matches a common demographic/economic pattern.",
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


_DATABANK_ALIAS_HINT = {
    "no.ssb.fdb": "db",
    "no.fhi.npr": "fnpr",
}


def build_full_catalog_block() -> str:
    """Render every variable in the catalog as a compact reference.

    Grouped by databank, alphabetical within each group. Format per row:
    `- NAME [data_type, temporalitet, enhetstype] — short_title`. At ~740
    variables the block is ~17k tokens; combined with the rest of the
    cached prefix it stays well under Sonnet's comfortable cache size.
    Having every name visible at all times eliminates retrieval-recall
    misses on the variable-name axis and lets the model pick correctly
    without spending tool turns on `lookup_variable`.
    """
    corpus = retrieval.get_corpus()
    variables = corpus.get("variables") or []
    if not variables:
        return ""
    by_bank: dict[str, list[dict]] = {}
    for v in variables:
        bank = (v.get("databank") or "(unknown)").strip() or "(unknown)"
        by_bank.setdefault(bank, []).append(v)
    # SSB FDB first (the bulk), then alphabetical.
    bank_order = sorted(
        by_bank.keys(),
        key=lambda b: (b != "no.ssb.fdb", b),
    )

    lines = [
        "## Full variable catalog",
        "",
        "Every variable available in the microdata.no platform is listed below,",
        "grouped by databank. The ranked candidates in each user turn are a",
        "convenience hint; pick from anywhere in this catalog. Row format:",
        "`NAME [data_type, temporalitet, enhetstype] — short_title`.",
        "",
        "Interpretation:",
        "- `temporalitet = Fast` → import without a date.",
        "- `temporalitet ∈ {Tverrsnitt, Akkumulert, Forløp}` → import with a date.",
        "- `temporalitet = Event` → import with a date range.",
        "- `enhetstype ≠ Person` → you must also import the corresponding",
        "  person-ref column (see the cross-dataset entity-linking table above).",
        "",
    ]
    for bank in bank_order:
        alias = _DATABANK_ALIAS_HINT.get(bank)
        header = f"### `{bank}`"
        if alias:
            header += f" — conventional alias `{alias}`"
        lines.append(header)
        lines.append("")
        rows = sorted(by_bank[bank], key=lambda v: (v.get("name") or "").upper())
        for v in rows:
            name = v.get("name") or ""
            dt = v.get("data_type") or ""
            temp = v.get("temporalitet") or ""
            ehtp = v.get("enhetstype") or ""
            title = (v.get("short_title") or "").strip()
            if len(title) > 110:
                title = title[:107] + "..."
            meta = f"[{dt}, {temp}, {ehtp}]"
            if title:
                lines.append(f"- `{name}` {meta} — {title}")
            else:
                lines.append(f"- `{name}` {meta}")
        lines.append("")
    return "\n".join(lines).rstrip()


REPAIR_INSTRUCTION = """\
The previous script failed validation. Below are the structured errors.
Produce a corrected version of the same script that fixes each error.

Error-kind guide:
- `kind="unknown_variable"` — the name is not in the catalog. Pick a
  different variable from the full catalog above. Never invent names.
- `kind="unknown_command"` — the command isn't supported. Pick one from
  the command reference above.
- `kind="parse"` — syntax error on the given line. Rewrite that line.
- `kind="runtime"` — came from executing the script on a mock dataset.
  Re-check the platform rules above (merge direction and `use <source>`
  ordering, entity-type mixing in one dataset, date-import conventions,
  person-ref columns for non-Person entities). The message usually names
  the failing construct.

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


def build_judge_command_list() -> str:
    """Return a comma-separated list of valid microdata.no command names.

    Used by the LLM-judge so Opus stops false-flagging real but unfamiliar
    commands (`reshape-to-panel`, `tabulate-panel`, `kaplan-meier`, etc.)
    as "invented". Just names — no descriptions — to keep the judge prompt
    small (cached on Opus is expensive at $1.50/M cache-read).
    """
    corpus = retrieval.get_corpus()
    names = sorted(
        {(r.get("name") or "").strip() for r in (corpus.get("commands") or []) if r.get("name")}
    )
    return ", ".join(f"`{n}`" for n in names)


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
                    DATASET_STRUCTURE,
                    build_entity_links_block(),
                    NPR_CANONICAL_IMPORTS,
                    MERGE_CHEATSHEET,
                    DATE_QUIRKS,
                    PRIVACY_RULES,
                    build_full_catalog_block(),
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
