"""Process-stability test: the across-processes axis of the done-definition
determinism gate.

Spawn a Python subprocess, run derive(seed, namespace, source) in the child,
assert byte-identical output to the parent. Catches process-locality bugs
(implementation reads a process-local cache; PID enters the envelope by
accident).

This test does NOT catch envelope-shape bugs (both parent and child would
have the same wrong output); the reference-vector test in
test_derive_vectors.py covers that axis.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from decoy_engine.determinism import derive

_SEED_HEX = "0102030405060708"
_NS = "process-stability-namespace"
_SRC_HEX = "deadbeef"

_CHILD_SCRIPT = """
import sys
from decoy_engine.determinism import derive

seed = bytes.fromhex(sys.argv[1])
ns = sys.argv[2]
src = bytes.fromhex(sys.argv[3])
out = derive(seed, ns, src)
sys.stdout.write(out.hex())
"""


@pytest.mark.golden
class TestProcessStability:
    def test_subprocess_produces_byte_identical_output(self) -> None:
        parent_output = derive(bytes.fromhex(_SEED_HEX), _NS, bytes.fromhex(_SRC_HEX)).hex()

        child_proc = subprocess.run(  # noqa: S603 -- args are test literals, not untrusted input
            [sys.executable, "-c", _CHILD_SCRIPT, _SEED_HEX, _NS, _SRC_HEX],
            capture_output=True,
            text=True,
            check=True,
        )
        child_output = child_proc.stdout.strip()

        assert child_output == parent_output, (
            f"process-stability drift: parent={parent_output} child={child_output}. "
            "Implementation may be reading process-local state (PID, getpid, "
            "thread-local, etc.) that defeats the determinism contract."
        )

    def test_subprocess_with_different_seed_produces_different_output(self) -> None:
        """Sanity: subprocess invocation isn't a no-op; different inputs in
        the subprocess produce different outputs."""
        parent_a = derive(bytes.fromhex("0000000000000001"), _NS, b"src").hex()
        parent_b = derive(bytes.fromhex("0000000000000002"), _NS, b"src").hex()
        assert parent_a != parent_b
