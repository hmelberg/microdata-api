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

SAFEPY_DIALECTS = {"python", "pandas", "polars", "r", "duckdb", "he", "r-he",
                   "polars-he", "duckdb-he"}

# Dialects that need optional third-party engine(s) on the server (str or tuple).
_DIALECT_DEPS = {"polars": "polars", "duckdb": "duckdb", "he": "phe",
                 "r-he": "phe", "polars-he": ("polars", "phe"),
                 "duckdb-he": ("duckdb", "phe")}

# Encrypted (format="he") sources force the homomorphic variant of the language:
# a user picks Python/R/polars/duckdb and just points at an encrypted source.
_HE_VARIANT = {"pandas": "he", "he": "he", "r": "r-he", "r-he": "r-he",
               "polars": "polars-he", "polars-he": "polars-he",
               "duckdb": "duckdb-he", "duckdb-he": "duckdb-he"}

_LEVEL_ORDER = {"public": 0, "protected": 1, "sensitive": 2}


def run_extended(script, sources_req, dialect="pandas", on_progress=None):
    """Execute `script` in `dialect` against registry sources via safepy STRICT.

    Levels and locations come from the registry only — never the request. The
    most restrictive source level selects the safepy policy tier.

    ``on_progress`` (optional): called with one client-shaped fragment dict —
    {"kind": "html"|"note", "html": ...} — per released statement while the run
    is still executing. Charts are deferred to the final render (a "note"
    placeholder streams instead). Transport (task_state) is the caller's concern.
    """
    from source_registry import resolve_source, load_dataframe, load_encrypted_source

    # "python" is a meta-dialect: the library (pandas/polars) is chosen by the
    # script itself (a polars import). Resolve to the concrete base dialect before
    # the encryption routing below, so it composes with _HE_VARIANT.
    if dialect == "python":
        import safepy
        dialect = safepy.detect_python_dialect(script)      # "pandas" | "polars"

    frames, level, n_he = {}, "public", 0
    for s in sources_req or []:
        alias, sid = s.get("alias"), s.get("source_id")
        if not alias or not sid:
            return _error_shape(script, "hver kilde må ha 'alias' og 'source_id'")
        try:
            src = resolve_source(sid)
        except KeyError:
            return _error_shape(script, f"ukjent kilde: {sid}")
        # HE sources (format="he", Plane B) never decrypt to a frame; they are
        # carried as EncryptedSource and force the language's homomorphic variant.
        if (src.get("format") or "") == "he":
            if dialect not in _HE_VARIANT:
                return _error_shape(
                    script, f"dialekten '{dialect}' kan ikke analysere krypterte "
                            f"kilder ennå (bruk Python eller R)")
            frames[alias] = load_encrypted_source(src)
            n_he += 1
        else:
            if dialect in ("he", "r-he"):
                return _error_shape(
                    script, f"kilden '{sid}' er ikke kryptert — bruk en vanlig dialekt")
            frames[alias] = load_dataframe(src)
        if _LEVEL_ORDER.get(src["level"], 1) > _LEVEL_ORDER[level]:
            level = src["level"]

    if n_he and n_he != len(frames):
        return _error_shape(
            script, "kan ikke blande krypterte og ukrypterte kilder i én kjøring")

    # encrypted sources switch the run to the homomorphic variant of the language
    effective = _HE_VARIANT.get(dialect, dialect) if n_he else dialect

    deps = _DIALECT_DEPS.get(effective)
    for dep in ((deps,) if isinstance(deps, str) else (deps or ())):
        try:
            __import__(dep)
        except ImportError:
            return _error_shape(
                script, f"dialect '{effective}' er ikke tilgjengelig på serveren "
                        f"(mangler pakken '{dep}')")

    import safepy
    # profile="strict" is mandatory: safepy's own policy would give the
    # protected level the OPEN sandbox (safepy/policy.py), and only the STRICT
    # capability facade is safe-by-construction for user-supplied code.
    cb = None
    if on_progress is not None:
        def cb(res):
            frag = _leaf_fragment(res._leaf())
            if frag is not None:
                on_progress(frag)
    res = safepy.run(script, frames, level=level, profile="strict",
                     dialect=effective, render="plotly", on_result=cb)
    d = res.as_dict()
    out = _to_client_shape(script, d)
    import query_audit
    out["_audit_releases"] = query_audit.collect_fingerprints(d)
    out["_audit_level"] = level
    return out


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


def _leaf_fragment(leaf):
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
