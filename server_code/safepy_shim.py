# microdata-api/server_code/safepy_shim.py
"""Native shim between /run_extended and the vendored safepy engine.

Runs dialect scripts (pandas/polars/r/duckdb) against registry sources through
safepy's STRICT capability core and adapts SafeResult into the client result
shape that renderSafeStatResult already consumes:

    {code, out, html, n, err, figs, results, datasetInfo, audit}

This adapter is the single seam that decides the transport format (HTML
fragments for tables, plotly JSON for figures); nothing else knows about it.
"""
from __future__ import annotations

import html as _html
import json
import os

# safepy reads SAFEPY_NOISE_SALT at import time (cell-key count noise), so the
# secret must be in the environment BEFORE `import safepy`. Keep every safepy
# import in this module lazy and behind this block.
try:
    import anvil.secrets
    _salt = anvil.secrets.get_secret("safepy_noise_salt")
    if _salt:
        os.environ.setdefault("SAFEPY_NOISE_SALT", _salt)
except Exception:
    pass  # non-Anvil test run; prod MUST configure the safepy_noise_salt secret

SAFEPY_DIALECTS = {"pandas", "polars", "r", "duckdb"}

# Dialects that need an optional third-party engine on the server.
_DIALECT_DEPS = {"polars": "polars", "duckdb": "duckdb"}

_LEVEL_ORDER = {"public": 0, "protected": 1, "sensitive": 2}


def run_extended(script, sources_req, dialect="pandas"):
    """Execute `script` in `dialect` against registry sources via safepy STRICT.

    Levels and locations come from the registry only — never the request. The
    most restrictive source level selects the safepy policy tier.
    """
    dep = _DIALECT_DEPS.get(dialect)
    if dep is not None:
        try:
            __import__(dep)
        except ImportError:
            return _error_shape(
                script, f"dialect '{dialect}' er ikke tilgjengelig på serveren "
                        f"(mangler pakken '{dep}')")

    from source_registry import resolve_source, load_dataframe

    frames, level = {}, "public"
    for s in sources_req or []:
        alias, sid = s.get("alias"), s.get("source_id")
        if not alias or not sid:
            return _error_shape(script, "hver kilde må ha 'alias' og 'source_id'")
        try:
            src = resolve_source(sid)
        except KeyError:
            return _error_shape(script, f"ukjent kilde: {sid}")
        frames[alias] = load_dataframe(src)
        if _LEVEL_ORDER.get(src["level"], 1) > _LEVEL_ORDER[level]:
            level = src["level"]

    import safepy
    # profile="strict" is mandatory: safepy's own policy would give the
    # protected level the OPEN sandbox (safepy/policy.py), and only the STRICT
    # capability facade is safe-by-construction for user-supplied code.
    res = safepy.run(script, frames, level=level, profile="strict",
                     dialect=dialect, render="plotly")
    return _to_client_shape(script, res.as_dict())


# ---------------------------------------------------------------------------
# SafeResult -> client shape
# ---------------------------------------------------------------------------

def _error_shape(script, message):
    return {"code": script, "out": "", "html": "", "n": None, "err": message,
            "figs": [], "results": [], "datasetInfo": {}, "audit": None}


def _to_client_shape(script, d):
    figs, results = [], []
    leaves = d.get("results")
    if not leaves:
        leaves = [d] if d.get("kind") not in ("none", "error", None) else []
    for leaf in leaves:
        payload = leaf.get("payload")
        if leaf.get("kind") in ("chart", "plot"):
            fig = _fig_json(payload)
            if fig is not None:
                figs.append(fig)
            continue
        html_frag = _leaf_html(leaf.get("kind"), payload)
        if html_frag:
            results.append(html_frag)

    info = {}
    for entry in d.get("catalog") or []:
        cols = entry.get("columns") or []
        info[entry["name"]] = {
            "columns": [c["name"] for c in cols],
            "dtypes": {c["name"]: c["dtype"] for c in cols},
            "nrows": entry.get("n_rows"),
        }

    err = None
    if d.get("error"):
        e = d["error"]
        err = f'{e.get("kind", "Error")}: {e.get("message", "")}'.strip()

    return {"code": script, "out": "", "html": "", "n": None, "err": err,
            "figs": figs, "results": results, "datasetInfo": info,
            "audit": d.get("audit")}


def _fig_json(payload):
    """Chart payload -> plotly JSON string for the client's mdRenderPlotlyFigure."""
    if not isinstance(payload, dict):
        return None
    if payload.get("format") == "plotly":
        return payload.get("content")
    # Raw chart spec (render="spec" fallback): ship as-is; the client's JSON
    # parse will fail gracefully and log, never crash the page.
    try:
        return json.dumps(payload)
    except (TypeError, ValueError):
        return None


def _esc(v):
    return _html.escape(str(v))


def _cell(v):
    """Suppressed cells arrive as None; render the microdata-style dot."""
    if v is None:
        return "·"
    if isinstance(v, float) and v == int(v):
        return _esc(int(v))
    return _esc(v)


def _table(headers, rows):
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_cell(v)}</td>" for v in row) + "</tr>"
        for row in rows)
    return ('<table border="0" class="output-table">'
            f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")


def _leaf_html(kind, payload):
    """One released result -> an HTML fragment (matches m2py_remote's
    _render_result look: output-table tables, <pre> for the rest)."""
    if not isinstance(payload, dict):
        return None if payload is None else f"<pre>{_esc(payload)}</pre>"
    ptype = payload.get("type")
    if ptype == "series":
        name = payload.get("name") or "value"
        rows = list(zip(payload.get("index") or [], payload.get("values") or []))
        return _table(["", name], rows)
    if ptype == "frame":
        cols = payload.get("columns") or []
        index = payload.get("index") or []
        data = payload.get("data") or []
        rows = [[ix] + list(r) for ix, r in zip(index, data)]
        return _table([""] + cols, rows)
    if ptype == "scalar":
        label = payload.get("stat") or payload.get("name") or "value"
        return f"<pre>{_esc(label)}: {_cell(payload.get('value'))}</pre>"
    if ptype == "regression":
        terms = payload.get("terms") or []
        headers = ["term", "coef", "ci_low", "ci_high", "pvalue"]
        extra = [k for k in ("hazard_ratio",) if terms and k in terms[0]]
        rows = [[t.get(h) for h in headers + extra] for t in terms]
        meta = (f'<div style="font-size:12px;opacity:.7">'
                f'{_esc(payload.get("family", "model"))} · n={_esc(payload.get("n", "?"))}</div>')
        return meta + _table(headers + extra, rows)
    # Unknown payload type (marginal_effects, future kinds): readable JSON.
    try:
        return f"<pre>{_esc(json.dumps(payload, indent=2, default=str))}</pre>"
    except (TypeError, ValueError):
        return f"<pre>{_esc(payload)}</pre>"
