# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the safepy repo. This file is produced by sync_to_api.py.
# Edit the engine in the safepy repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
# safepy/assembly.py
"""Variable-level assembly executor (Project A, spec
m2py/docs/superpowers/specs/2026-07-05-variable-level-assembly-design.md).

Pure pandas: turns a mode-neutral AssemblySpec + a resolver(source_alias)->
DataFrame into named DataFrames. Trusted code (never the safepy facade); the
same file runs in the browser (Pyodide) and on the server (shim), vendored to
both by m2py/sync_to_api.py, so local and remote assembly cannot diverge.

Assembly is structure only: whole-table load, column select, single-key
equi-join (left default). Rows/derivation/aggregation are the analysis
script's job.
"""
from __future__ import annotations


class AssemblyError(ValueError):
    """Structural problem in an assembly spec (Norwegian message)."""


_VALID_HOW = {"left", "inner", "outer"}


def referenced_sources(spec: dict) -> list[str]:
    return list(spec.get("sources") or [])


def _check_key(df, key, where):
    if key not in df.columns:
        raise AssemblyError(f"{where} mangler nøkkelkolonnen «{key}»")


def build_datasets(spec: dict, resolver):
    """spec + resolver(alias)->DataFrame -> ({name: DataFrame}, [notes])."""
    datasets: dict = {}
    notes: list = []

    # Two passes so a `join` may reference a whole-table `load` dataset
    # regardless of declaration order: build the dependency-free `load`
    # datasets first, then the assembled `steps` datasets in written order.
    all_ds = spec.get("datasets") or []
    ordered = ([d for d in all_ds if "load" in d]
               + [d for d in all_ds if "load" not in d])

    for ds in ordered:
        name = ds["name"]
        if "load" in ds:
            datasets[name] = resolver(ds["load"])
            continue

        key = ds.get("key")
        if not key:
            raise AssemblyError(f"datasettet «{name}» mangler key(...)")
        acc = None                                   # accumulator, built by steps

        for step in ds.get("steps") or []:
            how = step.get("how", "left")
            if how not in _VALID_HOW:
                raise AssemblyError(f"ukjent join-type «{how}»")

            if step["op"] == "import":
                src = resolver(step["source"])
                _check_key(src, key, f"kilden «{step['source']}»")
                missing = [c for c in step["columns"] if c not in src.columns]
                if missing:
                    raise AssemblyError(
                        f"kolonnen «{missing[0]}» finnes ikke i kilden "
                        f"«{step['source']}» (har: {', '.join(map(str, src.columns))})")
                # the key is always carried; drop it from the value columns if
                # the caller also listed it (import p/pid — avoids a dup select)
                _cols = [c for c in step["columns"] if c != key]
                piece = src[[key] + _cols].copy()
                if acc is None:
                    acc = piece                       # first import establishes rows
                else:
                    _merge_check(acc, piece, key, name, step["source"])
                    before = len(acc)
                    acc = acc.merge(piece, on=key, how=how)
                    _note_multiplication(notes, name, before, len(acc))

            elif step["op"] == "join":
                on = step["on"]
                other = datasets.get(step["from"])
                if other is None:
                    raise AssemblyError(f"ukjent datasett «{step['from']}» "
                                        f"(join into «{name}» — feil rekkefølge?)")
                if acc is None:
                    raise AssemblyError(f"«{name}» er tomt — importer variabler "
                                        f"før join")
                _check_key(acc, on, f"«{name}»")
                _check_key(other, on, f"«{step['from']}»")
                _merge_check(acc, other, on, name, step["from"])
                before = len(acc)
                acc = acc.merge(other, on=on, how=how)
                _note_multiplication(notes, name, before, len(acc))
            else:
                raise AssemblyError(f"ukjent monterings-operasjon «{step['op']}»")

        datasets[name] = acc if acc is not None else _empty()
    return datasets, notes


def _merge_check(a, b, key, name, other_name):
    if a[key].dtype != b[key].dtype:
        raise AssemblyError(
            f"nøkkelen «{key}» har ulik type i «{name}» og «{other_name}»")


def _note_multiplication(notes, name, before, after):
    if after != before:
        notes.append(f"{name}: {before} → {after} rader etter join")


def _empty():
    import pandas as pd
    return pd.DataFrame()
