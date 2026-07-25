"""engine-v2 S9 carry-in M1: cross-process determinism for BOTH composites.

S8 shipped in-process reproducibility only; the hard gate (the S7 pattern) is a
subprocess test in a fresh interpreter. The child JSON-dumps the seeded bundle;
the parent generates the same bundle in-process and compares byte-for-byte.
Pattern copied from
`test_mimesis_adapter.py::test_seeded_batch_reproducible_cross_process` (S7) +
`tests/unit/determinism/test_process_stability.py` (S3).
"""

from __future__ import annotations

import json
import subprocess
import sys

import pandas as pd

from decoy_engine.generation.composite import (
    CompositeGenerator,
    composite_city_state_zip,
    composite_name_email,
)
from decoy_engine.providers_v2._adapter import ProviderSpec

_SEED = (0x0123456789).to_bytes(8, "big")
_SEED_EXPR = "(0x0123456789).to_bytes(8,'big')"


def _parent_bundle(gen: CompositeGenerator, cols: tuple[str, ...]) -> dict[str, list[str]]:
    src = pd.Series([f"row{i}" for i in range(32)], dtype=object)
    spec = ProviderSpec(locale="en_US", deterministic=True, namespace="cp", seed=_SEED)
    out = gen.generate_bundle(spec, 32, source=src, deterministic=True)
    return {c: [str(v) for v in out[c]] for c in cols}


def _child_bundle(constructor_expr: str, cols: tuple[str, ...]) -> dict[str, list[str]]:
    script = (
        "import json;import pandas as pd;"
        "from decoy_engine.generation.composite import "
        "composite_city_state_zip,composite_name_email;"
        "from decoy_engine.providers_v2._adapter import ProviderSpec;"
        f"s=ProviderSpec(locale='en_US',deterministic=True,namespace='cp',seed={_SEED_EXPR});"
        "src=pd.Series(['row%d'%i for i in range(32)],dtype=object);"
        f"out={constructor_expr}.generate_bundle(s,32,source=src,deterministic=True);"
        f"print(json.dumps({{c:[str(v) for v in out[c]] for c in {cols!r}}}))"
    )
    result = subprocess.run(  # noqa: S603 -- args are test literals, not untrusted input
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout.strip())  # type: ignore[no-any-return]


class TestCompositeCrossProcess:
    def test_city_state_zip_byte_identical_cross_process(self) -> None:
        cols = ("city", "state", "zip")
        child = _child_bundle("composite_city_state_zip(coherent_namespace='cp')", cols)
        parent = _parent_bundle(composite_city_state_zip(coherent_namespace="cp"), cols)
        assert child == parent

    def test_name_email_byte_identical_cross_process(self) -> None:
        cols = ("first_name", "last_name", "email")
        child = _child_bundle("composite_name_email(coherent_namespace='cp',pool_size=500)", cols)
        parent = _parent_bundle(composite_name_email(coherent_namespace="cp", pool_size=500), cols)
        assert child == parent
