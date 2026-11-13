"""PS-8 review rulings on the post-scan gate: quarantine, a small budget, null-typed keys, a mixed
generated and masked parent, and `integrity: true` withholding a run. All values are made up."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_pipeline
from decoy_engine.execution.pipeline import _post_scan
from decoy_engine.execution.pipeline._post_scan import PostScanGate
from decoy_engine.verification.postmask import SCHEMA_VERSION, PostScanBlockedError
from tests.unit.execution import _subset_run_support as subset_sup
from tests.unit.execution._b6a_support import RecordingSink
from tests.unit.execution.post_scan.test_wiring import _blocked, _config, _run
from tests.unit.execution.test_post_scan_integrity_in_run import _cfg

REL = {
    "parent": {"table": "p", "columns": ["id"]},
    "children": [{"table": "c", "columns": ["pid"]}],
    "orphan_policy": "preserve",
}
QUARANTINED = "post_scan.foreign_key_orphans_quarantined"
ORPHANS = "post_scan.foreign_key_orphans_introduced"
KINDS = {"p": "mask", "c": "mask"}


@pytest.fixture(autouse=True)
def _subset_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(subset_sup.VAR, "ab" * 32)


def _gate(rels: list[dict[str, Any]] | None = None) -> PostScanGate:
    return PostScanGate({"relationships": rels or [REL]})


def _warned(gate: PostScanGate) -> set[str]:
    assert gate.report is not None
    return {f"{c['namespace']}.{c['code']}" for c in gate.report["verdict"]["warnings"]}


# ----- B4: parent rows quarantine removed -------------------------------------------------------


def _quarantined_run(child_out: list[str], source_child: list[str] | None = None) -> PostScanGate:
    gate = _gate()
    before = pa.table({"id": ["a", "b", "c"]})
    gate.note_quarantined("p", before, {2})
    source = {"p": before, "c": pa.table({"pid": source_child or ["a", "b", "c"]})}
    outputs = {"p": pa.table({"id": ["a", "b"]}), "c": pa.table({"pid": child_out})}
    gate.run_resident(outputs, source, KINDS)
    return gate


def test_children_of_a_quarantined_parent_warn_and_do_not_block() -> None:
    gate = _quarantined_run(["a", "b", "c", "c"])
    assert gate.verdict is not None and not gate.verdict.blocking
    assert _warned(gate) == {QUARANTINED}
    (child,) = (k for k in gate.report["keys"] if k["kind"] == "foreign")
    assert child["output"]["orphan_rows"] == 0  # only orphans the run introduced are counted


def test_an_orphan_that_quarantine_did_not_cause_still_blocks() -> None:
    exc = _blocked(lambda: _quarantined_run(["a", "b", "c", "zzz"]))
    assert exc.reason == ORPHANS


def test_without_quarantine_the_same_orphans_block() -> None:
    gate = _gate()
    source = {"p": pa.table({"id": ["a", "b", "c"]}), "c": pa.table({"pid": ["a", "b", "c"]})}
    outputs = {"p": pa.table({"id": ["a", "b"]}), "c": pa.table({"pid": ["a", "b", "c"]})}
    assert _blocked(lambda: gate.run_resident(outputs, source, KINDS)).reason == ORPHANS


def test_a_removed_key_that_is_not_a_declared_parent_is_not_recorded() -> None:
    gate = _gate()
    gate.note_quarantined("c", pa.table({"pid": ["a"]}), {0})
    gate.note_quarantined("p", pa.table({"other": ["a"]}), {0})
    assert gate._quarantined == {}


# ----- B2: a small budget does not starve the key scan ------------------------------------------


def test_a_64mb_budget_no_sink_run_passes_the_gate(tmp_path: Path) -> None:
    n = 100_000
    parent = pa.table({"id": [f"p{i}" for i in range(n)], "note": ["x"] * n})
    child = pa.table(
        {"cid": [f"c{i}" for i in range(1_000)], "parent_id": [f"p{i}" for i in range(1_000)]}
    )
    result, _ = _run(
        _config(tmp_path),
        "full_frame",
        sources={"parent": parent, "child": child},
        out_of_core_budget_bytes=64 * 1024 * 1024,
    )
    assert result.quality_metrics["post_scan_integrity"]["verdict"]["blocking"] is False


# ----- B3: a null-typed key column compares with every family -----------------------------------

GENERATED = {
    "parent": {"table": "people", "columns": ["id"]},
    "children": [{"table": "accounts", "columns": ["person_id"]}],
    "orphan_policy": "preserve",
}
GEN_KINDS = {"people": "generate", "accounts": "mask"}


def test_a_zero_row_null_typed_generated_parent_does_not_block() -> None:
    gate = _gate([GENERATED])
    gate.run_resident(
        {
            "people": pa.table({"id": pa.array([], pa.null())}),
            "accounts": pa.table({"person_id": ["a", "b"]}),
        },
        {"accounts": pa.table({"person_id": ["a", "b"]})},
        GEN_KINDS,
    )
    assert gate.verdict is not None
    assert "post_scan.key_type_mismatch" not in {
        f"{c['namespace']}.{c['code']}" for c in gate.report["verdict"]["codes"]
    }


@pytest.mark.parametrize("family", [pa.int64(), pa.string()])
def test_a_zero_row_null_typed_child_matches_either_key_family(family: pa.DataType) -> None:
    gate = _gate()
    values = pa.array([1, 2], family) if family == pa.int64() else pa.array(["a", "b"], family)
    source = {"p": pa.table({"id": values}), "c": pa.table({"pid": values})}
    outputs = {"p": pa.table({"id": values}), "c": pa.table({"pid": pa.array([], pa.null())})}
    gate.run_resident(outputs, source, KINDS)
    assert gate.verdict is not None and not gate.verdict.blocking


def test_a_fully_quarantined_table_is_not_a_type_mismatch() -> None:
    gate = _gate()
    before = pa.table({"id": ["a", "b"]})
    gate.note_quarantined("p", before, {0, 1})
    source = {"p": before, "c": pa.table({"pid": ["a", "b"]})}
    outputs = {"p": pa.table({"id": pa.array([], pa.null())}), "c": pa.table({"pid": ["a", "b"]})}
    gate.run_resident(outputs, source, KINDS)
    assert gate.verdict is not None and not gate.verdict.blocking
    assert _warned(gate) == {QUARANTINED}


def test_a_key_pair_with_no_exact_cast_defers_the_gate_and_does_not_block() -> None:
    gate = _gate()
    source = {"p": pa.table({"id": ["a"]}), "c": pa.table({"pid": ["a"]})}
    outputs = {"p": pa.table({"id": ["a"]}), "c": pa.table({"pid": [[1]]})}
    gate.run_resident(outputs, source, KINDS)
    assert gate.deferred and gate.verdict is None


# ----- H1: one generated and one masked parent --------------------------------------------------

TWO_PARENTS = [
    {
        "parent": {"table": "gen", "columns": ["id"]},
        "children": [{"table": "c", "columns": ["pid"]}],
        "orphan_policy": "preserve",
    },
    {
        "parent": {"table": "m", "columns": ["id"]},
        "children": [{"table": "c", "columns": ["pid"]}],
        "orphan_policy": "preserve",
    },
]
TWO_KINDS = {"gen": "generate", "m": "mask", "c": "mask"}


def _two_parent_run(child_out: list[str]) -> PostScanGate:
    gate = _gate(TWO_PARENTS)
    outputs = {
        "gen": pa.table({"id": ["g0", "g1"]}),
        "m": pa.table({"id": ["x0", "x1", "x2"]}),
        "c": pa.table({"pid": child_out}),
    }
    sources = {
        "m": pa.table({"id": ["m0", "m1", "m2"]}),
        "c": pa.table({"pid": ["m0", "m1", "m2"]}),
    }
    gate.run_resident(outputs, sources, TWO_KINDS)
    return gate


def test_p1_a_mask_that_breaks_every_child_key_blocks_beside_a_generated_parent() -> None:
    exc = _blocked(lambda: _two_parent_run(["y0", "y1", "y2"]))
    assert exc.reason == ORPHANS


def test_a_child_that_still_resolves_against_the_masked_parent_passes() -> None:
    gate = _two_parent_run(["x0", "x1", "x2"])
    assert gate.verdict is not None and not gate.verdict.blocking


# ----- L5: an inherited type mismatch is reported as one ----------------------------------------


def test_an_inherited_type_mismatch_still_blocks_and_is_labelled_as_one() -> None:
    gate = _gate([GENERATED])
    # A list key has no cast to an integer, in the source as in the output.
    lists = pa.array([[1], [2]])
    outputs = {"people": pa.table({"id": [1, 2]}), "accounts": pa.table({"person_id": lists})}
    exc = _blocked(
        lambda: gate.run_resident(outputs, {"accounts": pa.table({"person_id": lists})}, GEN_KINDS)
    )
    assert exc.reason == "post_scan.key_type_mismatch"
    (child,) = (k for k in exc.report["keys"] if k["kind"] == "foreign")
    assert child["baseline"] == "inherited_type_mismatch"


def test_a_run_that_changes_a_key_to_a_type_with_no_cast_defers_beside_a_generated_parent() -> None:
    gate = _gate([GENERATED])
    outputs = {
        "people": pa.table({"id": [1.0, 2.0]}),
        "accounts": pa.table({"person_id": pa.array([[1], [2]])}),
    }
    sources = {"accounts": pa.table({"person_id": [1.0, 2.0]})}
    gate.run_resident(outputs, sources, GEN_KINDS)  # not check_error, not a block
    assert gate.deferred and gate.verdict is None


def test_a_run_that_changes_a_float_key_to_int_beside_a_generated_parent_is_measured() -> None:
    gate = _gate([GENERATED])
    outputs = {
        "people": pa.table({"id": [1.0, 2.0]}),
        "accounts": pa.table({"person_id": pa.array([1, 2], pa.int64())}),
    }
    sources = {"accounts": pa.table({"person_id": [1.0, 2.0]})}
    gate.run_resident(outputs, sources, GEN_KINDS)
    assert gate.ran and not gate.deferred


# ----- M1: integrity: true withholds a run on a failing column finding --------------------------


def test_m1_integrity_withholds_a_run_on_a_failing_column_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finding = {"table": "t", "column": "email", "detector_id": "email", "severity": "fail"}

    def failing_suite(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "residual_pii": [finding]}

    monkeypatch.setattr(_post_scan, "run_post_mask", failing_suite)
    cfg, sources = _cfg(tmp_path, post_scan_integrity=True)
    sink = RecordingSink()
    with pytest.raises(PostScanBlockedError) as caught:
        run_pipeline(cfg, sources=sources, engine_version="ps8-m1", sink=sink)
    assert caught.value.reason == "post_scan.finding_blocked"
    assert sink.calls == []
    assert not Path(cfg["targets"]["t"]["path"]).exists()
