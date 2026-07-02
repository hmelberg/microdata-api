# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the m2py repo. This file is produced by sync_to_api.py.
# Edit the engine in the m2py repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""SafeStat remote compute core (pure CPython).

Mirrors the client's in-Pyodide run path (index.html ~7536-7603) on the server:
translate the microdata script, exec it against provided REAL data, collect the
result_* / fig_* objects, apply result-side suppression per the protection
policy, and serialize to the JSON shape the SafeStat client renderer consumes.
The emulator is NOT used here — `datasets` carries real data the caller fetched.
"""
from __future__ import annotations

import contextlib
import io

import m2py_translate as _mt
from m2py_protection import PandasProtect
from m2py_runtime.sources import read_source
from m2py_protection import resolve_policy


def _trivial_index(df) -> bool:
    """True when the index is a plain 0..n-1 counter carrying no information
    (fresh/reset frames) — hide it from output; keep real indexes (group keys)."""
    try:
        idx = df.index
        return (getattr(idx, "name", None) is None
                and list(idx) == list(range(len(df))))
    except Exception:
        return False


def _render_result(r):
    if hasattr(r, "to_html"):
        return r.to_html(border=0, classes="output-table",
                         index=not _trivial_index(r))
    if hasattr(r, "summary"):
        return "<pre>" + str(r.summary()) + "</pre>"
    return "<pre>" + str(r) + "</pre>"


def _dataset_info(ns):
    """Sidebar metadata for each named working frame ``df_<name>`` in the
    namespace: ``{name: {columns, dtypes, nrows}}``. Schema + row-count only —
    no row-level data — so it is safe to return for a remote (server-held) run.
    """
    info = {}
    for k, v in ns.items():
        if not k.startswith("df_") or not hasattr(v, "columns"):
            continue
        try:
            info[k[3:]] = {
                "columns": [str(c) for c in v.columns],
                "dtypes": {str(c): str(v[c].dtype) for c in v.columns},
                "nrows": int(len(v)),
            }
        except Exception:
            pass
    return info


def run_remote(script, *, datasets, backend="pandas", policy=None, raw=False):
    code = _mt.translate(script, backend=backend, source_path=None,
                         allow_emulated=False, print_results=raw)
    ns = {"datasets": dict(datasets)}
    buf = io.StringIO()
    err = None
    try:
        with contextlib.redirect_stdout(buf):
            exec(code, ns)
    except Exception as exc:
        err = repr(exc)

    adapter = PandasProtect()
    spec = (policy or {}).get("post_suppress")

    figs = []
    for k in sorted(ns):
        if k.startswith("fig_"):
            try:
                figs.append(ns[k].to_json())
            except Exception:
                pass

    results = []
    for k in sorted(ns):
        if k.startswith("result_"):
            results.append(_render_result(adapter.suppress(ns[k], spec)))

    df = ns.get("df")  # translator footer materializes the final active frame as `df`
    html = ""
    if df is not None:
        try:
            html = df.head(50).to_html(border=0, index=not _trivial_index(df))
        except Exception:
            html = "<pre>" + str(df)[:5000] + "</pre>"

    return {"code": code, "out": buf.getvalue(), "html": html,
            "n": (None if df is None else int(len(df))),
            "err": err, "figs": figs, "results": results,
            "datasetInfo": _dataset_info(ns)}


def run_remote_from_sources(script, sources, *, backend="pandas", raw=False):
    """Fetch each registered source into a DataFrame, resolve the protection
    policy (most-restrictive across sources), and run the script.

    `sources` is a list of {"alias", "location", "level"}; `alias` is the
    dataset name the script loads. Real data only — the emulator is not used.
    """
    datasets = {s["alias"]: read_source(s["location"]) for s in sources}
    policy = resolve_policy([s.get("level", "public") for s in sources])
    return run_remote(script, datasets=datasets, backend=backend,
                      policy=policy, raw=raw)
