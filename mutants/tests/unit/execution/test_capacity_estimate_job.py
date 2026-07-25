"""`estimate_job_capacity`: end-to-end over real fixture files (T6/T7/T8
groundwork, docs/plans/2026-07-24-oom-checker-cli-v1.md).

Covers: NOT_APPLICABLE for a no-relationships config and for a job below the
out-of-core size threshold that is NOT out-of-core-compatible; the Codex
P1-1 anti-under-refusal proof for a below-threshold job that IS out-of-core-
compatible (`TestUnderRefusalAntiProof`); Parquet footer-only row counts
(never a full frame read) alongside the real bounded local sample the
profiler DOES take (`TestParquetFooterOnly`); a CSV parent table forcing
UNKNOWN (R6); FIT/INSUFFICIENT through the real budget-resolution path; R3
propagation of an unexpected budget-resolution defect
(`TestBudgetResolutionR3`); and base_dir-driven (not CWD-driven) source path
resolution (R2).

`decide_execution_route`'s `out_of_core_threshold_rows` default is
5,000,000 rows, and `estimate_job_capacity` has no kwarg to override it (R2's
signature is `config_dump`/`base_dir`/`budget_bytes` only) -- the
`low_threshold` fixture monkeypatches `capacity.decide_execution_route` with
a thin wrapper that lowers it, the same trick
`test_lazy_path_route_admission.py` uses one layer up as a `run_pipeline`
kwarg, so a 40-row fixture exercises the same routing decision a
multi-million-row job would, without the data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import capacity as capacity_mod
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.capacity import estimate_job_capacity
from decoy_engine.execution.out_of_core._capacity_eval import CapacityVerdict

_N = 40
_MIB = 1024 * 1024
_GIB = 1024 * _MIB


def _hash_col(name: str, namespace: str) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": namespace}


def _fk_relationships() -> list[dict[str, Any]]:
    return [
        {
            "parent": {"table": "parent", "columns": ["id"]},
            "children": [{"table": "child", "columns": ["parent_id"]}],
            "orphan_policy": "preserve",
            "namespace": "ns",
        }
    ]


def _parent_child_tables(n: int = _N) -> tuple[pa.Table, pa.Table]:
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
            "note": pa.array([f"secret{i}" for i in range(n)], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(n)], type=pa.string()),
            "parent_id": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
        }
    )
    return parent, child


def _write_parquet(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _write_csv(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.csv"
    table.to_pandas().to_csv(p, index=False)
    return str(p)


def _ooc_config(
    tmp_path: Path,
    *,
    parent_fmt: str = "parquet",
    child_fmt: str = "parquet",
    tables: tuple[pa.Table, pa.Table] | None = None,
) -> dict[str, Any]:
    """A pure-mask FK job whose every strategy (hash keys + a redact payload)
    is in the out-of-core supported set -- the shape that auto-routes to
    out-of-core once it clears the size threshold."""
    parent, child = tables if tables is not None else _parent_child_tables()
    parent_src = (
        _write_parquet(tmp_path, parent, "parent")
        if parent_fmt == "parquet"
        else _write_csv(tmp_path, parent, "parent")
    )
    child_src = (
        _write_parquet(tmp_path, child, "child")
        if child_fmt == "parquet"
        else _write_csv(tmp_path, child, "child")
    )
    return {
        "version": 1,
        "global_settings": {"job_name": "capacity-estimate-test", "seed": 7},
        "sources": {
            "parent": {"type": "file", "path": parent_src, "format": parent_fmt},
            "child": {"type": "file", "path": child_src, "format": child_fmt},
        },
        "targets": {
            "parent": {
                "type": "file",
                "path": str(tmp_path / "parent.out.parquet"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": str(tmp_path / "child.out.parquet"),
                "format": "parquet",
            },
        },
        "tables": [
            {
                "name": "parent",
                "columns": [_hash_col("id", "ns"), {"name": "note", "strategy": "redact"}],
            },
            {"name": "child", "columns": [_hash_col("cid", "cns"), _hash_col("parent_id", "ns")]},
        ],
        "relationships": _fk_relationships(),
    }


@pytest.fixture
def low_threshold(monkeypatch):
    """Lower `decide_execution_route`'s size thresholds so the 40-row fixture
    routes `out_of_core` instead of `sequential` -- mirrors
    `test_lazy_path_route_admission.py`'s `run_pipeline(out_of_core_threshold_
    rows=10)`, one layer down (`estimate_job_capacity` exposes no such kwarg
    itself, so the wrapper is patched in at the call `capacity.py` makes)."""
    real_decide = capacity_mod.decide_execution_route

    def _patched(*args, **kwargs):
        kwargs.setdefault("out_of_core_threshold_rows", 10)
        kwargs.setdefault("full_frame_reject_rows", 10)
        return real_decide(*args, **kwargs)

    monkeypatch.setattr(capacity_mod, "decide_execution_route", _patched)


class TestNotApplicable:
    def test_no_relationships(self, tmp_path: Path) -> None:
        config = {
            "version": 1,
            "global_settings": {"job_name": "no-fk", "seed": 1},
            "sources": {},
            "targets": {},
            "tables": [],
        }
        est = estimate_job_capacity(config, tmp_path)
        assert est.verdict is CapacityVerdict.NOT_APPLICABLE
        assert est.code is None

    def test_below_size_threshold_is_ambiguous_not_a_confirmed_not_applicable(
        self, tmp_path: Path
    ) -> None:
        # No low_threshold fixture: at the real 5,000,000-row default, the
        # row-count-only decision (byte-estimate routing OFF) picks
        # `sequential` for this 40-row fixture. But this fixture IS
        # out-of-core-compatible, and a real `decoy run` (byte-estimate
        # routing ON by default) drops `out_of_core_threshold_rows` entirely
        # once its byte estimate fails to confirm a `full_frame` fit --
        # routing an out-of-core-compatible job to `out_of_core` REGARDLESS
        # of size (see `test_byte_estimate_routing.py::
        # test_does_not_fit_routes_out_of_core_when_eligible_and_compatible`).
        # `estimate_job_capacity` cannot compute that real byte estimate
        # itself (R6), so it cannot confirm which route a real run takes --
        # NOT_APPLICABLE here would be a false "fine" (Codex P1-1's
        # under-refusal finding); UNKNOWN is the honest verdict.
        config = _ooc_config(tmp_path)
        est = estimate_job_capacity(config, tmp_path)
        assert est.verdict is CapacityVerdict.UNKNOWN
        assert est.route == "out_of_core"
        assert "byte-level estimate" in est.message

    def test_below_size_threshold_and_not_ooc_compatible_stays_not_applicable(
        self, tmp_path: Path
    ) -> None:
        # `faker` is a deferred-unsupported out-of-core strategy (falls back
        # to sequential/full-frame, `_compat._DEFERRED_GROUP_B`), so this job
        # is out-of-core-INCOMPATIBLE -- there is no real-run ambiguity to
        # resolve here: neither the row-count-only decision NOR a real run's
        # byte-estimate routing (`decide_execution_route`'s `out_of_core`
        # branch also requires `out_of_core_compatible`) can ever send this
        # job to `out_of_core`. NOT_APPLICABLE stays correct -- proves the
        # P1-1 fix does not turn every below-threshold job into UNKNOWN, only
        # the genuinely ambiguous (out-of-core-COMPATIBLE) ones.
        parent, child = _parent_child_tables()
        config = _ooc_config(tmp_path, tables=(parent, child))
        config["tables"][0]["columns"] = [
            _hash_col("id", "ns"),
            {"name": "note", "strategy": "faker", "provider": "person_email"},
        ]
        est = estimate_job_capacity(config, tmp_path)
        assert est.verdict is CapacityVerdict.NOT_APPLICABLE
        assert est.route == "sequential"


class TestUnderRefusalAntiProof:
    """Codex P1-1 gate finding: the anti-under-refusal proof.

    Mirrors `test_byte_estimate_routing.py::TestFlagOnByteEstimateRouting::
    test_does_not_fit_routes_out_of_core_when_eligible_and_compatible` at the
    `estimate_job_capacity` layer: a wide, out-of-core-compatible parent table
    BELOW `out_of_core_threshold_rows` (so the row-count-only decision alone
    would pick `sequential`), under a budget too tiny for its real
    `out_of_core` build floor. A real `decoy run` (byte-estimate routing on
    by default) routes this exact job to `out_of_core` regardless of its
    size and refuses it there (`out_of_core_insufficient_memory`). Before the
    P1-1 fix, `estimate_job_capacity` reported NOT_APPLICABLE/`sequential` on
    this job -- a false "fine" on a job `decoy run` actually refuses. It must
    now report INSUFFICIENT or UNKNOWN, NEVER NOT_APPLICABLE or FIT.
    """

    def test_below_threshold_ooc_compatible_tiny_budget_never_fit_or_not_applicable(
        self, tmp_path: Path
    ) -> None:
        # No low_threshold fixture: 300k rows is genuinely below the real
        # 5,000,000-row out_of_core_threshold_rows default, so the row-count-
        # only decision (byte-estimate routing OFF) picks `sequential` here.
        big_parent, big_child = _parent_child_tables(300_000)
        config = _ooc_config(tmp_path, tables=(big_parent, big_child))
        est = estimate_job_capacity(config, tmp_path, budget_bytes=1 * _MIB)
        assert est.verdict in {CapacityVerdict.INSUFFICIENT, CapacityVerdict.UNKNOWN}
        assert est.verdict is not CapacityVerdict.NOT_APPLICABLE
        assert est.verdict is not CapacityVerdict.FIT
        # The 1 MiB budget is far below this parent's real build floor -- the
        # promoted out-of-core pricing (not a mere "can't tell") should reach
        # a definite INSUFFICIENT, matching the real gate's own refusal.
        assert est.verdict is CapacityVerdict.INSUFFICIENT
        assert est.code in {"out_of_core_insufficient_memory", "out_of_core_fanin_exceeds_budget"}


class TestParquetFooterOnly:
    def test_fit_never_materializes_a_full_frame_but_does_take_a_bounded_sample(
        self, tmp_path: Path, low_threshold, monkeypatch
    ) -> None:
        # T6 + P2 (Codex gate): the row-count derivation must read the
        # Parquet FOOTER only, never a whole-file read -- `pq.read_table`
        # (the only whole-file call on this path, `ParquetFileSource.
        # to_frame`) is blocked and proven cold. But `profile_source`'s
        # bounded residency DOES take a real, bounded (<=10k-row) LOCAL
        # sample per source via `pq.ParquetFile.iter_batches` (`_readers.py`
        # `LazySource.iter_batches` / `ParquetFileSource.sample_frame`) --
        # the prior version of this test asserted only the first half and
        # left the sample read unverified, which understated what this
        # checker actually reads (see the module docstring's honest framing:
        # "a bounded local sample", not metadata-only).
        real_iter_batches = pq.ParquetFile.iter_batches
        sample_batch_sizes: list[int] = []

        def _spy_iter_batches(self: pq.ParquetFile, *args: Any, **kwargs: Any) -> Any:
            batch_size = kwargs.get("batch_size", args[0] if args else None)
            if batch_size is not None:
                sample_batch_sizes.append(batch_size)
            return real_iter_batches(self, *args, **kwargs)

        monkeypatch.setattr(pq.ParquetFile, "iter_batches", _spy_iter_batches)
        monkeypatch.setattr(
            pq, "read_table", mock.Mock(side_effect=AssertionError("must not read a full frame"))
        )
        config = _ooc_config(tmp_path)
        est = estimate_job_capacity(config, tmp_path, budget_bytes=64 * _GIB)
        assert est.verdict is CapacityVerdict.FIT
        assert est.route == "out_of_core"
        # The bounded sample DID run (once per file source), and every call
        # asked for at most the 10k-row default cap -- never unbounded.
        assert sample_batch_sizes, "expected a bounded iter_batches sample per source"
        assert all(size <= 10_000 for size in sample_batch_sizes)

    def test_insufficient_on_a_tiny_budget(self, tmp_path: Path, low_threshold) -> None:
        # `resolve_ooc_memory_limit` floors any explicit budget at
        # `_MIN_BUDGET_BYTES` (64 MiB), so a large-enough parent table is
        # needed to push its floor past even that minimum cap -- a 300k-row
        # parent's floor (~81 MB) comfortably clears the ~64 MB cap a 64 MiB
        # budget resolves to for a single live instance.
        big_parent, big_child = _parent_child_tables(300_000)
        config = _ooc_config(tmp_path, tables=(big_parent, big_child))
        est = estimate_job_capacity(config, tmp_path, budget_bytes=1 * _MIB)
        assert est.verdict is CapacityVerdict.INSUFFICIENT
        assert est.code in {"out_of_core_insufficient_memory", "out_of_core_fanin_exceeds_budget"}
        assert est.needed_bytes is None or est.needed_bytes > 0


class TestCsvParentForcesUnknown:
    def test_csv_parent_row_count_is_never_trusted_for_the_floor(
        self, tmp_path: Path, low_threshold
    ) -> None:
        config = _ooc_config(tmp_path, parent_fmt="csv", child_fmt="csv")
        est = estimate_job_capacity(config, tmp_path, budget_bytes=64 * _GIB)
        assert est.verdict is CapacityVerdict.UNKNOWN
        assert "parent" in est.message


class TestBudgetResolutionR3:
    """Codex P1-2 (item 1): the budget-resolution `try/except` around
    `resolve_ooc_memory_limit` must catch ONLY the one expected "RAM
    undetectable" code and RE-RAISE everything else -- never fold an
    unexpected defect into a silent `UNKNOWN`."""

    def test_ram_undetectable_folds_to_unknown_not_a_crash(
        self, tmp_path: Path, low_threshold, monkeypatch
    ) -> None:
        config = _ooc_config(tmp_path)

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise ExecutionError(
                code="out_of_core_memory_detection_failed",
                message="host RAM could not be detected (test double).",
            )

        monkeypatch.setattr(capacity_mod, "resolve_ooc_memory_limit", _boom)
        est = estimate_job_capacity(config, tmp_path)  # no explicit budget_bytes
        assert est.verdict is CapacityVerdict.UNKNOWN

    def test_other_execution_error_propagates_never_swallowed(
        self, tmp_path: Path, low_threshold, monkeypatch
    ) -> None:
        config = _ooc_config(tmp_path)

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise ExecutionError(
                code="out_of_core_concurrency_invalid",
                message="max_concurrent_instances must be >= 1, got 0 (test double).",
            )

        monkeypatch.setattr(capacity_mod, "resolve_ooc_memory_limit", _boom)
        with pytest.raises(ExecutionError, match="test double"):
            estimate_job_capacity(config, tmp_path)

    def test_explicit_budget_bytes_never_caught_even_for_the_expected_code(
        self, tmp_path: Path, low_threshold, monkeypatch
    ) -> None:
        # An explicit caller-supplied budget_bytes must never be swallowed,
        # even for the one code that IS caught when budget_bytes is None.
        config = _ooc_config(tmp_path)

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise ExecutionError(
                code="out_of_core_memory_detection_failed",
                message="should not be reached with an explicit budget (test double).",
            )

        monkeypatch.setattr(capacity_mod, "resolve_ooc_memory_limit", _boom)
        with pytest.raises(ExecutionError, match="test double"):
            estimate_job_capacity(config, tmp_path, budget_bytes=1 * _MIB)

    def test_unexpected_routing_error_propagates_not_swallowed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # R3 (Codex re-gate HIGH): the two `decide_execution_route` catches must
        # fold ONLY a genuine reject-before-read code into NOT_APPLICABLE; an
        # unexpected routing code is a defect and must propagate, never be
        # swallowed into a false "fine".
        config = _ooc_config(tmp_path)

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise ExecutionError(
                code="some_unexpected_routing_bug",
                message="a routing defect that is NOT a reject-before-read (test double).",
            )

        monkeypatch.setattr(capacity_mod, "decide_execution_route", _boom)
        with pytest.raises(ExecutionError, match="test double"):
            estimate_job_capacity(config, tmp_path)

    def test_expected_reject_code_folds_to_not_applicable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # The flip side: a genuine reject-before-read code IS expected here and
        # folds to NOT_APPLICABLE (the capacity check does not apply to a job
        # the router would refuse before reading any data).
        config = _ooc_config(tmp_path)

        def _reject(*_args: Any, **_kwargs: Any) -> Any:
            raise ExecutionError(
                code="fk_full_frame_oom_risk_rejected",
                message="full-frame OOM risk; would be rejected before read (test double).",
            )

        monkeypatch.setattr(capacity_mod, "decide_execution_route", _reject)
        est = estimate_job_capacity(config, tmp_path)
        assert est.verdict is CapacityVerdict.NOT_APPLICABLE

    def test_corrupt_source_raises_typed_unprofilable_not_raw(self, tmp_path: Path) -> None:
        # A present-but-corrupt source (opens fine, but is not valid Parquet)
        # fails when profile_source reads it. It must surface as the TYPED
        # capacity_source_unprofilable ExecutionError -- an expected "unusable
        # source" condition a caller can render cleanly -- not a raw
        # pyarrow.ArrowInvalid the caller has to guess at (Codex re-gate MEDIUM).
        config = _ooc_config(tmp_path)
        (tmp_path / "child.parquet").write_bytes(b"NOT_A_PARQUET_FILE_corrupt")
        with pytest.raises(ExecutionError) as ei:
            estimate_job_capacity(config, tmp_path)
        assert ei.value.code == "capacity_source_unprofilable"


class TestBaseDirResolution:
    def test_relative_source_path_resolves_against_base_dir_not_cwd(
        self, tmp_path: Path, low_threshold
    ) -> None:
        config = _ooc_config(tmp_path)
        # Rewrite source paths to be RELATIVE (as a real pipeline YAML would
        # declare them) and confirm the estimator still finds the files via
        # base_dir, matching decoy run's own path-resolution convention (R2)
        # -- independent of whatever the test runner's CWD happens to be.
        for spec in config["sources"].values():
            spec["path"] = Path(spec["path"]).name
        est = estimate_job_capacity(config, tmp_path, budget_bytes=64 * _GIB)
        assert est.verdict is CapacityVerdict.FIT
