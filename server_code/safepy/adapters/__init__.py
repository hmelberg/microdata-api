# ============================================================================
# GENERATED COPY — DO NOT EDIT HERE.
# Source of truth: the safepy repo. This file is produced by sync_to_api.py.
# Edit the engine in the safepy repo and re-run that script; direct edits here
# are overwritten on the next sync.
# ============================================================================
"""Adapter registry. Importing this package registers the built-in adapters."""

from . import base  # noqa: F401
from . import pandas_adapter  # noqa: F401  (registers PandasAdapter on import)
from . import safeframe_adapter  # noqa: F401  (registers SafeFrameAdapter on import)

find = base.find
register = base.register
