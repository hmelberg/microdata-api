# Pytest-collection shim: tests/__init__.py makes pytest's Package collector
# import the repo-root __init__.py as a bare module named "__init__" (the
# hyphenated repo dir name prevents a dotted package name), where __path__ is
# not pre-populated and the Anvil bootstrap line would crash. Pre-registering
# a stub intercepts that import. The real __init__.py is untouched and behaves
# normally on Anvil's runtime.
import sys
import types

if "__init__" not in sys.modules:
    _stub = types.ModuleType("__init__")
    _stub.__path__ = []
    sys.modules["__init__"] = _stub
