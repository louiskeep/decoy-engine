"""GAP9 routing-level wiring, no compiled companion needed.

The grant is read off the run objects the two bounded routes construct, so these checks hold
on a box without the abi-3 companion (the Python handlers run, the constructor still gets the
value). The kernel-boundary and byte-parity halves live in
`tests/parity/test_gap9_native_thread_grant.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.parity import _gap9_jobs as jobs


@pytest.mark.parametrize("threads", [1, 3, jobs.THREADS])
def test_sequential_route_hands_the_grant_to_its_native_run(threads: int, tmp_path: Path) -> None:
    config, sources = jobs.seq_job(tmp_path, legacy_faker=False)
    with jobs.spy_run_objects() as objs:
        result = jobs.run_seq(config, sources, native_threads=threads)
    assert result.quality_metrics["execution"]["execution_mode"] == "sequential"
    assert objs.seq_runs == [threads]


def test_sequential_route_default_is_one(tmp_path: Path) -> None:
    config, sources = jobs.seq_job(tmp_path, legacy_faker=False)
    with jobs.spy_run_objects() as objs:
        jobs.run_seq(config, sources)
    assert objs.seq_runs == [1]


@pytest.mark.parametrize("lane", ["batch", "reorder"])
@pytest.mark.parametrize("threads", [1, jobs.THREADS])
def test_out_of_core_route_builds_one_configured_run_for_every_driver(
    lane: str, threads: int, tmp_path: Path
) -> None:
    config, paths = jobs.ooc_job(tmp_path)
    with jobs.spy_run_objects() as objs:
        jobs.run_ooc(config, paths, tmp_path / "out", lane=lane, native_threads=threads)
    assert objs.ooc_runs == [threads]
    assert {run_id for _, _, run_id, _ in objs.drivers} == set(objs.ooc_run_ids)
    assert {value for _, _, _, value in objs.drivers} == {threads}
    assert {table for table, _, _, _ in objs.drivers} == {"parent", "child"}
    assert {driver for _, driver, _, _ in objs.drivers} == (
        {"batch"} if lane == "batch" else {"batch", "reorder"}
    )


def test_out_of_core_route_default_is_one(tmp_path: Path) -> None:
    config, paths = jobs.ooc_job(tmp_path)
    with jobs.spy_run_objects() as objs:
        jobs.run_ooc(config, paths, tmp_path / "out", lane="batch")
    assert objs.ooc_runs == [1]
