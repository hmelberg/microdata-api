# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the safepy repo. This file is produced by sync_to_api.py.
# Edit the engine in the safepy repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""The one entry point: ``run(code, sources, level) -> SafeResult``.

This is the synchronous core of what the safestat spec calls ``/run_extended``.
The submit-then-poll wrapper (background task + ``task_id``) is deliberately not
here yet; it wraps this function without changing it. The ``on_result`` hook
exists precisely for that wrapper: it streams each released statement while the
run is still executing.

Pipeline:  policy -> gate -> sandbox -> mediate.  Each stage can only ever
*reduce* what is releasable; there is no path around the mediator.
"""

from __future__ import annotations

import ast
from typing import Any

import numpy as np
import pandas as pd

from .ast_gate import validate
from .errors import DisclosureError, SafePythonError, SandboxError, ValidationError
from .mediator import mediate
from .policy import Policy, Profile, ProtectionLevel, resolve_policy
from .result import SafeResult
from .runtime import execute
from .safe import SafeVerbs
from .safeframe import SafeFrame


def _build_namespace(profile: Profile, policy: Policy, sources: dict[str, Any],
                     dialect: str = "pandas") -> dict:
    """The single difference between the two security postures.

    OPEN   — real pandas/numpy + the raw frames are in scope.
    STRICT — only the safe-verb library, facade-wrapped sources, and the
             look-alike `pd`/`np` facades; no real pandas, no raw frame, so
             disclosive capabilities are simply not reachable.

    ``dialect`` selects the STRICT surface: ``pandas`` wraps sources in
    ``SafeFrame``; ``polars`` wraps them in ``SafePolarsFrame`` (polars surface,
    pandas suppression backend — see polars_api).
    """
    if dialect == "he":
        # Encrypted sources (Plane B): the facade is the entire surface, in every
        # profile — the raw EncryptedSource (which carries the key) is never
        # exposed. Lazy import: the he module needs the optional 'phe' package.
        from .he import build_he_namespace
        return build_he_namespace(sources, policy)
    if dialect == "polars-he":
        # Polars idiom over encrypted data: a facade routing to the same
        # HEAuthority (polars cannot run on ciphertext). See polars_he.
        from .polars_he import build_he_polars_namespace
        return build_he_polars_namespace(sources, policy)
    verbs = SafeVerbs(policy)
    if profile is Profile.STRICT and dialect == "polars":
        import polars as pl
        from .polars_api import SafePolarsFrame
        # sources may arrive as pandas (the server's load_dataframe always returns
        # pandas) — convert to polars so the polars surface works regardless of
        # how the frame was materialized. Already-polars frames pass through.
        def _to_pl(df):
            return pl.from_pandas(df) if isinstance(df, pd.DataFrame) else df
        return {"safe": verbs,
                **{name: SafePolarsFrame(_to_pl(df), verbs) for name, df in sources.items()}}
    if profile is Profile.STRICT:
        from .namespaces import SafeNp, SafePd
        from .formula_api import SafeStats
        return {"safe": verbs, "pd": SafePd(), "np": SafeNp(), "smf": SafeStats(verbs),
                **{name: SafeFrame(df, verbs) for name, df in sources.items()}}
    return {"pd": pd, "np": np, "safe": verbs, **sources}


def detect_python_dialect(code: str) -> str:
    """Which Python dataframe library the script uses: ``"polars"`` if it imports
    polars, else ``"pandas"``. AST-based (an ``import polars`` inside a string or
    comment does not count), and fail-safe — a script that is not valid Python
    resolves to ``"pandas"`` and the gate reports the real syntax error later."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return "pandas"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == "polars" for a in node.names):
                return "polars"
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "polars":
                return "polars"
    return "pandas"


def _sources_are_encrypted(sources: dict) -> bool:
    from .he import EncryptedSource
    return any(isinstance(v, EncryptedSource) for v in sources.values())


def run(code: str,
        sources: dict[str, Any],
        level: ProtectionLevel | str = ProtectionLevel.PROTECTED,
        *, profile: Profile | str | None = None,
        suppression=None,
        dialect: str = "pandas",
        render: str = "spec",
        on_result=None) -> SafeResult:
    """Validate, run, and disclosure-check ``code`` against ``sources``.

    ``sources`` maps the names user code may reference (e.g. ``{"df": frame}``)
    to private data objects. ``level`` selects the protection policy; ``profile``
    overrides the executor (OPEN sandbox vs STRICT capability) for that policy.
    ``suppression`` overrides the secondary-control tier — a preset name
    (``"light"``/``"standard"``/``"microdata"``) or a ``Suppression`` instance.
    ``render`` picks the transport encoding for chart results:
    ``spec`` (default, JSON) | ``plotly`` | ``png`` | ``html`` | ``ascii``.

    ``on_result`` (optional): called with each released ``SafeResult`` as its
    statement finishes — mediation is interleaved with execution, so a caller
    can stream partial results (the submit-then-poll wrapper uses this).
    Refused statements are not reported; callback exceptions are swallowed.
    Only the pandas/polars gate path is multi-statement; the r/duckdb/he
    paths produce a single result and ignore the hook.
    """
    policy: Policy = resolve_policy([level], suppression=suppression)
    active = Profile(profile) if profile is not None else policy.profile
    catalog = None  # datasets left in the session (populated once execution runs)
    outcomes = []   # per bare expression: ("ok", SafeResult) | ("refused", DisclosureError)

    if dialect == "python":
        # meta-dialect: the library is chosen by the code itself (a polars import),
        # and encrypted sources route to the homomorphic variant. See the design
        # spec 2026-07-04-python-meta-dialect-design.md.
        base = detect_python_dialect(code)                  # "pandas" | "polars"
        if _sources_are_encrypted(sources):
            dialect = "he" if base == "pandas" else "polars-he"
        else:
            dialect = base

    if dialect == "r":
        # R is parsed & translated (never executed) to the shared release core,
        # so it bypasses the Python gate/runtime entirely. See r_api.
        return _run_r(code, sources, policy, active, render)

    if dialect == "duckdb":
        # SQL is AST-gated, executed in a locked engine (no external access), and
        # released through the shared suppressor. See duckdb_api.
        return _run_duckdb(code, sources, policy, active)

    if dialect == "r-he":
        # R over encrypted data: the HE-computable R subset, translated (never
        # executed) and routed to an HEAuthority via the ReleaseBackend. See r_he.
        return _run_r_he(code, sources, policy, active)

    if dialect == "duckdb-he":
        # SQL over encrypted data: duckdb parses (never executes); the GROUP BY +
        # aggregate intent maps to an HEAuthority via the ReleaseBackend. See duckdb_he.
        return _run_duckdb_he(code, sources, policy, active)

    if dialect == "sqlite":
        # A deliberately narrower SQL menu than "duckdb" (SELECT/FROM/WHERE/
        # GROUP BY only, avg/sum/count) — sqlite3 has no AST-introspection
        # facility to build a generic gate on, so this uses a hand-written
        # narrow-grammar parser backed by SQLite's own set_authorizer() as an
        # independent second gate. See sqlite_api and docs/superpowers/specs/
        # 2026-07-08-sqlite-dialect-design.md.
        return _run_sqlite(code, sources, policy, active)

    if dialect == "sqlite-he":
        # SQL over encrypted data on the same narrow grammar; sqlite parses
        # nothing here either (parse-only via sqlite_grammar), mapped to an
        # HEAuthority. See sqlite_he.
        return _run_sqlite_he(code, sources, policy, active)

    try:
        namespace = _build_namespace(active, policy, sources, dialect)
        allowed_names = frozenset(namespace)
        # Whitelisted imports (resolving to safe facades) are allowed only in the
        # STRICT capability profile.
        imports_ok = active is Profile.STRICT
        gate = validate(code, allowed_names=allowed_names, allow_imports=imports_ok)
        if not gate.ok:
            assert gate.error is not None
            return SafeResult(ok=False, kind="error", error=gate.error.as_dict())

        # Each top-level bare expression is a potential result. Releasable ones are
        # collected; the last expression is the "primary" (top-level fields), which
        # may be a refusal (backward compatible). Non-releasable intermediates
        # (e.g. cph.fit()) are skipped. Mediation is interleaved with execution
        # (via on_expr) so released results can be streamed while later
        # statements are still running.
        def _stamp(res):
            res.audit.setdefault("level", policy.level.value)
            res.audit.setdefault("profile", active.value)
            res.audit.setdefault("verbs_used", gate.calls)
            if res.kind == "chart" and render != "spec":
                from .charts import render_chart
                res.payload = render_chart(res.payload, render)
                res.audit["render"] = render
            return res

        def _mediate_expr(value):
            try:
                res = _stamp(mediate(value, policy))
            except DisclosureError as exc:
                outcomes.append(("refused", exc))
                return
            outcomes.append(("ok", res))
            if on_result is not None:
                try:
                    on_result(res)
                except Exception:  # noqa: BLE001 - progress is best-effort
                    pass           # a bad callback must never kill the run

        expr_values, ns = execute(code, namespace, allow_imports=imports_ok,
                                  on_expr=_mediate_expr)
        catalog = _build_catalog(ns, policy)

        results = [res for tag, res in outcomes if tag == "ok"]
        primary = None
        if outcomes:
            tag, last = outcomes[-1]
            primary = last if tag == "ok" else SafeResult(
                ok=False, kind="error",
                error={"kind": type(last).__name__, "message": str(last)})
        if primary is None:  # no bare expressions (datasets-only) -> catalog only
            primary = SafeResult(ok=True, kind="none")
        primary.results = results
        primary.catalog = catalog
        return primary

    except ValidationError as exc:
        return SafeResult(ok=False, kind="error", error=exc.as_dict(), catalog=catalog,
                          results=[r for tag, r in outcomes if tag == "ok"])
    except (DisclosureError, SandboxError) as exc:
        return SafeResult(ok=False, kind="error", catalog=catalog,
                          results=[r for tag, r in outcomes if tag == "ok"],
                          error={"kind": type(exc).__name__, "message": str(exc)})
    except SafePythonError as exc:  # pragma: no cover - catch-all, still no data leak
        return SafeResult(ok=False, kind="error", catalog=catalog,
                          results=[r for tag, r in outcomes if tag == "ok"],
                          error={"kind": "SafePythonError", "message": str(exc)})


def _run_r(code: str, sources: dict, policy: Policy, active: Profile,
           render: str = "spec") -> SafeResult:
    """Translate a restricted R pipeline to the shared release core and mediate."""
    from .r_api import translate_r
    verbs = SafeVerbs(policy)
    try:
        released = translate_r(code, verbs, sources)
        res = mediate(released, policy)
        res.audit.setdefault("level", policy.level.value)
        res.audit.setdefault("profile", active.value)
        res.audit.setdefault("dialect", "r")
        if res.kind == "chart" and render != "spec":
            from .charts import render_chart
            res.payload = render_chart(res.payload, render)
            res.audit["render"] = render
        res.results = [res]
        return res
    except ValidationError as exc:
        return SafeResult(ok=False, kind="error", error=exc.as_dict())
    except (DisclosureError, SandboxError) as exc:
        return SafeResult(ok=False, kind="error",
                          error={"kind": type(exc).__name__, "message": str(exc)})
    except BaseException as exc:  # noqa: BLE001 - sanitise: never leak a data value
        return SafeResult(ok=False, kind="error", error={
            "kind": "SandboxError",
            "message": f"your R code raised {type(exc).__name__} during translation"})


def _run_r_he(code: str, sources: dict, policy: Policy, active: Profile) -> SafeResult:
    """Translate an R script over encrypted sources and release through the
    shared core. Mirrors _run_r, but the backend is an HEAuthority (see r_he)."""
    from .r_he import translate_r_he
    try:
        released = translate_r_he(code, policy, sources)
        res = mediate(released, policy)
        res.audit.setdefault("level", policy.level.value)
        res.audit.setdefault("profile", active.value)
        res.audit.setdefault("dialect", "r-he")
        res.results = [res]
        return res
    except ValidationError as exc:
        return SafeResult(ok=False, kind="error", error=exc.as_dict())
    except (DisclosureError, SandboxError) as exc:
        return SafeResult(ok=False, kind="error",
                          error={"kind": type(exc).__name__, "message": str(exc)})
    except BaseException as exc:  # noqa: BLE001 - sanitise: never leak a data value
        return SafeResult(ok=False, kind="error", error={
            "kind": "SandboxError",
            "message": f"your R code raised {type(exc).__name__} during translation"})


def _run_duckdb_he(code: str, sources: dict, policy: Policy, active: Profile) -> SafeResult:
    """Parse SQL over encrypted sources (never executed) and release through the
    shared core. Mirrors _run_duckdb, but the backend is an HEAuthority (see
    duckdb_he)."""
    from .duckdb_he import translate_sql_he
    try:
        released = translate_sql_he(code, policy, sources)
        res = mediate(released, policy)
        res.audit.setdefault("level", policy.level.value)
        res.audit.setdefault("profile", active.value)
        res.audit.setdefault("dialect", "duckdb-he")
        res.results = [res]
        return res
    except ValidationError as exc:
        return SafeResult(ok=False, kind="error", error=exc.as_dict())
    except (DisclosureError, SandboxError) as exc:
        return SafeResult(ok=False, kind="error",
                          error={"kind": type(exc).__name__, "message": str(exc)})
    except BaseException as exc:  # noqa: BLE001 - sanitise: never leak a data value
        return SafeResult(ok=False, kind="error", error={
            "kind": "SandboxError",
            "message": f"your SQL raised {type(exc).__name__} during translation"})


def _run_duckdb(code: str, sources: dict, policy: Policy, active: Profile) -> SafeResult:
    """Gate, execute (locked duckdb), and release SQL through the shared core."""
    from .duckdb_api import run_sql
    verbs = SafeVerbs(policy)
    try:
        released = run_sql(code, verbs, sources)
        res = mediate(released, policy)
        res.audit.setdefault("level", policy.level.value)
        res.audit.setdefault("profile", active.value)
        res.audit.setdefault("dialect", "duckdb")
        res.catalog = _raw_catalog(sources, policy)
        res.results = [res]
        return res
    except ValidationError as exc:
        return SafeResult(ok=False, kind="error", error=exc.as_dict())
    except (DisclosureError, SandboxError) as exc:
        return SafeResult(ok=False, kind="error",
                          error={"kind": type(exc).__name__, "message": str(exc)})
    except BaseException as exc:  # noqa: BLE001 - sanitise: a duckdb error message
        # may quote a data value, so only the exception type is reported.
        return SafeResult(ok=False, kind="error", error={
            "kind": "SandboxError",
            "message": f"your SQL raised {type(exc).__name__} during execution"})


def _run_sqlite_he(code: str, sources: dict, policy: Policy, active: Profile) -> SafeResult:
    """Parse SQL (narrow grammar, never executed) over encrypted sources and
    release through the shared core. Mirrors _run_sqlite, but the backend is
    an HEAuthority (see sqlite_he)."""
    from .sqlite_he import translate_sql_he
    try:
        released = translate_sql_he(code, policy, sources)
        res = mediate(released, policy)
        res.audit.setdefault("level", policy.level.value)
        res.audit.setdefault("profile", active.value)
        res.audit.setdefault("dialect", "sqlite-he")
        res.results = [res]
        return res
    except ValidationError as exc:
        return SafeResult(ok=False, kind="error", error=exc.as_dict())
    except (DisclosureError, SandboxError) as exc:
        return SafeResult(ok=False, kind="error",
                          error={"kind": type(exc).__name__, "message": str(exc)})
    except BaseException as exc:  # noqa: BLE001 - sanitise: never leak a data value
        return SafeResult(ok=False, kind="error", error={
            "kind": "SandboxError",
            "message": f"your SQL raised {type(exc).__name__} during translation"})


def _run_sqlite(code: str, sources: dict, policy: Policy, active: Profile) -> SafeResult:
    """Gate (narrow grammar + set_authorizer), execute (locked sqlite3), and
    release SQL through the shared core."""
    from .sqlite_api import run_sql
    verbs = SafeVerbs(policy)
    try:
        released = run_sql(code, verbs, sources)
        res = mediate(released, policy)
        res.audit.setdefault("level", policy.level.value)
        res.audit.setdefault("profile", active.value)
        res.audit.setdefault("dialect", "sqlite")
        res.catalog = _raw_catalog(sources, policy)
        res.results = [res]
        return res
    except ValidationError as exc:
        return SafeResult(ok=False, kind="error", error=exc.as_dict())
    except (DisclosureError, SandboxError) as exc:
        return SafeResult(ok=False, kind="error",
                          error={"kind": type(exc).__name__, "message": str(exc)})
    except BaseException as exc:  # noqa: BLE001 - sanitise: an error message
        # may quote a data value, so only the exception type is reported.
        return SafeResult(ok=False, kind="error", error={
            "kind": "SandboxError",
            "message": f"your SQL raised {type(exc).__name__} during execution"})


def _raw_catalog(sources: dict, policy: Policy) -> list:
    """Schema-only catalog of raw source frames (for the duckdb dialect), with the
    same suppressed row/missing counts as _build_catalog."""
    k, rt = policy.min_n, policy.round_to

    def count(n: int):
        n = int(n)
        if n == 0:
            return 0
        if n < k:
            return None
        return int(round(n / rt) * rt) if rt else n

    cat = []
    for name, df in sources.items():
        d = df.to_pandas() if hasattr(df, "to_pandas") else df
        cols = [{"name": str(c), "dtype": str(d[c].dtype),
                 "n_missing": count(d[c].isna().sum())} for c in d.columns]
        cat.append({"name": name, "n_rows": count(len(d)),
                    "n_columns": len(d.columns), "columns": cols})
    return cat


def _build_catalog(ns: dict, policy: Policy) -> list:
    """A schema-only catalog of every SafeFrame bound in the session: names,
    columns, dtypes, and suppressed counts (n_rows / n_missing). Never values."""
    from .safeframe import SafeFrame

    k, rt = policy.min_n, policy.round_to

    def count(n: int):
        n = int(n)
        if n == 0:
            return 0                      # "no missing" is not disclosive
        if n < k:
            return None                   # a small nonzero count is suppressed
        return int(round(n / rt) * rt) if rt else n

    catalog = []
    for name, val in ns.items():
        if name.startswith("_"):
            continue
        if isinstance(val, SafeFrame):
            d = val._df
            cols = [(str(c), str(d[c].dtype), int(d[c].isna().sum())) for c in d.columns]
            n_rows = len(d)
        elif getattr(val, "_is_polars_safeframe", False):
            # polars source (eager or lazy): the facade introspects its own frame,
            # so api.py stays decoupled from polars specifics.
            n_rows, cols = val._catalog_raw()
        else:
            continue
        columns = [{"name": c, "dtype": dt, "n_missing": count(nm)} for c, dt, nm in cols]
        catalog.append({"name": name, "n_rows": count(n_rows),
                        "n_columns": len(columns), "columns": columns})
    return catalog
