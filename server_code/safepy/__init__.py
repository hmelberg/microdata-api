# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the safepy repo. This file is produced by sync_to_api.py.
# Edit the engine in the safepy repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""safepy — run a familiar subset of Python against private data without ever
revealing individual-level rows.

Posture: a *sandbox* that runs AST-gated user Python directly (the hard,
research path), scoped to public/local data, standalone for now but shaped to
fold into m2py as a Python *frontend* later. Result-side and data-side
protection are delegated to the existing ``protect`` package, not reimplemented.

Public surface:
    run(code, sources, level) -> SafeResult
"""

from .api import detect_python_dialect, run
from .policy import ProtectionLevel
from .result import SafeResult

__all__ = ["run", "detect_python_dialect", "ProtectionLevel", "SafeResult"]
