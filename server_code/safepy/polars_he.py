# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the safepy repo. This file is produced by sync_to_api.py.
# Edit the engine in the safepy repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""Polars over encrypted data (Plane B) — the polars-expression surface of the
one-core-many-surfaces design (spec §3).

Like the pandas :class:`safepy.he.HEFrame`, this is a translating *facade*, not
an executor: polars cannot run on ciphertext, so it recognises the HE-computable
polars subset and routes to an ``HEAuthority`` via
:class:`safepy.backend.ReleaseBackend`. It reuses polars_api's ``SafeExpr`` /
``SafePl`` expression builders — ``pl.col("salary").mean()`` records ``(col,
agg)`` without touching data, and the facade reads those off the expression.

Supported:  df.group_by(*by).agg(pl.col(c).mean()/.sum()/.count()/.std()/.var())
            df.group_by(*by).len()
            df.value_counts(col) / df.crosstab(row, col)
            df.ols(y=..., x=...)
Refused:    median/order stats, derived-expression reducers (e.g.
            (pl.col('a')+1).mean()), filters/selects on encrypted columns.
An analyst writes ordinary polars with a smaller menu — not a new vocabulary.
"""

from __future__ import annotations

from .errors import DisclosureError
from .policy import Policy
from .result import Released


class HEPolarsGroupBy:
    """``df.group_by(*by)`` over an encrypted dataset — only ``agg`` of simple
    column reducers and ``len()``, each routed to the HEAuthority."""

    _is_polars_intermediate = True

    def __init__(self, ds, by, auth):
        self._ds, self._by, self._auth = ds, list(by), auth

    def len(self):
        # size counts rows per group and never uses a value's ciphertext, but the
        # authority still needs a valid encrypted column as the count carrier.
        value = next(iter(self._ds["value_columns"]), None)
        if value is None:
            raise DisclosureError("dataset has no encrypted value column to count over")
        by = self._by[0] if len(self._by) == 1 else self._by
        return self._auth.group_agg(self._ds, by, value, "size")

    def agg(self, *exprs):
        if not exprs:
            raise DisclosureError(
                "agg needs at least one reducer, e.g. pl.col('salary').mean()")
        rels = [self._one(e) for e in exprs]
        return rels[0] if len(rels) == 1 else self._combine(rels)

    def _one(self, e) -> Released:
        from .polars_api import SafeExpr
        if not isinstance(e, SafeExpr) or e._agg is None:
            raise DisclosureError(
                "agg needs reducers over columns, e.g. pl.col('salary').mean()")
        if e._col is None:
            raise DisclosureError(
                "on encrypted data, aggregate a single column "
                "(e.g. pl.col('salary').mean()), not a derived expression")
        by = self._by[0] if len(self._by) == 1 else self._by
        rel = self._auth.group_agg(self._ds, by, e._col, e._agg)
        if e._name:
            rel.payload["name"] = e._name
        return rel

    def _combine(self, rels) -> Released:
        """Assemble several suppressed per-group series into one frame aligned on
        the shared group index (mirrors polars_api.SafePolarsGroupBy._combine)."""
        index = rels[0].payload["index"]
        columns = [r.payload["name"] for r in rels]
        dicts = [dict(zip(r.payload["index"], r.payload["values"])) for r in rels]
        data = [[d.get(g) for d in dicts] for g in index]
        return Released(
            {"type": "frame", "columns": columns, "index": index, "data": data},
            audit={"kind": "table", "verb": "group_agg_compound", "by": self._by,
                   "stats": columns, "backend": "paillier"})


class HEPolarsFrame:
    """Capability facade over an encrypted dataset with the polars idiom — the
    only object polars-he scripts can reach. The gate blocks ``_``-private
    attributes, so the ciphertext and key are unreachable from user code."""

    _is_heframe = True

    def __init__(self, source, authority):
        self._ds = source.dataset
        self._auth = authority

    def group_by(self, *by) -> HEPolarsGroupBy:
        cols = [c for grp in by for c in ([grp] if isinstance(grp, str) else grp)]
        return HEPolarsGroupBy(self._ds, cols, self._auth)

    def value_counts(self, col: str, *, min_n=None, round=None) -> Released:
        return self._auth.value_counts(self._ds, col, min_n=min_n, round=round)

    def crosstab(self, row: str, col: str, *, min_n=None, round=None) -> Released:
        return self._auth.crosstab(self._ds, row, col, min_n=min_n, round=round)

    def ols(self, *, y: str, x, min_n=None) -> Released:
        return self._auth.ols(self._ds, y=y, x=x, min_n=min_n)


def build_he_polars_namespace(sources: dict, policy: Policy) -> dict:
    """Namespace for ``dialect="polars-he"``: each source becomes an
    ``HEPolarsFrame`` bound to its own authority. ``pl`` arrives via
    ``import polars as pl`` (mapped to ``SafePl`` by the runtime)."""
    from .he import EncryptedSource, HEAuthority
    ns = {}
    for name, src in sources.items():
        if not isinstance(src, EncryptedSource):
            raise DisclosureError(
                f"source '{name}' is not an EncryptedSource; the 'polars-he' "
                f"dialect only accepts encrypted datasets")
        ns[name] = HEPolarsFrame(src, HEAuthority(src.private_key, policy))
    return ns
