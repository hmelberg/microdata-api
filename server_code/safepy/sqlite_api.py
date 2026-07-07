# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the safepy repo. This file is produced by sync_to_api.py.
# Edit the engine in the safepy repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""The SQLite dialect — STRICT: gated execution over the shared release core.

See docs/superpowers/specs/2026-07-08-sqlite-dialect-design.md for the full
design and why this is NOT a port of duckdb_api.py (sqlite3 has no
AST-introspection facility to build a generic gate on, so this uses a
deliberately narrow hand-written grammar instead — see sqlite_grammar.py —
backed by SQLite's own Connection.set_authorizer() as a second, independent,
compiler-level gate).

Three layers, same shape as duckdb_api.py's:
1. Parse (sqlite_grammar.parse_query) into table/select/where/group_by —
   anything outside the narrow grammar fails to parse.
2. Lock down execution: winsorize at registration (Tiltak 2), and
   Connection.set_authorizer() denies everything except SELECT, reading a
   registered table's real columns, and whitelisted functions.
3. Release through SafeVerbs._release_group_agg — the same suppressor
   pandas/polars/duckdb/HE all share.

The query actually executed is reconstructed from the parsed, validated
pieces (never the user's literal text) with a paired COUNT(col) — or
COUNT(*) pairing with itself — appended to the same SELECT list, so value
and count come back aligned from one query, one execution.
"""
from __future__ import annotations

import sqlite3

import pandas as pd

from .errors import DisclosureError, ValidationError
from .safe import SafeVerbs, _winsorize_col
from .sqlite_grammar import SQL_AGGS, parse_query

_ALLOWED_READ_ONLY_ACTIONS = frozenset({
    sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
})


def _quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _make_authorizer(table: str, columns: frozenset, allowed_funcs: frozenset):
    def authorizer(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            if arg1 == table and arg2 in columns:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            if (arg2 or "").lower() in allowed_funcs:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY
    return authorizer


def _connect_one_table(name: str, df: pd.DataFrame, policy) -> sqlite3.Connection:
    pdf = df.to_pandas() if hasattr(df, "to_pandas") else pd.DataFrame(df)
    if policy.suppression.winsorize is not None:
        for c in pdf.columns:
            pdf = _winsorize_col(pdf, c, policy)
    conn = sqlite3.connect(":memory:")
    # Defence in depth; off by default anyway. Some sqlite3 builds (Pyodide's
    # among them, confirmed 2026-07-08) compile out extension-loading
    # entirely, so the method itself doesn't exist — which is strictly safer
    # than having it present-but-disabled, so a missing method is a no-op,
    # not an error.
    try:
        conn.enable_load_extension(False)
    except AttributeError:
        pass
    pdf.to_sql(name, conn, index=False)
    return conn


def _build_sql(parsed: dict, columns: frozenset) -> tuple[str, list, list]:
    """-> (sql_text, params, labels). labels: (val_alias, cnt_alias_or_None,
    user_alias_or_None, func, arg) per aggregate, in select order."""
    table = parsed["table"]
    select_parts = []
    labels = []
    agg_i = 0
    for item in parsed["select"]:
        if item["kind"] == "col":
            select_parts.append(_quote(item["name"]) + " AS " + _quote(item["alias"] or item["name"]))
        else:
            func, arg, alias = item["func"], item["arg"], item["alias"]
            val_alias = f"__val_{agg_i}"
            if arg == "*":
                select_parts.append(f"COUNT(*) AS {val_alias}")
                labels.append((val_alias, None, alias, func, "*"))
            else:
                if arg not in columns:
                    raise DisclosureError(f"column '{arg}' is not in table '{table}'")
                fn_sql = "AVG" if func == "mean" else func.upper()
                select_parts.append(f"{fn_sql}({_quote(arg)}) AS {val_alias}")
                cnt_alias = f"__cnt_{agg_i}"
                select_parts.append(f"COUNT({_quote(arg)}) AS {cnt_alias}")
                labels.append((val_alias, cnt_alias, alias, func, arg))
            agg_i += 1

    sql = "SELECT " + ", ".join(select_parts) + " FROM " + _quote(table)
    params: list = []
    if parsed["where"]:
        clauses = []
        for i, cond in enumerate(parsed["where"]):
            if cond["col"] not in columns:
                raise DisclosureError(f"column '{cond['col']}' is not in table '{table}'")
            prefix = "" if i == 0 else f" {cond['combinator'].upper()} "
            clauses.append(f"{prefix}{_quote(cond['col'])} {cond['op']} ?")
            params.append(cond["value"])
        sql += " WHERE " + "".join(clauses)
    if parsed["group_by"]:
        for g in parsed["group_by"]:
            if g not in columns:
                raise DisclosureError(f"column '{g}' is not in table '{table}'")
        sql += " GROUP BY " + ", ".join(_quote(g) for g in parsed["group_by"])
    return sql, params, labels


def run_sql(code: str, verbs: SafeVerbs, sources: dict) -> "Released":
    from .result import Released   # local import: avoid a cycle at module load

    parsed = parse_query(code)
    table = parsed["table"]
    if table not in sources:
        raise ValidationError(f"unknown data source: {table!r}", kind="name")
    df = sources[table]
    pdf = df.to_pandas() if hasattr(df, "to_pandas") else pd.DataFrame(df)
    columns = frozenset(pdf.columns)

    sql, params, labels = _build_sql(parsed, columns)

    # AVG/SUM/COUNT are what SQL_AGGS maps to as real SQLite function names
    # (both "avg" and "mean" translate to the SQLite function AVG — see
    # _build_sql's fn_sql translation below).
    allowed_funcs = frozenset({"avg", "sum", "count"})
    conn = _connect_one_table(table, df, verbs._policy)
    try:
        conn.set_authorizer(_make_authorizer(table, columns, allowed_funcs))
        cur = conn.execute(sql, params)
        col_names = [d[0] for d in cur.description]
        rows = cur.fetchall()
    finally:
        conn.close()

    res = pd.DataFrame(rows, columns=col_names)
    group_by = parsed["group_by"]
    if group_by:
        res = res.dropna(subset=group_by)   # match pandas groupby(observed=True)
    idx = res.set_index(group_by) if group_by else None
    by = group_by[0] if len(group_by) == 1 else (group_by or None)

    rels = []
    for val_alias, cnt_alias, user_alias, func, arg in labels:
        if idx is not None:
            table_s = pd.to_numeric(idx[val_alias], errors="coerce")
            counts = (idx[val_alias] if cnt_alias is None else idx[cnt_alias]).fillna(0).astype(int)
        else:
            label = user_alias or arg
            table_s = pd.to_numeric(pd.Series([res[val_alias].iloc[0]], index=[label]),
                                    errors="coerce")
            counts = pd.Series([int(res[(cnt_alias or val_alias)].iloc[0])], index=[label])
        agg_name = "size" if arg == "*" else SQL_AGGS[func]
        rel = verbs._release_group_agg(table_s, counts, agg=agg_name, by=by,
                                       value=arg, backend="sqlite")
        if user_alias:
            rel.payload["name"] = user_alias
        rels.append(rel)

    if len(rels) == 1:
        return rels[0]
    index = rels[0].payload["index"]
    dicts = [dict(zip(r.payload["index"], r.payload["values"])) for r in rels]
    out_columns = [r.payload["name"] for r in rels]
    data = [[d.get(g) for d in dicts] for g in index]
    return Released(
        {"type": "frame", "columns": out_columns, "index": index, "data": data},
        audit={"kind": "table", "verb": "sql_agg_compound", "by": by,
               "stats": out_columns, "backend": "sqlite"})
