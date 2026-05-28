"""engine-v2 S7: optional-dependency handling + default-registry shape.

These tests do NOT import `mimesis` at module load, so they run whether or not
the optional dep is installed. They assert the contract the engine must hold
in BOTH states: the default registry is 24 providers with zero Mimesis
adopted, and a direct import of the mimesis package without the dep raises the
documented install message.
"""

from __future__ import annotations

import json
import subprocess
import sys

from decoy_engine.providers_v2 import get_default_registry


class TestDefaultRegistryShape:
    def test_default_registry_has_24_providers(self) -> None:
        assert len(get_default_registry().known_providers()) == 24

    def test_no_provider_is_mimesis_bound_by_default(self) -> None:
        reg = get_default_registry()
        backends = {reg.get_capabilities(p).backend_type for p in reg.known_providers()}
        assert "mimesis" not in backends
        assert backends == {"faker", "decoy_native"}


# Subprocess that simulates Mimesis being absent via a meta-path finder that
# raises ModuleNotFoundError for `mimesis`, then checks (a) importing the
# mimesis package raises the documented install message and (b) the default
# registry still builds to 24 providers without importing the mimesis package.
_ABSENT_SCRIPT = """
import sys, json

class _BlockMimesis:
    def find_spec(self, name, path=None, target=None):
        if name == "mimesis" or name.startswith("mimesis."):
            raise ModuleNotFoundError("No module named 'mimesis' (blocked for test)")
        return None

sys.meta_path.insert(0, _BlockMimesis())
sys.modules.pop("mimesis", None)

result = {}
try:
    import decoy_engine.providers_v2.mimesis  # noqa: F401
    result["import_raised"] = False
    result["has_install_msg"] = False
except ImportError as exc:
    result["import_raised"] = True
    result["has_install_msg"] = "decoy-engine[mimesis]" in str(exc)

from decoy_engine.providers_v2 import get_default_registry
result["registry_size"] = len(get_default_registry().known_providers())
print(json.dumps(result))
"""


class TestMimesisAbsent:
    def test_absent_behavior_in_subprocess(self) -> None:
        proc = subprocess.run(  # noqa: S603 -- args are test literals, not untrusted input
            [sys.executable, "-c", _ABSENT_SCRIPT],
            capture_output=True,
            text=True,
            check=True,
        )
        result = json.loads(proc.stdout.strip())
        assert result["import_raised"] is True, "mimesis package import should raise when absent"
        assert result["has_install_msg"] is True, "ImportError should name the [mimesis] extra"
        assert result["registry_size"] == 24, "registry stays 24 when mimesis is absent"
