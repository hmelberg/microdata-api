"""Thin wrapper over the existing m2py.py asset.

m2py.py is shipped as a server module inside the Anvil app (copied from
C:\\Users\\hansm\\m2py\\m2py.py). This shim exposes just the pieces the API
needs, with lazy imports so that cold-start only pays for MicroParser.
MockDataEngine and StatsEngine (which pull pandas/numpy) only load when
deep_validate=True is requested.

In the Anvil app repository, place m2py.py at server_code/m2py.py next to
this file so `import m2py` resolves inside server modules.
"""

from __future__ import annotations

# Eager: MicroParser is small, pure-Python; we want it ready for every request.
from m2py import MicroParser  # type: ignore  # noqa: F401


_mock_engine = None
_stats_engine = None


def get_mock_engine():
    """Lazy-load MockDataEngine. Only called when deep_validate=True."""
    global _mock_engine
    if _mock_engine is None:
        from m2py import MockDataEngine  # type: ignore

        _mock_engine = MockDataEngine
    return _mock_engine


def get_stats_engine():
    """Lazy-load StatsEngine. Only called when deep_validate=True."""
    global _stats_engine
    if _stats_engine is None:
        from m2py import StatsEngine  # type: ignore

        _stats_engine = StatsEngine
    return _stats_engine


def make_parser() -> MicroParser:
    return MicroParser()
