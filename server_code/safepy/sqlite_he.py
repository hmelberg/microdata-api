# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the safepy repo. This file is produced by sync_to_api.py.
# Edit the engine in the safepy repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""SQL over encrypted data (Plane B), narrow-grammar variant — the SQL
surface of the one-core-many-surfaces design (spec §3), companion to
sqlite_api.py the same way duckdb_he.py is to duckdb_api.py.

Unlike the plaintext sqlite dialect this *cannot execute* the query (SQL
can't run on ciphertext), so it reuses sqlite_grammar only as a **parser**:
parse (never execute) the same narrow SELECT/FROM/WHERE/GROUP BY grammar,
gate it, then map the GROUP BY + aggregate intent to an HEAuthority via
:class:`safepy.he.HEAuthority`. No sqlite connection is ever opened.

Supported:  SELECT g1[, g2], agg(col)[, agg2(col)] FROM t GROUP BY g1[, g2]
            where agg ∈ avg/mean/sum/count/count(*)
Refused:    WHERE (ciphertext can't be filtered — ties into the shared HE
            constraint enforced identically in duckdb_he.py), no GROUP BY
            (whole-table aggregates over encrypted data are not released),
            COUNT(DISTINCT ...) (not additively homomorphic — also not in
            this grammar's menu at all, so it can't even be spelled).
"""
from __future__ import annotations

from .errors import DisclosureError, ValidationError
from .result import Released
from .sqlite_grammar import SQL_AGGS, parse_query


def translate_sql_he(code: str, policy, sources: dict) -> Released:
    """Parse one narrow-grammar SELECT over an encrypted source and release
    through the shared core. ``sources`` maps names to
    :class:`safepy.he.EncryptedSource`."""
    from .he import EncryptedSource, HEAuthority

    parsed = parse_query(code)
    if parsed["where"]:
        raise DisclosureError(
            "WHERE is not available on encrypted data; filtering needs comparisons "
            "on ciphertext. Group only on the plaintext group columns.")
    if not parsed["group_by"]:
        raise DisclosureError(
            "a GROUP BY is required; whole-table aggregates over encrypted data "
            "are not released")

    name = parsed["table"]
    if name not in sources:
        raise ValidationError(f"unknown data source: {name!r}", kind="name")
    src = sources[name]
    if not isinstance(src, EncryptedSource):
        raise DisclosureError(
            f"source '{name}' is not an EncryptedSource; the 'sqlite-he' dialect "
            f"only reads encrypted datasets")
    ds = src.dataset
    auth = HEAuthority(src.private_key, policy)

    by_cols = parsed["group_by"]
    by = by_cols[0] if len(by_cols) == 1 else by_cols

    specs = []   # (agg, value, alias)
    for item in parsed["select"]:
        if item["kind"] == "col":
            continue   # already validated (in group_by) by parse_query
        func, arg, alias = item["func"], item["arg"], item["alias"]
        if arg == "*":
            value = next(iter(ds["value_columns"]), None)   # size carrier (unused ciphertext)
            if value is None:
                raise DisclosureError("dataset has no encrypted value column to count over")
            specs.append(("size", value, alias))
        else:
            specs.append((SQL_AGGS[func], arg, alias))

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
