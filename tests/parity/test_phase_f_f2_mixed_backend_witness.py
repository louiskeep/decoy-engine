"""F2 mixed route: kernel-ABI insertion-boundary witness (dennis F2 P2-1).

`executed_backend` telemetry says the compiled backend ran; this proves the compiled
kernel's RESULT is what the mixed route published. See `_f2_backend_witness`.

Non-vacuity: the oracle leg leaves the witness empty, and a substituted equal-valued
column (same bytes, different buffers, which no byte-identity or telemetry assertion can
see) makes the consumption assertion fail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from tests.parity._f2_backend_witness import HASH, REDACT, F2BackendWitness
from tests.parity._f2_mixed_support import (
    CHILD,
    NEEDS_COMPANION,
    assert_outputs_identical,
    child_table,
    chunk_count,
    executed_backends,
    mixed_config,
    oracle,
    run,
    warning_signature,
)

pytestmark = [pytest.mark.perf, NEEDS_COMPANION]

KEYS = [str(i % 5) for i in range(30)]  # 4 full 7-row chunks plus a partial


def _job(tmp_path: Path) -> tuple[pa.Table, dict[str, Any]]:
    child = child_table(pa.array(KEYS, pa.string()))
    return child, mixed_config(tmp_path, child)


def test_native_kernel_results_are_the_published_columns(tmp_path: Path) -> None:
    child, config = _job(tmp_path)
    want = oracle(config, {CHILD: child})
    witness = F2BackendWitness()
    with witness.observe():
        got = run(config, {CHILD: child})

    # Route + telemetry (belt) and byte identity to the oracle.
    assert got.quality_metrics["execution"]["execution_mode"] == "mixed"
    assert chunk_count(got) >= 2
    assert executed_backends(got)["email"] == "rust_companion"
    assert_outputs_identical(got.outputs, want.outputs)
    assert warning_signature(got) == warning_signature(want)

    # Suspenders: the compiled kernel's own result is what was consumed.
    witness.assert_kernel_result_consumed(HASH, "email", got.outputs[CHILD])
    witness.assert_kernel_result_consumed(REDACT, "note", got.outputs[CHILD])
    assert len(witness.masked_chunks) == chunk_count(got)


def test_the_oracle_leg_leaves_the_witness_empty(tmp_path: Path) -> None:
    child, config = _job(tmp_path)
    witness = F2BackendWitness()
    with witness.observe():
        want = oracle(config, {CHILD: child})
    assert witness.results == {HASH: [], REDACT: []}
    assert witness.masked_chunks == []
    with pytest.raises(AssertionError, match="never called"):
        witness.assert_kernel_result_consumed(HASH, "email", want.outputs[CHILD])


@pytest.mark.parametrize("kind,column", [(HASH, "email"), (REDACT, "note")])
def test_an_equal_valued_substitute_is_caught(tmp_path: Path, kind: str, column: str) -> None:
    """The kernel runs and the output bytes match the oracle, but the column the route
    consumed is a copy computed elsewhere. Telemetry and byte identity pass; the
    insertion check must fail."""
    child, config = _job(tmp_path)
    want = oracle(config, {CHILD: child})
    witness = F2BackendWitness(substitute_equal_copy=True)
    with witness.observe():
        got = run(config, {CHILD: child})
    assert executed_backends(got)["email"] == "rust_companion"
    assert_outputs_identical(got.outputs, want.outputs)
    assert witness.results[kind], "the kernel boundary was reached"
    with pytest.raises(AssertionError, match="discarded"):
        witness.assert_kernel_result_consumed(kind, column, got.outputs[CHILD])


def test_a_committed_column_that_differs_from_the_kernel_result_is_caught(
    tmp_path: Path,
) -> None:
    child, config = _job(tmp_path)
    witness = F2BackendWitness()
    with witness.observe():
        got = run(config, {CHILD: child})
    tampered = got.outputs[CHILD].column("email").combine_chunks().to_pylist()
    tampered[0] = "tampered"
    with pytest.raises(AssertionError, match="do not equal the committed"):
        witness.assert_kernel_result_consumed(
            HASH,
            "email",
            pa.array(tampered, pa.string()),
        )
