# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the m2py repo. This file is produced by sync_to_api.py.
# Edit the engine in the m2py repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""Compile a microdata expression/condition into a ``polars.Expr``.

Strategy: reuse the emulator's own syntax normalisers (``_micro_expr_fixup`` for
expressions, ``_stata_like_bool_fixup`` for conditions) so we accept exactly the
microdata operator syntax the emulator does, then parse the normalised string
with Python's ``ast`` and translate node-by-node to polars expressions.

Coverage (vertical slice): column refs, numeric/string literals, ``+ - * / % **``,
unary ``-``/``~``, comparisons, ``and/or`` and ``& |``, a whitelist of element-wise
functions, and ``np.where``/``where``. Anything outside this set raises
:class:`UnsupportedExpr` so the translator can emit an ``UNTRANSLATED`` marker
instead of silently-wrong polars.
"""

import ast


class UnsupportedExpr(ValueError):
    """Raised when an expression uses syntax the polars compiler can't map."""


def compile_expr(expr, *, condition=False):
    """Return a ``polars.Expr`` for ``expr``. Set ``condition=True`` for if-clauses."""
    import m2py

    expr = m2py._micro_expr_fixup(expr)
    # Stata-like &/| precedence: always for conditions, and for expressions that
    # use &/| (Python binds & tighter than >=, so `a >= 1 & a < 9` needs fixing).
    if condition or (isinstance(expr, str) and ("&" in expr or "|" in expr)):
        expr = m2py._stata_like_bool_fixup(expr)
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:  # pragma: no cover - normaliser usually prevents this
        raise UnsupportedExpr(f"cannot parse {expr!r}: {e}")
    return _conv(tree.body)


# unary numpy/microdata funcs -> polars Expr method name
_UNARY_METHOD = {
    "log": "log", "ln": "log", "log10": "log10", "exp": "exp", "sqrt": "sqrt",
    "abs": "abs", "abs_": "abs", "floor": "floor", "ceil": "ceil",
    "sin": "sin", "cos": "cos", "tan": "tan",
    "acos": "arccos", "asin": "arcsin", "atan": "arctan",
}

# horizontal (row-wise across columns) microdata funcs -> polars helper
_ROW_FN = {"rowmean": "mean_horizontal", "rowmax": "max_horizontal",
           "rowmin": "min_horizontal", "rowtotal": "sum_horizontal",
           "rowsum": "sum_horizontal"}


def _pl():
    import polars as pl
    return pl


def _conv(node):
    pl = _pl()

    if isinstance(node, ast.Expression):
        return _conv(node.body)

    if isinstance(node, ast.BinOp):
        l, r, op = _conv(node.left), _conv(node.right), node.op
        if isinstance(op, ast.Add):  return l + r
        if isinstance(op, ast.Sub):  return l - r
        if isinstance(op, ast.Mult): return l * r
        if isinstance(op, ast.Div):  return l / r
        if isinstance(op, ast.Mod):  return l % r
        if isinstance(op, ast.Pow):  return l ** r
        if isinstance(op, ast.BitAnd): return l & r
        if isinstance(op, ast.BitOr):  return l | r
        raise UnsupportedExpr(f"operator {type(op).__name__}")

    if isinstance(node, ast.BoolOp):
        vals = [_conv(v) for v in node.values]
        acc = vals[0]
        for v in vals[1:]:
            acc = (acc & v) if isinstance(node.op, ast.And) else (acc | v)
        return acc

    if isinstance(node, ast.UnaryOp):
        operand = _conv(node.operand)
        if isinstance(node.op, ast.USub):   return -operand
        if isinstance(node.op, ast.Invert): return ~operand
        if isinstance(node.op, ast.Not):    return ~operand
        if isinstance(node.op, ast.UAdd):   return operand
        raise UnsupportedExpr(f"unary {type(node.op).__name__}")

    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            raise UnsupportedExpr("chained comparison")
        l, r, op = _conv(node.left), _conv(node.comparators[0]), node.ops[0]
        if isinstance(op, ast.Gt):    return l > r
        if isinstance(op, ast.GtE):   return l >= r
        if isinstance(op, ast.Lt):    return l < r
        if isinstance(op, ast.LtE):   return l <= r
        if isinstance(op, ast.Eq):    return l == r
        if isinstance(op, ast.NotEq): return l != r
        raise UnsupportedExpr(f"comparison {type(op).__name__}")

    if isinstance(node, ast.Call):
        return _conv_call(node)

    if isinstance(node, ast.Name):
        if node.id == "np":
            raise UnsupportedExpr("bare numpy reference")
        if node.id in ("nan", "NaN"):
            return pl.lit(None)
        return pl.col(node.id)

    if isinstance(node, ast.Attribute):
        # np.nan -> null literal
        if (isinstance(node.value, ast.Name) and node.value.id == "np"
                and node.attr == "nan"):
            return pl.lit(None)
        raise UnsupportedExpr(f"attribute {ast.dump(node)}")

    if isinstance(node, ast.Constant):
        return pl.lit(node.value)

    raise UnsupportedExpr(f"node {type(node).__name__}")


def _func_name(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"  # e.g. np.where, np.log
    raise UnsupportedExpr("complex callable")


def _conv_call(node):
    pl = _pl()
    name = _func_name(node.func)
    bare = name.split(".")[-1]
    args = [_conv(a) for a in node.args]

    if bare in ("where",) and len(args) == 3:          # np.where(cond, a, b)
        cond, a, b = args
        return pl.when(cond).then(a).otherwise(b)
    if bare in ("minimum", "min") and len(args) == 2:
        return pl.min_horizontal(args[0], args[1])
    if bare in ("maximum", "max") and len(args) == 2:
        return pl.max_horizontal(args[0], args[1])
    if bare == "round":
        ndigits = node.args[1].value if len(node.args) > 1 else 0
        return args[0].round(ndigits)

    # truncate toward zero (microdata int()) -> polars cast drops the fraction
    if bare in ("int", "int_") and len(args) == 1:
        return args[0].cast(pl.Int64, strict=False)

    # missing-value predicates (used in conditions)
    if bare in ("sysmiss", "missing") and len(args) == 1:
        return args[0].is_null()
    if bare in ("nonmissing",) and len(args) == 1:
        return args[0].is_not_null()

    # row-wise (horizontal) aggregations
    if bare in _ROW_FN and len(args) >= 1:
        return getattr(pl, _ROW_FN[bare])(*args)
    if bare == "rowmissing":
        return sum((a.is_null().cast(pl.Int64) for a in args[1:]), args[0].is_null().cast(pl.Int64))
    if bare == "rowvalid":
        return sum((a.is_not_null().cast(pl.Int64) for a in args[1:]),
                   args[0].is_not_null().cast(pl.Int64))

    # membership / range
    if bare == "inlist" and len(args) >= 2:
        consts = [n.value for n in node.args[1:] if isinstance(n, ast.Constant)]
        if len(consts) == len(node.args) - 1:
            return args[0].is_in(consts)
        raise UnsupportedExpr("inlist with non-literal values")
    if bare == "inrange" and len(args) == 3:
        return (args[0] >= args[1]) & (args[0] <= args[2])

    if bare == "logit" and len(args) == 1:
        return (args[0] / (1 - args[0])).log()
    if bare in ("to_int",) and len(args) == 1:
        return args[0].cast(pl.Int64, strict=False)

    # string functions
    if bare in ("trim",) and len(args) == 1:
        return args[0].cast(pl.Utf8).str.strip_chars()
    if bare == "ltrim" and len(args) == 1:
        return args[0].cast(pl.Utf8).str.strip_chars_start()
    if bare == "rtrim" and len(args) == 1:
        return args[0].cast(pl.Utf8).str.strip_chars_end()
    if bare == "startswith" and len(args) == 2:
        return args[0].cast(pl.Utf8).str.starts_with(node.args[1].value)
    if bare == "endswith" and len(args) == 2:
        return args[0].cast(pl.Utf8).str.ends_with(node.args[1].value)
    if bare == "substr" and len(args) == 3:
        # microdata substr(x, pos, length): pos is 1-based; negative pos = from end
        pos = node.args[1].value
        length = node.args[2].value
        offset = pos - 1 if pos > 0 else pos
        return args[0].cast(pl.Utf8).str.slice(offset, length)
    if bare in ("string", "to_str") and len(args) == 1:
        return args[0].cast(pl.Utf8)
    if bare in ("lower",) and len(args) == 1:
        return args[0].cast(pl.Utf8).str.to_lowercase()
    if bare in ("upper",) and len(args) == 1:
        return args[0].cast(pl.Utf8).str.to_uppercase()
    if bare in ("length", "strlen") and len(args) == 1:
        return args[0].cast(pl.Utf8).str.len_chars()

    if bare in _UNARY_METHOD and len(args) == 1:
        return getattr(args[0], _UNARY_METHOD[bare])()

    raise UnsupportedExpr(f"function {name}()")
