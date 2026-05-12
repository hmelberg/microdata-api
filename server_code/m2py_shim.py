"""Thin wrapper over the existing m2py.py asset.

m2py.py is shipped as a server module inside the Anvil app (copied from
C:\\Users\\hansm\\m2py\\m2py.py). This shim exposes just the pieces the API
needs, with lazy imports so that cold-start only pays for MicroParser.
MicroInterpreter (which pulls pandas/numpy through MockDataEngine and
StatsEngine) only loads when deep_validate=True is requested.

In the Anvil app repository, place m2py.py at server_code/m2py.py next to
this file so `import m2py` resolves inside server modules.
"""

from __future__ import annotations

# Eager: MicroParser is small, pure-Python; we want it ready for every request.
from m2py import MicroParser  # type: ignore  # noqa: F401


_interpreter_cls = None


def get_interpreter_cls():
    """Lazy-load MicroInterpreter. Only called when deep_validate=True.

    MicroInterpreter is the full execution engine — it internally
    constructs a MockDataEngine + StatsEngine + the handler suite and
    exposes run_script(text) which preprocesses and executes every line
    against synthetic data. That's what we use to catch runtime errors
    (merge direction, entity-type mixing, missing date, etc.) the static
    validator can't see.
    """
    global _interpreter_cls
    if _interpreter_cls is None:
        from m2py import MicroInterpreter  # type: ignore

        _interpreter_cls = MicroInterpreter
    return _interpreter_cls


def make_parser() -> MicroParser:
    return MicroParser()


# ─── Høynivå-hjelpere brukt av /translate og /run ────────────────────────────

def translate(script: str) -> str:
    """Oversett microdata-script til ekvivalent Python uten å kjøre.

    Bruker MicroInterpreter.translate_script_to_python — emittering, ikke
    eksekvering, så pandas/numpy lastes ikke før noen ber om kjøring.
    """
    InterpreterCls = get_interpreter_cls()
    interp = InterpreterCls(echo_commands=False)
    return interp.translate_script_to_python(script)


def run_with_summary(script: str, max_rows: int | None = None,
                     echo_commands: bool = True) -> dict:
    """Kjør scriptet mot MockDataEngine og returner output + dataset-info.

    Returnerer en dict:
        {
          "output": str,           # `output_log` joinet med newline
          "datasets": [            # ett oppslag per datasett etterpå
            {"name": str, "n_rows": int, "columns": [str, ...]},
            ...
          ],
          "error": Optional[str],  # exception-melding hvis kjøringen brøt
        }

    `max_rows` overstyrer MockDataEngine.default_rows (syntetisk data).
    Ingen ekte microdata-data blir berørt; serveren har kun mock-data.
    """
    InterpreterCls = get_interpreter_cls()
    interp = InterpreterCls(echo_commands=echo_commands)
    if max_rows is not None and hasattr(interp, "data_engine"):
        try:
            interp.data_engine.default_rows = int(max_rows)
        except Exception:
            pass
    error: str | None = None
    try:
        interp.run_script(script)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    output = "\n".join(getattr(interp, "output_log", []) or [])
    datasets: list[dict] = []
    for name, df in getattr(interp, "datasets", {}).items():
        try:
            datasets.append({
                "name": str(name),
                "n_rows": int(len(df)),
                "columns": [str(c) for c in df.columns],
            })
        except Exception:
            continue
    return {"output": output, "datasets": datasets, "error": error}
