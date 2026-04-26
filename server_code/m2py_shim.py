import anvil.email
import anvil.users
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
