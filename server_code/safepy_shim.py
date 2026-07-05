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


def run_extended(script, sources_req, dialect="pandas", on_progress=None,
                 source_keys=None):
    """Execute `script` in `dialect` against registry sources via safepy STRICT.

    Levels and locations come from the registry only — never the request. The
    most restrictive source level selects the safepy policy tier.

    ``source_keys`` (optional): {source_id: dekrypteringsnøkkel} for
    kind="encrypted_url"-kilder der eieren ikke lagret nøkkelen (mode 2).
    Brukes kun i minnet; logges aldri (query_audit skrubber script-nøkler,
    og source_keys skrives aldri noe sted).

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
        if source_keys and sid in source_keys:
            src = dict(src, _run_key=str(source_keys[sid]))
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
            try:
                frames[alias] = load_dataframe(src)
            except ValueError as exc:
                # f.eks. manglende/feil dekrypteringsnøkkel eller byttet fil —
                # ren feilmelding uten datainnhold
                return _error_shape(script, str(exc))
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
# SafeResult -> client shape (moved to safepy.client_shape; see there for the
# shared implementation used by both this shim and the browser strict runner)
# ---------------------------------------------------------------------------

from safepy import client_shape as _cs

_error_shape = _cs.error_shape
_to_client_shape = _cs.to_client_shape
_leaf_fragment = _cs.leaf_fragment
