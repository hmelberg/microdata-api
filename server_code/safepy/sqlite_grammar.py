# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the safepy repo. This file is produced by sync_to_api.py.
# Edit the engine in the safepy repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""Shared narrow-grammar SQL parser for the sqlite/sqlite-he dialects (spec
2026-07-08-sqlite-dialect-design.md). Deliberately NOT a general SQL parser —
exactly one shape, chosen so the whole grammar can be reasoned about by hand
(no AST-introspection facility exists for sqlite3 the way duckdb's
json_serialize_sql exists for the duckdb dialect; see the design doc for why
that rules out porting duckdb_api.py's approach):

    SELECT <col|agg(col)|agg(*)> [AS alias] (',' ...)
    FROM <table>
    [WHERE <col> <op> <literal> [(AND|OR) ...]]
    GROUP BY <col> (',' ...)

No subqueries, joins, CTEs, window functions, HAVING, ORDER BY, LIMIT,
parenthesized WHERE expressions, or function calls inside WHERE. A query
that doesn't fit this shape raises ValidationError (parse failure), not a
partial/best-effort match.

The aggregate/scalar whitelists are imported from duckdb_api.py so there is
one canonical list, not a second copy that could silently drift from it.
"""
from __future__ import annotations

import re

from .duckdb_api import _SQL_AGGS
from .errors import DisclosureError, ValidationError

__all__ = ["parse_query", "SQL_AGGS"]

# Deliberately NARROWER than duckdb_api.py's own _SQL_AGGS. Two independent
# reasons, checked empirically before choosing this list (see the design
# doc): (1) SQLite has no native STDDEV/VARIANCE/VAR_SAMP — they raise
# "no such function" at execution time, so including them would be an
# accepted-but-broken whitelist entry. (2) SQLite DOES have a native MEDIAN
# aggregate (confirmed working, correct results) but median is an order
# statistic — its correct release rule is "min(#<=v, #>=v) >= min_n", not
# the plain contributing-count check _release_group_agg applies (see the
# 2026-07-07 safepy fix to SafeColumn.median()/SafeFrame.median() in the
# safestat codebase's docs/superpowers/2026-07-07-code-review.md §1a for
# the exact shape of that gap). Whether duckdb_api.py's own SQL-median
# support (same _release_group_agg count-based path) already has the
# identical gap is a genuinely open question this module does not resolve —
# rather than silently copy a pattern of uncertain correctness into new
# code, median is simply not in this dialect's menu.
SQL_AGGS = {k: v for k, v in _SQL_AGGS.items() if k in ("avg", "mean", "sum", "count")}

_KEYWORDS = {"select", "from", "where", "group", "by", "as", "and", "or"}
_COMPARISON_OPS = ("<=", ">=", "<>", "!=", "=", "<", ">")

_TOKEN_RE = re.compile(r"""
    \s*(?:
        (?P<string>'(?:[^']|'')*')
      | (?P<number>-?\d+(?:\.\d+)?)
      | (?P<op><=|>=|<>|!=|=|<|>|,|\(|\)|\*)
      | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    )
""", re.VERBOSE)


def _tokenize(sql: str) -> list[tuple[str, str]]:
    """Comments are skipped like whitespace, not preserved anywhere — the
    reconstructed query safepy actually executes is built from validated
    tokens, never this raw text, so a comment can't smuggle anything past
    the gate; it's simply discarded. -- to end of line and /* ... */ are
    both supported since the app's own connect/load directive lines
    conventionally use "--" in SQL-dialect modes (a real SQL comment,
    unlike "#"/"//") specifically so they coexist with a real SQL query in
    the same script — any realistic script reaching this dialect has such
    lines above the actual query."""
    tokens = []
    pos, n = 0, len(sql)
    while pos < n:
        if sql[pos].isspace():
            pos += 1
            continue
        if sql[pos:pos + 2] == "--":
            nl = sql.find("\n", pos)
            pos = n if nl == -1 else nl + 1
            continue
        if sql[pos:pos + 2] == "/*":
            end = sql.find("*/", pos + 2)
            if end == -1:
                raise ValidationError("unterminated /* comment */", kind="parse")
            pos = end + 2
            continue
        m = _TOKEN_RE.match(sql, pos)
        if not m or m.end() == pos:
            raise ValidationError(f"could not parse SQL near: {sql[pos:pos + 20]!r}", kind="parse")
        pos = m.end()
        if m.group("string") is not None:
            tokens.append(("string", m.group("string")[1:-1].replace("''", "'")))
        elif m.group("number") is not None:
            tokens.append(("number", m.group("number")))
        elif m.group("op") is not None:
            tokens.append(("op", m.group("op")))
        else:
            word = m.group("ident")
            tokens.append(("kw", word.lower()) if word.lower() in _KEYWORDS else ("ident", word))
    return tokens


class _Cursor:
    def __init__(self, tokens):
        self._t = tokens
        self._i = 0

    def peek(self):
        return self._t[self._i] if self._i < len(self._t) else (None, None)

    def next(self):
        tok = self.peek()
        self._i += 1
        return tok

    def expect_kw(self, word):
        kind, val = self.next()
        if kind != "kw" or val != word:
            raise ValidationError(f"expected '{word.upper()}', got {val!r}", kind="parse")

    def expect_ident(self) -> str:
        kind, val = self.next()
        if kind != "ident":
            raise ValidationError(f"expected an identifier, got {val!r}", kind="parse")
        return val

    def at_end(self) -> bool:
        return self._i >= len(self._t)


def _parse_select_item(c: _Cursor) -> dict:
    kind, val = c.peek()
    if kind == "ident" and val.lower() in SQL_AGGS:
        func = val.lower()
        c.next()
        c.expect_op = None
        okind, oval = c.next()
        if okind != "op" or oval != "(":
            raise ValidationError(f"expected '(' after {func}", kind="parse")
        skind, sval = c.peek()
        if skind == "op" and sval == "*":
            c.next()
            arg = "*"
        else:
            arg = c.expect_ident()
        ckind, cval = c.next()
        if ckind != "op" or cval != ")":
            raise ValidationError(f"expected ')' after {func}(...", kind="parse")
        alias = _maybe_alias(c)
        return {"kind": "agg", "func": func, "arg": arg, "alias": alias}
    name = c.expect_ident()
    alias = _maybe_alias(c)
    return {"kind": "col", "name": name, "alias": alias}


def _maybe_alias(c: _Cursor):
    kind, val = c.peek()
    if kind == "kw" and val == "as":
        c.next()
        return c.expect_ident()
    return None


_LITERAL_KINDS = {"string", "number"}


def _parse_literal(c: _Cursor):
    kind, val = c.next()
    if kind not in _LITERAL_KINDS:
        raise ValidationError("WHERE literals must be a quoted string or a number", kind="parse")
    return float(val) if (kind == "number" and ("." in val)) else (
        int(val) if kind == "number" else val)


def _parse_where(c: _Cursor) -> list:
    conds = [{"combinator": None, **_parse_comparison(c)}]
    while True:
        kind, val = c.peek()
        if kind == "kw" and val in ("and", "or"):
            c.next()
            conds.append({"combinator": val, **_parse_comparison(c)})
        else:
            break
    return conds


def _parse_comparison(c: _Cursor) -> dict:
    col = c.expect_ident()
    kind, val = c.next()
    if kind != "op" or val not in _COMPARISON_OPS:
        raise ValidationError(
            f"WHERE comparisons must use one of {_COMPARISON_OPS}, got {val!r}", kind="parse")
    lit = _parse_literal(c)
    return {"col": col, "op": val, "value": lit}


def parse_query(sql: str) -> dict:
    """-> {"table": str, "select": [...], "where": [...] | None, "group_by": [str]}.
    Raises ValidationError on anything outside the narrow grammar; raises
    DisclosureError for a construct the grammar recognizes but that is
    itself refused (e.g. no aggregate present at all)."""
    if not sql.strip():
        raise ValidationError("empty program", kind="empty")
    c = _Cursor(_tokenize(sql))
    c.expect_kw("select")
    select = [_parse_select_item(c)]
    while True:
        kind, val = c.peek()
        if kind == "op" and val == ",":
            c.next()
            select.append(_parse_select_item(c))
        else:
            break
    if not any(item["kind"] == "agg" for item in select):
        raise DisclosureError(
            "the query must compute at least one aggregate; raw rows or "
            "distinct values are never released")
    c.expect_kw("from")
    table = c.expect_ident()
    where = None
    kind, val = c.peek()
    if kind == "kw" and val == "where":
        c.next()
        where = _parse_where(c)
    c.expect_kw("group")
    c.expect_kw("by")
    group_by = [c.expect_ident()]
    while True:
        kind, val = c.peek()
        if kind == "op" and val == ",":
            c.next()
            group_by.append(c.expect_ident())
        else:
            break
    if not c.at_end():
        kind, val = c.peek()
        raise ValidationError(
            f"unsupported SQL construct near {val!r} (only SELECT ... FROM ... "
            f"[WHERE ...] GROUP BY ... is supported)", kind="parse")
    group_cols = set(group_by)
    for item in select:
        if item["kind"] == "col" and item["name"] not in group_cols:
            raise DisclosureError(
                f"column '{item['name']}' must be a GROUP BY key")
    return {"table": table, "select": select, "where": where, "group_by": group_by}
