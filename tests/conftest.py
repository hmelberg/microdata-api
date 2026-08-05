import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server_code"))

# Anvil-editoren INJISERER `import anvil.microsoft.auth` øverst i alle
# servermoduler når Microsoft-auth er på (auto-commiten 2026-08-05 a9e2a13
# brakk pytest-importen av alle rene moduler). Stub KUN denne kjeden —
# anvil.server/anvil.tables skal fortsatt feile, så _ANVIL-guardene i
# keystore/userdoc forblir False i testkjøring.
if "anvil" not in sys.modules:
    _anvil = types.ModuleType("anvil")
    _anvil.__path__ = []
    _ms = types.ModuleType("anvil.microsoft")
    _ms.__path__ = []
    _msauth = types.ModuleType("anvil.microsoft.auth")
    _anvil.microsoft = _ms
    _ms.auth = _msauth
    sys.modules["anvil"] = _anvil
    sys.modules["anvil.microsoft"] = _ms
    sys.modules["anvil.microsoft.auth"] = _msauth
