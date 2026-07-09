"""C4: host-aware memory budget, batch sizing, and temp-disk spill guard.

`resolve_budget` must give a caller ONE knob that bounds the out-of-core
route's memory: a DuckDB `memory_limit` string sized to an explicit budget (or
a conservative fraction of detected host RAM), plus a `batch_rows` sized to
that budget, floored and capped so it is never zero, never absurd, and never
sized by table cardinality. `check_temp_disk_budget` must fail closed with a
coded ExecutionError before the spill footprint outgrows its disk budget. The
`batch_rows` override on `run_fk_out_of_core` must be byte-transparent: any
legal batch size produces output identical to the default.
"""

from __future__ import annotations

from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core import _budget as budget_mod
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.execution.out_of_core._budget import (
    check_temp_disk_budget,
    detect_host_memory_bytes,
    resolve_budget,
    temp_disk_bytes,
)
from decoy_engine.execution.out_of_core._join import _JOIN_BATCH_ROWS
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy
from tests.perf_fixtures.fk_relational import (
    lazy_sources,
    make_graph,
    make_plan,
    write_large_fk_chain,
)

_MIB = 1024 * 1024
_GIB = 1024 * _MIB


class TestDetectHostMemory:
    def test_uses_sysconf_when_available(self, monkeypatch) -> None:
        def fake_sysconf(name: str) -> int:
            return {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 2 * _GIB // 4096}[name]

        monkeypatch.setattr(budget_mod.os, "sysconf", fake_sysconf)
        assert detect_host_memory_bytes() == 2 * _GIB

    def test_falls_back_to_proc_meminfo(self, monkeypatch, tmp_path) -> None:
        def broken_sysconf(name: str) -> int:
            raise ValueError(name)

        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:        8388608 kB\nMemFree:  100 kB\n")
        monkeypatch.setattr(budget_mod.os, "sysconf", broken_sysconf)
        monkeypatch.setattr(budget_mod, "_PROC_MEMINFO", meminfo)
        assert detect_host_memory_bytes() == 8 * _GIB

    def test_fails_closed_when_undetectable(self, monkeypatch, tmp_path) -> None:
        def broken_sysconf(name: str) -> int:
            raise ValueError(name)

        monkeypatch.setattr(budget_mod.os, "sysconf", broken_sysconf)
        monkeypatch.setattr(budget_mod, "_PROC_MEMINFO", tmp_path / "missing")
        with pytest.raises(ExecutionError) as excinfo:
            detect_host_memory_bytes()
        assert excinfo.value.code == "out_of_core_memory_detection_failed"


class TestResolveBudget:
    def test_auto_budget_is_conservative_fraction_of_host_ram(self, monkeypatch) -> None:
        monkeypatch.setattr(budget_mod, "detect_host_memory_bytes", lambda: 8 * _GIB)
        budget = resolve_budget()
        assert budget.budget_bytes == 2 * _GIB
        assert budget.memory_limit == "2048MB"

    def test_explicit_budget_wins_over_detection(self, monkeypatch) -> None:
        def no_detection() -> int:
            raise AssertionError("explicit budget must not trigger host detection")

        monkeypatch.setattr(budget_mod, "detect_host_memory_bytes", no_detection)
        budget = resolve_budget(budget_bytes=512 * _MIB)
        assert budget.budget_bytes == 512 * _MIB
        assert budget.memory_limit == "512MB"

    def test_budget_floor_never_returns_absurdly_small_values(self, monkeypatch) -> None:
        monkeypatch.setattr(budget_mod, "detect_host_memory_bytes", lambda: 64 * _MIB)
        for budget in (resolve_budget(), resolve_budget(budget_bytes=1)):
            assert budget.budget_bytes >= 64 * _MIB
            assert budget.memory_limit == "64MB"
            assert budget.batch_rows >= 1_024

    def test_batch_rows_scales_with_budget_but_is_bounded(self) -> None:
        small = resolve_budget(budget_bytes=64 * _MIB)
        mid = resolve_budget(budget_bytes=512 * _MIB)
        huge = resolve_budget(budget_bytes=64 * _GIB)
        assert small.batch_rows <= mid.batch_rows <= huge.batch_rows
        # The ceiling is the route's pinned default: a bigger host must not
        # silently change behavior versus the constant the suite pins.
        assert huge.batch_rows == _JOIN_BATCH_ROWS
        assert small.batch_rows >= 1_024

    def test_rejects_nonpositive_budget(self) -> None:
        with pytest.raises(ExecutionError) as excinfo:
            resolve_budget(budget_bytes=0)
        assert excinfo.value.code == "out_of_core_budget_invalid"


class TestTempDiskGuard:
    def test_footprint_counts_all_files_under_root(self, tmp_path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "a.bin").write_bytes(b"x" * 1000)
        (tmp_path / "sub" / "b.bin").write_bytes(b"y" * 500)
        assert temp_disk_bytes(tmp_path) == 1500
        assert temp_disk_bytes(tmp_path / "missing") == 0

    def test_passes_below_budget_and_returns_usage(self, tmp_path) -> None:
        (tmp_path / "spill.tmp").write_bytes(b"z" * 100)
        assert check_temp_disk_budget(tmp_path, max_bytes=1000) == 100

    def test_trips_fail_closed_at_budget(self, tmp_path) -> None:
        (tmp_path / "spill.tmp").write_bytes(b"z" * 2000)
        with pytest.raises(ExecutionError) as excinfo:
            check_temp_disk_budget(tmp_path, max_bytes=1000)
        assert excinfo.value.code == "out_of_core_temp_disk_exceeded"


class TestRunnerBatchRows:
    def _run(self, paths, tmp_path, tag: str, **kwargs) -> SimpleNamespace:
        target = tmp_path / f"published-{tag}"
        run_fk_out_of_core(
            make_plan(),
            lazy_sources(paths),
            registry=get_default_registry(),
            relationship_graph=make_graph(OrphanPolicy.PRESERVE),
            sink=ParquetTransactionalSink(target),
            temp_dir=tmp_path / f"work-{tag}",
            **kwargs,
        )
        return SimpleNamespace(
            outputs={
                name: pq.read_table(target / f"{name}.parquet")
                for name in ("parent", "child", "grandchild")
            }
        )

    def test_batch_rows_and_memory_caps_are_byte_identical_to_default(self, tmp_path) -> None:
        """C4 acceptance: the same job under a smaller batch size and a tighter
        DuckDB memory cap still produces byte-identical published output."""
        paths = write_large_fk_chain(
            tmp_path / "src", 700, width=2, orphan_frac=0.02, batch_rows=300
        )
        default = self._run(paths, tmp_path, "default")
        small = self._run(paths, tmp_path, "small", batch_rows=97)
        budgeted = self._run(paths, tmp_path, "budgeted", batch_rows=resolve_budget().batch_rows)
        capped = self._run(paths, tmp_path, "capped", batch_rows=97, memory_limit="64MB")
        for name in ("parent", "child", "grandchild"):
            assert small.outputs[name].equals(default.outputs[name]), name
            assert budgeted.outputs[name].equals(default.outputs[name]), name
            assert capped.outputs[name].equals(default.outputs[name]), name

    def test_batch_rows_override_bounds_streamed_batches(self, tmp_path, monkeypatch) -> None:
        from decoy_engine.execution.out_of_core import _runner as runner_mod

        paths = write_large_fk_chain(tmp_path / "src", 400, width=1, batch_rows=200)
        sizes: list[int] = []
        real_mask_batch = runner_mod.mask_batch

        def spy(*args: object, **kwargs: object) -> pa.RecordBatch:
            sizes.append(args[2].num_rows)  # type: ignore[union-attr]
            return real_mask_batch(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(runner_mod, "mask_batch", spy)
        self._run(paths, tmp_path, "bounded", batch_rows=64)
        assert sizes and max(sizes) <= 64

    def test_rejects_nonpositive_batch_rows(self, tmp_path) -> None:
        paths = write_large_fk_chain(tmp_path / "src", 10, width=1)
        with pytest.raises(ExecutionError) as excinfo:
            self._run(paths, tmp_path, "invalid", batch_rows=0)
        assert excinfo.value.code == "out_of_core_batch_rows_invalid"

    def test_temp_disk_budget_trips_fail_closed_and_aborts_sink(self, tmp_path) -> None:
        paths = write_large_fk_chain(tmp_path / "src", 500, width=2, batch_rows=250)
        with pytest.raises(ExecutionError) as excinfo:
            self._run(paths, tmp_path, "disk", temp_disk_budget_bytes=1)
        assert excinfo.value.code == "out_of_core_temp_disk_exceeded"
        # Fail closed all the way: the transactional sink must not publish.
        assert not (tmp_path / "published-disk").exists()

    def test_temp_disk_budget_passes_when_generous(self, tmp_path) -> None:
        paths = write_large_fk_chain(tmp_path / "src", 200, width=1, batch_rows=100)
        result = self._run(paths, tmp_path, "disk-ok", temp_disk_budget_bytes=1 * _GIB)
        assert result.outputs["child"].num_rows == 200
