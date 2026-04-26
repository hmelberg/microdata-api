import anvil.email
import anvil.users
"""Tiered validator for generated microdata scripts.

Tier 1 (static, always on): parse each line with MicroParser, check variable
names against the in-memory set built from corpus.pkl, check command names
against the same corpus. Typical cost: 5-50 ms (no SQL round trips).

Tier 2 (repair, default on): on static failure, call the generation layer
once with a `fix` prompt. Caller decides via max_repair=0|1.

Tier 3 (dry_run, off by default): run the script through MockDataEngine +
StatsEngine on a tiny synthetic frame. Opt-in via deep_validate=true.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import anvil.server

import m2py_shim
import retrieval


_IMPORT_RE = re.compile(
    r"^\s*import\s+(?:\w+/)*([A-Z][A-Z0-9_]+)(?:\s+\d{4}-\d{2}-\d{2}(?:\s+to\s+\d{4}-\d{2}-\d{2})?)?(?:\s+as\s+(\S+))?",
    re.MULTILINE,
)

_VAR_STOPWORDS = {
    "NULL", "NONE", "NAN", "TRUE", "FALSE", "AND", "OR", "NOT",
    "MEAN", "MEDIAN", "SD", "MIN", "MAX", "COUNT", "SUM", "FIRST", "LAST",
}


@dataclass
class ValidationError:
    kind: str
    message: str
    line_no: Optional[int] = None
    token: Optional[str] = None


@dataclass
class ValidationResult:
    passed: bool
    tier_ran: str
    errors: list[ValidationError] = field(default_factory=list)
    variables_used: list[str] = field(default_factory=list)
    commands_used: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "tier_ran": self.tier_ran,
            "errors": [e.__dict__ for e in self.errors],
            "variables_used": self.variables_used,
            "commands_used": self.commands_used,
        }


# ---------------------------------------------------------------------------
# Extraction


def _extract_variables(script: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in _IMPORT_RE.finditer(script):
        name = m.group(1)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _extract_commands(script: str, parser) -> list[str]:
    try:
        pre = parser.preprocess_script(script)
    except Exception:
        pre = script
    seen: set[str] = set()
    out: list[str] = []
    for line in pre.splitlines():
        t = line.strip()
        if not t or t.startswith("//"):
            continue
        head = t.split(None, 1)[0]
        if head not in seen:
            seen.add(head)
            out.append(head)
    return out


# ---------------------------------------------------------------------------
# Static validation


def validate_static(script: str) -> ValidationResult:
    parser = m2py_shim.make_parser()
    errors: list[ValidationError] = []
    vars_used = _extract_variables(script)
    cmds_used = _extract_commands(script, parser)

    try:
        pre = parser.preprocess_script(script)
    except Exception as exc:
        errors.append(ValidationError(kind="parse", message=f"preprocess failed: {exc}"))
        return ValidationResult(
            passed=False, tier_ran="static", errors=errors,
            variables_used=vars_used, commands_used=cmds_used,
        )

    for i, line in enumerate(pre.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parser.parse_line(line)
        except Exception as exc:
            errors.append(ValidationError(kind="parse", message=str(exc), line_no=i))

    known_cmds = retrieval.known_commands()
    for cmd in cmds_used:
        if cmd not in known_cmds:
            errors.append(
                ValidationError(kind="unknown_command", message=f"Unknown command '{cmd}'.", token=cmd)
            )

    known_vars = retrieval.known_variables()
    for var in vars_used:
        if var in _VAR_STOPWORDS:
            continue
        if var not in known_vars:
            errors.append(
                ValidationError(kind="unknown_variable", message=f"Unknown variable '{var}'.", token=var)
            )

    return ValidationResult(
        passed=not errors,
        tier_ran="static",
        errors=errors,
        variables_used=vars_used,
        commands_used=cmds_used,
    )


# ---------------------------------------------------------------------------
# Dry-run validation


_DRY_RUN_DEFAULT_ROWS = 200


def validate_dry_run(script: str) -> ValidationResult:
    static = validate_static(script)
    if not static.passed:
        static.tier_ran = "dry_run"
        return static

    try:
        InterpreterCls = m2py_shim.get_interpreter_cls()
    except Exception as exc:
        # Infrastructure problem, not a script problem — don't count this
        # against the caller. Return the (passing) static result with a
        # note so the repair loop doesn't chase a non-error.
        static.tier_ran = "static (dry_run unavailable: " + str(exc)[:120] + ")"
        return static

    try:
        interp = InterpreterCls(echo_commands=False)
        # Shrink the synthetic frame: dry-run only needs enough rows to
        # exercise joins and group-bys, not realistic statistical power.
        if hasattr(interp, "data_engine"):
            try:
                interp.data_engine.default_rows = _DRY_RUN_DEFAULT_ROWS
            except Exception:
                pass
        interp.run_script(script)  # type: ignore[attr-defined]
    except Exception as exc:
        static.errors.append(ValidationError(kind="runtime", message=str(exc)))
        static.passed = False

    static.tier_ran = "dry_run"
    return static


# ---------------------------------------------------------------------------
# Callable surface


@anvil.server.callable
def server_validate(script: str, deep: bool = False) -> dict:
    result = validate_dry_run(script) if deep else validate_static(script)
    return result.to_dict()
