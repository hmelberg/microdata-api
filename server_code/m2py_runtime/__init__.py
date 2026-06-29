# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the m2py repo. This file is produced by sync_to_api.py.
# Edit the engine in the m2py repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""Pure, side-effect-free runtime ops shared by the emulator and the translators.

Ops are deterministic ``frame -> frame``. No logging, no disclosure control, no
``self``. The pandas ops reuse the emulator's own expression evaluator
(``m2py._py_eval_expr``/``_py_eval_cond``) so they match the in-browser emulator
exactly. The polars ops (in :mod:`m2py_runtime.polars_ops`) import polars lazily
so this package stays importable under Pyodide, where polars is unavailable.

This package is the foundation for offline code generation: ``m2py_translate``
walks the parsed microdata IR and emits a script of thin calls to these ops, in
either the pandas backend (eager ``pd.DataFrame``) or the polars backend (lazy
``pl.LazyFrame``, streaming-capable for larger-than-memory data).
"""

from . import pandas_ops  # noqa: F401

__all__ = ["pandas_ops"]
