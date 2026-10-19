"""F2 mixed route: kernel-ABI insertion-boundary witness (dennis F2 P2-1).

`executed_backend` telemetry says the compiled backend ran; this proves the compiled
kernel's RESULT is what the mixed route published. See `_f2_backend_witness`.

Non-vacuity: the oracle leg leaves the witness empty, and a substituted equal-valued
column (same bytes, different buffers, which no byte-identity or telemetry assertion can
see) makes the consumption assertion fail.

F-audit seam (bottom of the file, NOT companion-gated): the same route is audited from the
GENERATE side and across the hand-off. The generated parent's key, pooled-faker and categorical
columns must run on their classified Python/numpy backends with zero deterministic-index entries
inside generation (a generation-scoped spy, so the mask half's legitimate compiled calls stay
allowed), and the parent keys the mask half resolves against must be the keys generation made:
the edge's orphan count must equal the count computed from the recorded generated parent. The
mask half's own compute classification is the D-audit's
(`test_phase_d_audit_no_legacy.py`, "the no-legacy COMPUTE-backend audit") and is cited, not
re-derived.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import _pipeline_mixed_route
from decoy_engine.generation import _faker_pool
from tests.parity import _f0_witness as w
from tests.parity._f2_backend_witness import HASH, REDACT, F2BackendWitness
from tests.parity._f2_mixed_support import (
    CHILD,
    NEEDS_COMPANION,
    PARENT,
    SEAM_PARENT_ROWS,
    SEAM_POOL_THRESHOLD,
    assert_outputs_identical,
    child_table,
    chunk_count,
    executed_backends,
    mixed_config,
    oracle,
    orphan_rows,
    run,
    seam_child_keys,
    seam_orphan_count,
    seam_parent_columns,
    warning_signature,
)
from tests.parity._f_audit_witness import DeterministicIndexSpy, audit

pytestmark = [pytest.mark.perf]

KEYS = [str(i % 5) for i in range(30)]  # 4 full 7-row chunks plus a partial


def _job(tmp_path: Path) -> tuple[pa.Table, dict[str, Any]]:
    child = child_table(pa.array(KEYS, pa.string()))
    return child, mixed_config(tmp_path, child)


@NEEDS_COMPANION
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


@NEEDS_COMPANION
def test_the_oracle_leg_leaves_the_witness_empty(tmp_path: Path) -> None:
    child, config = _job(tmp_path)
    witness = F2BackendWitness()
    with witness.observe():
        want = oracle(config, {CHILD: child})
    assert witness.results == {HASH: [], REDACT: []}
    assert witness.masked_chunks == []
    with pytest.raises(AssertionError, match="never called"):
        witness.assert_kernel_result_consumed(HASH, "email", want.outputs[CHILD])


@NEEDS_COMPANION
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


@NEEDS_COMPANION
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


# --------------------------------------------------------------------------------------------
# F-audit: the generate side and the generate -> mask hand-off (runs without the companion)
# --------------------------------------------------------------------------------------------


def _seam_job(tmp_path: Path) -> tuple[list[str], pa.Table, dict[str, Any]]:
    keys = seam_child_keys()
    child = child_table(pa.array(keys, pa.string()))
    config = mixed_config(
        tmp_path,
        child,
        parent_columns=seam_parent_columns(),
        parent_rows=SEAM_PARENT_ROWS,
        policy="warn",
    )
    return keys, child, config


def _record_generated(mp: pytest.MonkeyPatch) -> list[dict[str, pa.Table]]:
    """Record-and-delegate on the route's generate step: what generation really produced."""
    real = _pipeline_mixed_route._generate
    seen: list[dict[str, pa.Table]] = []

    def generate(ctx: Any) -> dict[str, pa.Table]:
        out = real(ctx)
        seen.append(out)
        return out

    mp.setattr(_pipeline_mixed_route, "_generate", generate)
    return seen


def _assert_handoff(result: Any, generated: dict[str, pa.Table], keys: list[str]) -> None:
    """The mask half resolved against the keys generation made, and the parent was published."""
    parent = generated[PARENT]
    expected = seam_orphan_count(set(parent.column("id").to_pylist()), keys)
    assert 0 < expected < len(keys), "the fixture needs both orphan and valid child keys"
    got = orphan_rows(result)
    assert got == expected, f"orphan count {got} differs from the generated parent's {expected}"
    published = result.outputs[PARENT]
    for column in parent.column_names:
        assert published.column(column).to_pylist() == parent.column(column).to_pylist(), column


def test_generate_half_runs_on_python_numpy_with_no_deterministic_index_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", SEAM_POOL_THRESHOLD)
    keys, child, config = _seam_job(tmp_path)
    generated = _record_generated(monkeypatch)
    spy = DeterministicIndexSpy(generation_only=True).install(monkeypatch)
    with audit(monkeypatch, boundary_marking=False) as witness:
        result = run(config, {CHILD: child})
    assert result.quality_metrics["execution"]["execution_mode"] == "mixed"
    assert len(generated) == 1
    reports = witness.classify(generated[0])
    assert {k: r.selection for k, r in reports.items()} == {
        (PARENT, "id"): {w.NO_SELECTION},
        (PARENT, "city"): {w.NUMPY_SELECTION},
        (PARENT, "tier"): {w.PYTHON_RNG},
    }
    assert {(PARENT, "id"), (PARENT, "city"), (PARENT, "tier")} <= witness.core_keys
    spy.assert_generation_never_entered("mixed-route generate half")
    _assert_handoff(result, generated[0], keys)


@pytest.mark.parametrize("mutation", ["renamed", "truncated"])
def test_a_handoff_mutant_that_changes_the_parent_key_set_changes_the_orphan_count(
    mutation: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", SEAM_POOL_THRESHOLD)
    keys, child, config = _seam_job(tmp_path)
    generated = _record_generated(monkeypatch)
    recorded = _pipeline_mixed_route._generate

    def corrupt(ctx: Any) -> dict[str, pa.Table]:
        out = recorded(ctx)  # the recorder keeps the true generated tables
        parent = out[PARENT]
        if mutation == "truncated":
            return {**out, PARENT: parent.slice(0, SEAM_PARENT_ROWS // 2)}
        ids = pa.array(["Q" + v for v in parent.column("id").to_pylist()], pa.string())
        return {**out, PARENT: parent.set_column(parent.schema.get_field_index("id"), "id", ids)}

    monkeypatch.setattr(_pipeline_mixed_route, "_generate", corrupt)
    result = run(config, {CHILD: child})
    with pytest.raises(AssertionError, match="orphan count"):
        _assert_handoff(result, generated[0], keys)


def test_the_mask_half_is_cited_from_the_d_audit_not_rederived() -> None:
    from tests.parity import test_phase_d_audit_no_legacy as d_audit

    assert "no-legacy COMPUTE-backend audit" in (d_audit.__doc__ or "")


@NEEDS_COMPANION
def test_native_kernel_results_hold_on_the_strengthened_seam_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existing mask consumption witness, on the fixture with generated payload and orphans."""
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", SEAM_POOL_THRESHOLD)
    keys, child, config = _seam_job(tmp_path)
    generated = _record_generated(monkeypatch)
    witness = F2BackendWitness()
    with witness.observe():
        got = run(config, {CHILD: child})
    assert executed_backends(got)["email"] == "rust_companion"
    witness.assert_kernel_result_consumed(HASH, "email", got.outputs[CHILD])
    witness.assert_kernel_result_consumed(REDACT, "note", got.outputs[CHILD])
    _assert_handoff(got, generated[0], keys)
