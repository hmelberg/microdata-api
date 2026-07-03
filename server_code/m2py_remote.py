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


# Plot verbs whose plotly JSON embeds row-level values (scatter/box points,
# per-row sankey transitions) — refused on non-public data. histogram is NOT
# here: under an active release spec the op pre-bins server-side and releases
# suppressed bin counts only.
_RAW_PLOT_VERBS = ("scatter", "hexbin", "sankey", "boxplot")


def _raw_plot_verbs_used(script):
    used = []
    for line in script.splitlines():
        stripped = line.split("//", 1)[0].strip()
        if not stripped:
            continue
        cmd = stripped.split()[0].lower()
        if cmd in _RAW_PLOT_VERBS and cmd not in used:
            used.append(cmd)
    return used


def run_remote(script, *, datasets, backend="pandas", policy=None, raw=False):
    level = (policy or {}).get("level", "public")
    if level != "public":
        raw = False   # print_results echoes raw result objects to stdout
        bad = _raw_plot_verbs_used(script)
        if bad:
            return {"code": "", "out": "", "html": "", "n": None,
                    "err": ("Personvern: " + ", ".join(bad) + " viser "
                            "enkeltobservasjoner og er ikke tilgjengelig for "
                            "beskyttede data. Bruk aggregerte diagrammer "
                            "(f.eks. barchart) i stedet."),
                    "figs": [], "results": [], "datasetInfo": {}}
    code = _mt.translate(script, backend=backend, source_path=None,
                         allow_emulated=False, print_results=raw)
    datasets = dict(datasets)
    pre = (policy or {}).get("pre_recipe")
    if pre and pre.get("profile"):
        # Sensitive data: input-side protection recipe applied to every frame
        # BEFORE any script code sees it (microdata_no = Tiltak-1 population
        # floor + winsorize numerics). A failed floor is a clean refusal.
        try:
            import pandas as _pd
            import protect as _p
            from m2py import _get_df_key_col
            treated = {}
            for k, v in datasets.items():
                unit = _get_df_key_col(v)
                vv = v if unit else v.assign(_unit_tmp=range(len(v)))
                unit = unit or "_unit_tmp"
                wins = [c for c in vv.columns
                        if c != unit and _pd.api.types.is_numeric_dtype(vv[c])]
                vv, _log = _p.profile(vv, pre["profile"], unit_id=unit,
                                      winsorize_cols=wins)
                treated[k] = vv.drop(columns=["_unit_tmp"], errors="ignore")
            datasets = treated
        except Exception as exc:
            return {"code": "", "out": "", "html": "", "n": None,
                    "err": "Personvern (inndata-tiltak): " + str(exc),
                    "figs": [], "results": [], "datasetInfo": {}}
    ns = {"datasets": datasets}
    buf = io.StringIO()
    err = None
    from m2py_runtime import pandas_ops as _ops
    _ops.set_release_spec((policy or {}).get("post_suppress"))
    try:
        with contextlib.redirect_stdout(buf):
            exec(code, ns)
    except Exception as exc:
        err = repr(exc)
    finally:
        _ops.set_release_spec(None)

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
    if df is not None and level == "public":
        # The head(50) preview is raw rows — public data only. Non-public runs
        # get schema/row count via datasetInfo/n instead.
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
