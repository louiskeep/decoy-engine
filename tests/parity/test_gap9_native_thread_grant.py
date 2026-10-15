"""GAP9: the per-job `native_threads` grant reaches the sequential and out-of-core native runs.

`run_pipeline(native_threads=N)` already reached the chunked, full-frame and unified routes. On
the two bounded routes the route dispatch dropped it, so `SeqNativeRun` / `OocHashRun` always
ran at 1. These tests drive the real compiled companion through `run_pipeline` and assert the
three things the plan (`docs/plans/2026-10-15-gap9-native-thread-grant.md` section 5) names:

- T1 wiring: the value arrives at the constructed run objects AND at the compiled-kernel
  boundary (every hash / index / fpe call), on both OOC drivers, a multi-table job and the
  sequential REMAP path. Each dropped forward makes a spy read 1 and fails a test here.
- T2 byte parity across thread counts, with evidence the native kernel ran (per-column
  disposition plus a nonempty kernel-call log), not a Python fallback.
- T3 the default is unchanged: omitted equals an explicit 1.

Skipped, not weakened, where the abi-3 companion is not loadable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._sequential_native import PATH_REMAP
from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    load_compiled_crypto_kernel,
)
from decoy_engine.execution.native._fpe_ext import load_compiled_fpe_kernel
from decoy_engine.execution.native._index_ext import load_compiled_index_kernel
from tests.parity import _gap9_jobs as jobs


def _companion_loads() -> bool:
    try:
        load_compiled_crypto_kernel()
        load_compiled_fpe_kernel()
        load_compiled_index_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _companion_loads(),
    reason="decoy-engine-native abi-3 companion not loadable; the companion-present job covers this",
)

_SEQ_NATIVE = {
    ("parent", "id"),
    ("parent", "name"),
    ("parent", "region"),
    ("parent", "dob"),
    ("parent", "ssn"),
    ("parent", "alias"),
    ("parent", "joined"),
    ("child", "acct"),
}
_OOC_NATIVE = {
    ("parent", "id"),
    ("parent", "name"),
    ("parent", "region"),
    ("parent", "dob"),
    ("parent", "ssn"),
    ("child", "acct"),
}
_LANES = ["batch", "reorder"]
_N_VALUES = [2, jobs.THREADS]


def _seq_dispositions(result: Any) -> dict[str, Any]:
    return {k: v for k, v in result.quality_metrics.items() if k.startswith("sequential_")}


def _ooc_dispositions(result: Any) -> dict[str, Any]:
    return {k: v for k, v in result.quality_metrics.items() if k.startswith("out_of_core_")}


def _warning_codes(result: Any) -> list[str]:
    return [str(getattr(w, "code", w)) for w in result.warnings]


# ---------------------------------------------------------------------------
# T1: the grant reaches the run objects and the compiled-kernel boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("threads", [1, *_N_VALUES])
@pytest.mark.parametrize("policy", ["preserve", "remap"])
def test_t1_sequential_grant_reaches_every_kernel(
    threads: int, policy: str, tmp_path: Path
) -> None:
    config, sources = jobs.seq_job(tmp_path, orphan_policy=policy, legacy_faker=False)
    with jobs.spy_kernels() as kernels, jobs.spy_run_objects() as objs:
        result = jobs.run_seq(config, sources, native_threads=threads)
    assert result.quality_metrics["execution"]["execution_mode"] == "sequential"
    # The one run object carries the grant, and so does every compiled call under it.
    assert objs.seq_runs == [threads]
    assert {"hash", "index", "fpe"} <= kernels.kinds()
    assert kernels.values() == {threads}
    assert jobs.native_columns(result.quality_metrics) >= _SEQ_NATIVE


def test_t1_sequential_remap_after_parent_eviction_runs_at_the_grant(tmp_path: Path) -> None:
    config, sources = jobs.seq_job(tmp_path, orphan_policy="remap", legacy_faker=False)
    with jobs.spy_kernels() as kernels, jobs.spy_run_objects() as objs:
        result = jobs.run_seq(config, sources, native_threads=jobs.THREADS)
    entries = result.quality_metrics["sequential_hash"]
    remap = [e for e in entries if e.get("path") == PATH_REMAP]
    # The child's REMAP reads its parent's decision from the same run object after the parent
    # table was emitted, and those hashes ran native.
    assert remap
    assert all(e["executed_backend"] == "rust_companion" for e in remap)
    assert objs.seq_runs == [jobs.THREADS]
    assert kernels.values("hash") == {jobs.THREADS}


@pytest.mark.parametrize("threads", [1, *_N_VALUES])
@pytest.mark.parametrize("lane", _LANES)
def test_t1_out_of_core_grant_reaches_both_drivers_and_every_kernel(
    threads: int, lane: str, tmp_path: Path
) -> None:
    config, paths = jobs.ooc_job(tmp_path)
    with jobs.spy_kernels() as kernels, jobs.spy_run_objects() as objs:
        result, _ = jobs.run_ooc(
            config, paths, tmp_path / "out", lane=lane, batch_rows=100, native_threads=threads
        )
    assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    # Constructed exactly once, with the grant, and that one object is what every driver call got.
    assert objs.ooc_runs == [threads]
    assert {run_id for _, _, run_id, _ in objs.drivers} == set(objs.ooc_run_ids)
    assert {value for _, _, _, value in objs.drivers} == {threads}
    # A multi-table job: both tables went through a driver.
    assert {table for table, _, _, _ in objs.drivers} == {"parent", "child"}
    drivers = {driver for _, driver, _, _ in objs.drivers}
    # The reorder lane exercises BOTH drivers (root table on batch, child on reorder).
    assert drivers == ({"batch"} if lane == "batch" else {"batch", "reorder"})
    assert {"hash", "index", "fpe"} <= kernels.kinds()
    assert kernels.values() == {threads}
    assert jobs.native_columns(result.quality_metrics) >= _OOC_NATIVE


# ---------------------------------------------------------------------------
# T2: byte parity across thread counts (the gate)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["preserve", "remap"])
@pytest.mark.parametrize("threads", _N_VALUES)
def test_t2_sequential_output_is_identical_across_thread_counts(
    policy: str, threads: int, tmp_path: Path
) -> None:
    config, sources = jobs.seq_job(tmp_path, orphan_policy=policy)
    with jobs.spy_kernels() as one_calls:
        one = jobs.run_seq(config, sources, native_threads=1)
    with jobs.spy_kernels() as many_calls:
        many = jobs.run_seq(config, sources, native_threads=threads)
    # Native evidence for both runs: the compiled kernels ran, not a Python fallback.
    for calls, result in ((one_calls, one), (many_calls, many)):
        assert {"hash", "index", "fpe"} <= calls.kinds()
        assert jobs.native_columns(result.quality_metrics) >= _SEQ_NATIVE
    assert jobs.digest(one.outputs) == jobs.digest(many.outputs)
    for name, table in one.outputs.items():
        assert table.equals(many.outputs[name], check_metadata=True), name
    assert _seq_dispositions(one) == _seq_dispositions(many)
    assert _warning_codes(one) == _warning_codes(many)
    # The job really masked: a changed key column, not an echo of the source.
    assert (
        one.outputs["parent"].column("id").to_pylist() != sources["parent"].column("id").to_pylist()
    )


@pytest.mark.parametrize("policy", ["preserve", "remap"])
@pytest.mark.parametrize("batch_rows", [None, 100])
@pytest.mark.parametrize("lane", _LANES)
def test_t2_out_of_core_output_is_identical_across_thread_counts(
    policy: str, batch_rows: int | None, lane: str, tmp_path: Path
) -> None:
    config, paths = jobs.ooc_job(tmp_path, orphan_policy=policy)
    with jobs.spy_kernels() as one_calls:
        one, one_out = jobs.run_ooc(
            config, paths, tmp_path / "o1", lane=lane, batch_rows=batch_rows, native_threads=1
        )
    with jobs.spy_kernels() as many_calls:
        many, many_out = jobs.run_ooc(
            config,
            paths,
            tmp_path / "oN",
            lane=lane,
            batch_rows=batch_rows,
            native_threads=jobs.THREADS,
        )
    for calls, result in ((one_calls, one), (many_calls, many)):
        assert {"hash", "index", "fpe"} <= calls.kinds()
        assert jobs.native_columns(result.quality_metrics) >= _OOC_NATIVE
    assert jobs.digest(one_out) == jobs.digest(many_out)
    for name, table in one_out.items():
        assert table.equals(many_out[name], check_metadata=True), name
    assert _ooc_dispositions(one) == _ooc_dispositions(many)
    assert _warning_codes(one) == _warning_codes(many)


def test_t2_composite_foreign_key_is_identical_across_thread_counts(tmp_path: Path) -> None:
    config, sources, paths = jobs.composite_job(tmp_path)
    one_seq = jobs.run_seq(config, sources, native_threads=1)
    many_seq = jobs.run_seq(config, sources, native_threads=jobs.THREADS)
    assert jobs.digest(one_seq.outputs) == jobs.digest(many_seq.outputs)
    for lane in _LANES:
        _, one_out = jobs.run_ooc(
            config, paths, tmp_path / f"{lane}-1", lane=lane, batch_rows=50, native_threads=1
        )
        _, many_out = jobs.run_ooc(
            config,
            paths,
            tmp_path / f"{lane}-N",
            lane=lane,
            batch_rows=50,
            native_threads=jobs.THREADS,
        )
        assert jobs.digest(one_out) == jobs.digest(many_out), lane


def _bad_fpe_value(config: dict[str, Any], table: pa.Table) -> pa.Table:
    """Put one unencryptable cell in `ssn` (a letter in a digits-only column, separators off)."""
    parent = next(t for t in config["tables"] if t["name"] == "parent")
    ssn = next(c for c in parent["columns"] if c["name"] == "ssn")
    ssn["provider_config"] = {"charset": "digits", "preserve_separators": False}
    cells = table.column("ssn").to_pylist()
    cells[5] = "12ab34"
    return table.set_column(table.schema.get_field_index("ssn"), "ssn", pa.array(cells))


def _failure(call: Any) -> tuple[str, str, str]:
    with pytest.raises(Exception) as caught:
        call()
    exc = caught.value
    return type(exc).__name__, str(getattr(exc, "code", "")), str(exc)


def test_t2_sequential_fpe_row_errors_are_identical_across_thread_counts(tmp_path: Path) -> None:
    config, sources = jobs.seq_job(tmp_path)
    sources["parent"] = _bad_fpe_value(config, sources["parent"])
    with jobs.spy_kernels() as calls:
        one = _failure(lambda: jobs.run_seq(config, sources, native_threads=1))
    many = _failure(lambda: jobs.run_seq(config, sources, native_threads=jobs.THREADS))
    assert "fpe" in calls.kinds()  # the native kernel produced the error, not the Python path
    assert one == many
    assert one[1] == "fpe_unencryptable_value"
    assert "12ab34" not in one[2]


def test_t2_out_of_core_fpe_row_errors_are_identical_across_thread_counts(
    tmp_path: Path,
) -> None:
    config, paths = jobs.ooc_job(tmp_path)
    parent = _bad_fpe_value(config, pq.read_table(paths["parent"]))
    pq.write_table(parent, paths["parent"])
    results = []
    for threads in (1, jobs.THREADS):
        with jobs.spy_kernels() as calls:
            results.append(
                _failure(
                    lambda t=threads: jobs.run_ooc(
                        config, paths, tmp_path / f"o{t}", lane="batch", native_threads=t
                    )
                )
            )
        assert "fpe" in calls.kinds()
    assert results[0] == results[1]
    assert results[0][1] == "fpe_unencryptable_value"
    assert "12ab34" not in results[0][2]


# ---------------------------------------------------------------------------
# T3: the default is unchanged
# ---------------------------------------------------------------------------


def test_t3_omitted_grant_equals_an_explicit_one_on_both_routes(tmp_path: Path) -> None:
    config, sources = jobs.seq_job(tmp_path)
    with jobs.spy_run_objects() as objs:
        omitted = jobs.run_seq(config, sources)
    explicit = jobs.run_seq(config, sources, native_threads=1)
    assert objs.seq_runs == [1]
    assert jobs.digest(omitted.outputs) == jobs.digest(explicit.outputs)

    (tmp_path / "ooc").mkdir()
    ooc_config, paths = jobs.ooc_job(tmp_path / "ooc")
    for lane in _LANES:
        with jobs.spy_run_objects() as ooc_objs:
            _, omitted_out = jobs.run_ooc(ooc_config, paths, tmp_path / f"d-{lane}", lane=lane)
        _, explicit_out = jobs.run_ooc(
            ooc_config, paths, tmp_path / f"e-{lane}", lane=lane, native_threads=1
        )
        assert ooc_objs.ooc_runs == [1]
        assert jobs.digest(omitted_out) == jobs.digest(explicit_out), lane


def test_t3_a_mis_wired_grant_would_change_what_the_spies_see(tmp_path: Path) -> None:
    """Control: the spies are not blind. A different grant is visible, so equal digests across
    thread counts (T2) cannot be explained by every call silently running at one value."""
    config, sources = jobs.seq_job(tmp_path, legacy_faker=False)
    with jobs.spy_kernels() as low, jobs.spy_run_objects():
        jobs.run_seq(config, sources, native_threads=1)
    with jobs.spy_kernels() as high, jobs.spy_run_objects():
        jobs.run_seq(config, sources, native_threads=jobs.THREADS)
    assert low.values() == {1} and high.values() == {jobs.THREADS}
