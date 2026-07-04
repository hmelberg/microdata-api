# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the safepy repo. This file is produced by sync_to_api.py.
# Edit the engine in the safepy repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""DuckDB SQL over encrypted data (Plane B) — the SQL surface of the
one-core-many-surfaces design (spec §3).

Unlike the plaintext duckdb dialect this *cannot execute* the query (SQL can't
run on ciphertext), so it reuses duckdb only as a **parser**: it serializes the
SQL to an AST (``json_serialize_sql``), gates it with the same default-deny walk,
then maps the GROUP BY + aggregate intent to an ``HEAuthority`` via
:class:`safepy.backend.ReleaseBackend`. No tables are registered and nothing is
executed.

Supported:  SELECT g1[, g2], agg(col)[, agg2(col)] FROM t GROUP BY g1[, g2]
            where agg ∈ avg/sum/count/count(*)/stddev/var_samp
Refused:    WHERE/HAVING (filters/oracles), expressions inside aggregates
            (avg(x*2)), COUNT(DISTINCT), median/order stats, joins/subqueries,
            whole-table (no GROUP BY) aggregates. An analyst writes ordinary SQL
            with a smaller menu — not a new language.
"""

from __future__ import annotations

from .duckdb_api import _SQL_AGGS, _colname, _is_agg, _parse, _walk
from .errors import DisclosureError, ValidationError
from .result import Released


def translate_sql_he(code: str, policy, sources: dict) -> Released:
    """Parse one SQL SELECT over an encrypted source and release through the
    shared core. ``sources`` maps names to :class:`safepy.he.EncryptedSource`."""
    from .he import EncryptedSource, HEAuthority

    if not code.strip():
        raise ValidationError("empty program", kind="empty")
    try:
        import duckdb
    except ImportError:  # pragma: no cover
        raise DisclosureError("the 'duckdb' package is required for the duckdb dialect")

    con = duckdb.connect()                    # parse-only: no tables, never executed
    ast, node = _parse(con, code)
    _walk(ast["statements"])                  # default-deny gate on the whole tree

    if node.get("where_clause"):
        raise DisclosureError(
            "WHERE is not available on encrypted data; filtering needs comparisons "
            "on ciphertext. Group only on the plaintext group columns.")
    if node.get("having") or node.get("qualify"):
        raise DisclosureError("HAVING/QUALIFY filter on exact aggregate values (an oracle)")

    ft = node.get("from_table") or {}
    if ft.get("type") != "BASE_TABLE":
        raise DisclosureError(
            "only a single table is supported on encrypted data (no joins or subqueries)")
    name = ft.get("table_name")
    if name not in sources:
        raise ValidationError(f"unknown data source: {name!r}", kind="name")
    src = sources[name]
    if not isinstance(src, EncryptedSource):
        raise DisclosureError(
            f"source '{name}' is not an EncryptedSource; the 'duckdb-he' dialect "
            f"only reads encrypted datasets")
    ds = src.dataset
    auth = HEAuthority(src.private_key, policy)

    group_exprs = (node.get("group_expressions")
                   or (node.get("groups") or {}).get("group_expressions") or [])
    by_cols = []
    for g in group_exprs:
        if g.get("class") != "COLUMN_REF":
            raise DisclosureError(
                "GROUP BY must use plain columns on encrypted data (no expressions)")
        by_cols.append(_colname(g))
    if not by_cols:
        raise DisclosureError(
            "a GROUP BY is required; whole-table aggregates over encrypted data "
            "are not released")
    by = by_cols[0] if len(by_cols) == 1 else by_cols

    specs = []   # (agg, value, alias)
    for it in node.get("select_list") or []:
        if it.get("class") == "COLUMN_REF":
            if _colname(it) not in by_cols:
                raise DisclosureError(f"column '{_colname(it)}' must be a GROUP BY key")
            continue
        if not _is_agg(it):
            raise DisclosureError(
                "each SELECT item must be a GROUP BY key or a safe aggregate "
                "(avg/sum/count/stddev/var_samp)")
        if it.get("distinct"):
            raise DisclosureError(
                "COUNT(DISTINCT ...) is not computable on encrypted data (distinct "
                "counting is not additively homomorphic)")
        fname = (it.get("function_name") or "").lower()
        agg = _SQL_AGGS[fname]
        alias = it.get("alias") or None
        if fname == "count_star":
            value = next(iter(ds["value_columns"]), None)   # size carrier (unused ciphertext)
            if value is None:
                raise DisclosureError("dataset has no encrypted value column to count over")
            specs.append(("size", value, alias))
        else:
            children = it.get("children") or []
            if len(children) != 1 or children[0].get("class") != "COLUMN_REF":
                raise DisclosureError(
                    "aggregates must be over a single column on encrypted data "
                    "(e.g. avg(salary)), not an expression")
            specs.append((agg, _colname(children[0]), alias))

    if not specs:
        raise DisclosureError(
            "the query must compute at least one aggregate; raw rows are never released")

    rels = []
    for agg, value, alias in specs:
        rel = auth.group_agg(ds, by, value, agg)
        if alias:
            rel.payload["name"] = alias
        rels.append(rel)
    if len(rels) == 1:
        return rels[0]

    index = rels[0].payload["index"]
    columns = [r.payload["name"] for r in rels]
    dicts = [dict(zip(r.payload["index"], r.payload["values"])) for r in rels]
    data = [[d.get(g) for d in dicts] for g in index]
    return Released(
        {"type": "frame", "columns": columns, "index": index, "data": data},
        audit={"kind": "table", "verb": "sql_agg_compound", "by": by,
               "stats": columns, "backend": "paillier"})
