# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the safepy repo. This file is produced by sync_to_api.py.
# Edit the engine in the safepy repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
# safepy/client_shape.py
"""SafeResult dict -> the m2py client result shape.

    {code, out, html, n, err, figs, results, datasetInfo, audit}

One implementation shared by microdata-api's safepy_shim (server runs) and
the browser strict runner (Pyodide) — this module is the single seam that
decides the transport format (HTML fragments for tables, plotly JSON for
figures); nothing else knows about it. Pure Python, no Anvil, no safepy
imports (operates on plain dicts from SafeResult.as_dict())."""
from __future__ import annotations

import html as _html
import json


def error_shape(script, message):
    return {"code": script, "out": "", "html": "", "n": None, "err": message,
            "figs": [], "results": [], "datasetInfo": {}, "audit": None}


def to_client_shape(script, d):
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
        return _table([payload.get("index_name") or "", name], rows)
    if ptype == "frame":
        cols = payload.get("columns") or []
        index = payload.get("index") or []
        data = payload.get("data") or []
        # An unnamed plain 0..n-1 counter index carries no information — hide
        # it. Payload indexes arrive stringified, so compare as strings.
        if (not payload.get("index_name")
                and [str(i) for i in index] == [str(i) for i in range(len(data))]):
            return _table(cols, data)
        rows = [[ix] + list(r) for ix, r in zip(index, data)]
        return _table([payload.get("index_name") or ""] + cols, rows)
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


def leaf_fragment(leaf):
    """One streamed leaf -> a small client fragment for task_state. Charts are
    deferred to the final render (plotly JSON is too large to re-ship on every
    poll); everything else reuses the exact HTML the final results list will
    contain, so the progressive view and the final view can never disagree."""
    if leaf.get("kind") in ("chart", "plot"):
        return {"kind": "note",
                "html": '<pre style="opacity:.6">(figur klar — vises når kjøringen er fullført)</pre>'}
    html_frag = _leaf_html(leaf.get("kind"), leaf.get("payload"))
    if not html_frag:
        return None
    return {"kind": "html", "html": html_frag}
