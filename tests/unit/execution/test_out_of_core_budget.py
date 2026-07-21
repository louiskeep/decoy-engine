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
    resolve_ooc_memory_limit,
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
        # No cgroup limit present: falls through to the host-RAM fraction.
        monkeypatch.setattr(budget_mod, "detect_cgroup_memory_limit_bytes", lambda: None)
        monkeypatch.setattr(budget_mod, "detect_host_memory_bytes", lambda: 8 * _GIB)
        budget = resolve_budget()
        assert budget.budget_bytes == 2 * _GIB
        assert budget.memory_limit == "2048MB"

    def test_explicit_budget_wins_over_detection(self, monkeypatch) -> None:
        def no_detection() -> int:
            raise AssertionError("explicit budget must not trigger host detection")

        monkeypatch.setattr(budget_mod, "detect_host_memory_bytes", no_detection)
        monkeypatch.setattr(budget_mod, "detect_cgroup_memory_limit_bytes", no_detection)
        budget = resolve_budget(budget_bytes=512 * _MIB)
        assert budget.budget_bytes == 512 * _MIB
        assert budget.memory_limit == "512MB"

    def test_budget_floor_never_returns_absurdly_small_values(self, monkeypatch) -> None:
        monkeypatch.setattr(budget_mod, "detect_cgroup_memory_limit_bytes", lambda: None)
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


class TestResolveOocMemoryLimit:
    """FIX 1 (root cause 1): `resolve_ooc_memory_limit` is the out-of-core
    route's OWN generous, concurrency-aware DuckDB sizing -- deliberately
    separate from `resolve_budget`, whose conservative 0.25 fraction stays
    unchanged because the router's full-frame admission price
    (`_pipeline_routing_signals.py`) depends on that exact number and must
    stay conservative. This class pins the subtractive-reserve + concurrency
    model `_budget.py`'s module docstring documents.
    """

    def test_auto_budget_is_ceiling_minus_subtractive_reserve(self, monkeypatch) -> None:
        # No cgroup limit present: falls through to the host-RAM ceiling.
        # 8 GiB ceiling: fraction reserve (0.2 * 8 GiB = 1.6 GiB) is BELOW the
        # 2 GiB floor, so the floor wins -- reserve is 2 GiB, budget 6 GiB.
        monkeypatch.setattr(budget_mod, "detect_cgroup_memory_limit_bytes", lambda: None)
        monkeypatch.setattr(budget_mod, "detect_host_memory_bytes", lambda: 8 * _GIB)
        budget = resolve_ooc_memory_limit()
        assert budget.budget_bytes == 6 * _GIB
        # Default concurrency divisor is 4: 6 GiB / 4 = 1536 MiB.
        assert budget.memory_limit == "1536MB"

    def test_auto_budget_uses_the_fraction_when_it_exceeds_the_floor(self, monkeypatch) -> None:
        # 16 GiB ceiling: 0.2 * 16 GiB (3.2 GiB) exceeds the 2 GiB floor, so
        # the fraction wins -- budget is 80% of the ceiling, not a flat
        # ceiling-minus-2GiB.
        monkeypatch.setattr(budget_mod, "detect_cgroup_memory_limit_bytes", lambda: None)
        monkeypatch.setattr(budget_mod, "detect_host_memory_bytes", lambda: 16 * _GIB)
        budget = resolve_ooc_memory_limit()
        expected_reserve = max(int(16 * _GIB * 0.2), 2 * _GIB)
        assert budget.budget_bytes == 16 * _GIB - expected_reserve
        assert budget.budget_bytes > int(0.75 * 16 * _GIB)  # generous, not a quarter

    def test_uses_most_of_the_ceiling_not_a_quarter_of_it(self, monkeypatch) -> None:
        # The regression this whole fix exists for: on a host far bigger
        # than the reserve floor, DuckDB must get MOST of the ceiling, not
        # ~25% the way the old resolve_budget-shared fraction did.
        monkeypatch.setattr(budget_mod, "detect_cgroup_memory_limit_bytes", lambda: None)
        monkeypatch.setattr(budget_mod, "detect_host_memory_bytes", lambda: 32 * _GIB)
        budget = resolve_ooc_memory_limit()
        assert budget.budget_bytes >= int(0.75 * 32 * _GIB)

    def test_explicit_budget_skips_ceiling_detection(self, monkeypatch) -> None:
        def no_detection() -> int:
            raise AssertionError("explicit budget must not trigger host detection")

        monkeypatch.setattr(budget_mod, "detect_host_memory_bytes", no_detection)
        monkeypatch.setattr(budget_mod, "detect_cgroup_memory_limit_bytes", no_detection)
        budget = resolve_ooc_memory_limit(budget_bytes=512 * _MIB)
        assert budget.budget_bytes == 512 * _MIB
        # Default concurrency (4): 512 MiB / 4 = 128 MiB.
        assert budget.memory_limit == "128MB"

    def test_concurrency_divides_memory_limit_not_budget_bytes(self) -> None:
        conc1 = resolve_ooc_memory_limit(budget_bytes=8 * _GIB, max_concurrent_instances=1)
        conc4 = resolve_ooc_memory_limit(budget_bytes=8 * _GIB, max_concurrent_instances=4)
        # budget_bytes (used for batch_rows) is the UNDIVIDED total either way.
        assert conc1.budget_bytes == conc4.budget_bytes == 8 * _GIB
        assert conc1.batch_rows == conc4.batch_rows
        # memory_limit (the DuckDB per-instance cap) scales inversely with
        # concurrency so the SUM across live instances stays in budget.
        assert conc1.memory_limit == "8192MB"
        assert conc4.memory_limit == "2048MB"

    def test_default_concurrency_is_conservative_but_not_one(self) -> None:
        # No caller-supplied concurrency: the module's documented default
        # (sized from the real 100M-row benchmark's worst case of 2, with
        # headroom for wider FK fan-in) applies.
        default = resolve_ooc_memory_limit(budget_bytes=8 * _GIB)
        explicit = resolve_ooc_memory_limit(budget_bytes=8 * _GIB, max_concurrent_instances=4)
        assert default == explicit
        assert (
            default.memory_limit
            != resolve_ooc_memory_limit(
                budget_bytes=8 * _GIB, max_concurrent_instances=1
            ).memory_limit
        )

    def test_sum_of_per_instance_caps_never_exceeds_budget(self) -> None:
        # Dennis-1: the per-instance cap is a STRICT floor division, never
        # floored UP at _MIN_BUDGET_BYTES, so the sum of every live instance's
        # cap stays within budget_bytes rather than over-subscribing it. Here
        # 8 GiB / 5 = 1638 MiB each; 5 * 1638 MiB <= 8 GiB.
        conc = 5
        budget = resolve_ooc_memory_limit(budget_bytes=8 * _GIB, max_concurrent_instances=conc)
        per_instance_mib = int(budget.memory_limit.removesuffix("MB"))
        assert per_instance_mib * conc * _MIB <= budget.budget_bytes

    def test_tight_budget_takes_a_smaller_cap_not_an_oversubscribed_one(self) -> None:
        # A budget too small to give each instance _MIN_BUDGET_BYTES must yield
        # a SMALLER per-instance cap (invariant preserved), NOT the old 64 MiB
        # floor that over-subscribed. 128 MiB / 4 = 32 MiB each, and
        # 4 * 32 MiB == 128 MiB stays within budget.
        budget = resolve_ooc_memory_limit(budget_bytes=128 * _MIB, max_concurrent_instances=4)
        assert budget.memory_limit == "32MB"
        assert int(budget.memory_limit.removesuffix("MB")) * 4 * _MIB <= budget.budget_bytes

    def test_fan_in_67_on_64mib_budget_admits_at_one_mb(self) -> None:
        # ROUND-3 Fix C SUB-FIX 4: 67 is the exact safe boundary -- DuckDB's
        # decimal "1MB" floor for all 67 co-live instances still sums to
        # 67_000_000 B, within the 67_108_864 B (64 MiB) budget, so this
        # admits at "1MB" rather than raising.
        budget = resolve_ooc_memory_limit(budget_bytes=64 * _MIB, max_concurrent_instances=67)
        assert budget.memory_limit == "1MB"
        assert 64 * _MIB >= 67 * 1_000_000

    def test_fan_in_68_on_64mib_budget_raises_fanin_exceeds_budget(self) -> None:
        # A genuine over-commit: even DuckDB's decimal "1MB" floor for 68
        # co-live instances sums to 68_000_000 B, over the 67_108_864 B (64
        # MiB) budget -- the old `max(1, ...)` floor silently admitted this
        # (over-committing the sum-of-caps invariant); SUB-FIX 4 now fails
        # closed instead, matching `_memory_estimate._per_instance_mib`.
        with pytest.raises(ExecutionError) as excinfo:
            resolve_ooc_memory_limit(budget_bytes=64 * _MIB, max_concurrent_instances=68)
        assert excinfo.value.code == "out_of_core_fanin_exceeds_budget"

    def test_rejects_concurrency_below_one(self) -> None:
        with pytest.raises(ExecutionError) as excinfo:
            resolve_ooc_memory_limit(budget_bytes=1 * _GIB, max_concurrent_instances=0)
        assert excinfo.value.code == "out_of_core_concurrency_invalid"

    def test_rejects_nonpositive_budget(self) -> None:
        with pytest.raises(ExecutionError) as excinfo:
            resolve_ooc_memory_limit(budget_bytes=0)
        assert excinfo.value.code == "out_of_core_budget_invalid"

    def test_rejects_negative_reserved_bytes(self) -> None:
        with pytest.raises(ExecutionError) as excinfo:
            resolve_ooc_memory_limit(budget_bytes=1 * _GIB, reserved_bytes=-1)
        assert excinfo.value.code == "out_of_core_reserved_bytes_invalid"

    def test_does_not_share_state_or_derivation_with_resolve_budget(self, monkeypatch) -> None:
        # The router prices full-frame admission off resolve_budget's own
        # 0.25 fraction; resolve_ooc_memory_limit must never change that
        # function's return for the same inputs.
        monkeypatch.setattr(budget_mod, "detect_cgroup_memory_limit_bytes", lambda: None)
        monkeypatch.setattr(budget_mod, "detect_host_memory_bytes", lambda: 8 * _GIB)
        shared = resolve_budget()
        assert shared.budget_bytes == 2 * _GIB  # unchanged 0.25 fraction
        assert shared.memory_limit == "2048MB"


class TestResolveBudgetPrefersCgroup:
    """Sprint 1b: `resolve_budget`'s auto-detect path prefers the cgroup
    effective limit over raw host RAM (plan §3.1)."""

    def test_auto_budget_uses_cgroup_limit_when_present(self, monkeypatch) -> None:
        monkeypatch.setattr(budget_mod, "detect_cgroup_memory_limit_bytes", lambda: 4 * _GIB)

        def no_host_detection() -> int:
            raise AssertionError("cgroup limit present: host RAM must not be consulted")

        monkeypatch.setattr(budget_mod, "detect_host_memory_bytes", no_host_detection)
        budget = resolve_budget()
        assert budget.budget_bytes == 1 * _GIB  # 0.25 fraction of the 4 GiB cgroup limit

    def test_auto_budget_falls_back_to_host_ram_without_a_cgroup_limit(self, monkeypatch) -> None:
        monkeypatch.setattr(budget_mod, "detect_cgroup_memory_limit_bytes", lambda: None)
        monkeypatch.setattr(budget_mod, "detect_host_memory_bytes", lambda: 16 * _GIB)
        budget = resolve_budget()
        assert budget.budget_bytes == 4 * _GIB


class TestResolveBudgetSlotAware:
    """Sprint 1b: `reserved_bytes` charges co-running slots so the return is
    this job's SLOT budget, not the whole cgroup (plan §6, Sprint 1b)."""

    def test_reserved_bytes_shrinks_the_slot_budget(self) -> None:
        whole = resolve_budget(budget_bytes=8 * _GIB)
        slot = resolve_budget(budget_bytes=8 * _GIB, reserved_bytes=2 * _GIB)
        assert slot.budget_bytes == whole.budget_bytes - 2 * _GIB

    def test_reserved_bytes_defaults_to_zero_for_backward_compatibility(self) -> None:
        assert resolve_budget(budget_bytes=1 * _GIB) == resolve_budget(
            budget_bytes=1 * _GIB, reserved_bytes=0
        )

    def test_floor_holds_when_reserved_nearly_exhausts_the_budget(self) -> None:
        budget = resolve_budget(budget_bytes=100 * _MIB, reserved_bytes=99 * _MIB)
        assert budget.budget_bytes == 64 * _MIB
        assert budget.memory_limit == "64MB"

    def test_rejects_negative_reserved_bytes(self) -> None:
        with pytest.raises(ExecutionError) as excinfo:
            resolve_budget(budget_bytes=1 * _GIB, reserved_bytes=-1)
        assert excinfo.value.code == "out_of_core_reserved_bytes_invalid"


class TestCgroupV2Read:
    """v2 `memory.max`: numeric bytes, or the literal `"max"` for unlimited
    (cgroup-v2.txt), read from a process's resolved leaf directory."""

    def test_reads_numeric_memory_max(self, tmp_path, monkeypatch) -> None:
        mount = tmp_path / "cgroup_v2"
        leaf = mount / "user.slice"
        leaf.mkdir(parents=True)
        (leaf / "memory.max").write_text("1073741824\n")
        proc_self_cgroup = tmp_path / "self_cgroup"
        proc_self_cgroup.write_text("0::/user.slice\n")
        monkeypatch.setattr(budget_mod, "_CGROUP_V2_MOUNT", mount)
        monkeypatch.setattr(budget_mod, "_PROC_SELF_CGROUP", proc_self_cgroup)
        assert budget_mod.detect_cgroup_memory_limit_bytes() == 1 * _GIB

    def test_literal_max_is_unlimited_and_yields_no_cgroup_limit(
        self, tmp_path, monkeypatch
    ) -> None:
        mount = tmp_path / "cgroup_v2"
        leaf = mount / "user.slice"
        leaf.mkdir(parents=True)
        (leaf / "memory.max").write_text("max\n")
        proc_self_cgroup = tmp_path / "self_cgroup"
        proc_self_cgroup.write_text("0::/user.slice\n")
        monkeypatch.setattr(budget_mod, "_CGROUP_V2_MOUNT", mount)
        monkeypatch.setattr(budget_mod, "_PROC_SELF_CGROUP", proc_self_cgroup)
        monkeypatch.setattr(budget_mod, "_CGROUP_V1_MEMORY_MOUNT", tmp_path / "no-v1")
        assert budget_mod.detect_cgroup_memory_limit_bytes() is None

    def test_unlimited_v2_falls_back_to_host_ram_end_to_end(self, tmp_path, monkeypatch) -> None:
        mount = tmp_path / "cgroup_v2"
        leaf = mount / "user.slice"
        leaf.mkdir(parents=True)
        (leaf / "memory.max").write_text("max\n")
        proc_self_cgroup = tmp_path / "self_cgroup"
        proc_self_cgroup.write_text("0::/user.slice\n")
        monkeypatch.setattr(budget_mod, "_CGROUP_V2_MOUNT", mount)
        monkeypatch.setattr(budget_mod, "_PROC_SELF_CGROUP", proc_self_cgroup)
        monkeypatch.setattr(budget_mod, "_CGROUP_V1_MEMORY_MOUNT", tmp_path / "no-v1")
        monkeypatch.setattr(budget_mod, "detect_host_memory_bytes", lambda: 8 * _GIB)
        assert budget_mod.detect_effective_memory_bytes() == 8 * _GIB


class TestCgroupV1Read:
    """v1 `memory.limit_in_bytes`: numeric bytes, or a near-LLONG_MAX
    sentinel for unlimited (cgroup-v1/memory.txt), used only when no v2
    limit is found."""

    def test_reads_numeric_limit_in_bytes(self, tmp_path, monkeypatch) -> None:
        v1_mount = tmp_path / "cgroup_v1_memory"
        leaf = v1_mount / "docker" / "abc123"
        leaf.mkdir(parents=True)
        (leaf / "memory.limit_in_bytes").write_text("536870912\n")
        proc_self_cgroup = tmp_path / "self_cgroup"
        proc_self_cgroup.write_text("5:memory:/docker/abc123\n")
        monkeypatch.setattr(budget_mod, "_CGROUP_V2_MOUNT", tmp_path / "no-v2")
        monkeypatch.setattr(budget_mod, "_CGROUP_V1_MEMORY_MOUNT", v1_mount)
        monkeypatch.setattr(budget_mod, "_PROC_SELF_CGROUP", proc_self_cgroup)
        assert budget_mod.detect_cgroup_memory_limit_bytes() == 512 * _MIB

    @pytest.mark.parametrize(
        "sentinel",
        [
            "9223372036854771712",  # LLONG_MAX & ~(4 KiB - 1), x86-64
            "9223372036854710272",  # LLONG_MAX & ~(64 KiB - 1), ppc64le / some arm64
            "9223372036854775807",  # raw LLONG_MAX
        ],
    )
    def test_unlimited_sentinel_is_treated_as_no_limit(
        self, tmp_path, monkeypatch, sentinel
    ) -> None:
        # The v1 "unlimited" sentinel is page-size-specific; every page-size
        # variant must read as "no limit", not an ~8 EiB real budget.
        v1_mount = tmp_path / "cgroup_v1_memory"
        leaf = v1_mount / "docker" / "abc123"
        leaf.mkdir(parents=True)
        (leaf / "memory.limit_in_bytes").write_text(f"{sentinel}\n")
        proc_self_cgroup = tmp_path / "self_cgroup"
        proc_self_cgroup.write_text("5:memory:/docker/abc123\n")
        monkeypatch.setattr(budget_mod, "_CGROUP_V2_MOUNT", tmp_path / "no-v2")
        monkeypatch.setattr(budget_mod, "_CGROUP_V1_MEMORY_MOUNT", v1_mount)
        monkeypatch.setattr(budget_mod, "_PROC_SELF_CGROUP", proc_self_cgroup)
        assert budget_mod.detect_cgroup_memory_limit_bytes() is None

    def test_a_real_large_limit_below_the_sentinel_is_kept(self, tmp_path, monkeypatch) -> None:
        # A genuine (if large) limit must NOT be swallowed by the sentinel band.
        v1_mount = tmp_path / "cgroup_v1_memory"
        leaf = v1_mount / "docker" / "abc123"
        leaf.mkdir(parents=True)
        (leaf / "memory.limit_in_bytes").write_text("34359738368\n")  # 32 GiB
        proc_self_cgroup = tmp_path / "self_cgroup"
        proc_self_cgroup.write_text("5:memory:/docker/abc123\n")
        monkeypatch.setattr(budget_mod, "_CGROUP_V2_MOUNT", tmp_path / "no-v2")
        monkeypatch.setattr(budget_mod, "_CGROUP_V1_MEMORY_MOUNT", v1_mount)
        monkeypatch.setattr(budget_mod, "_PROC_SELF_CGROUP", proc_self_cgroup)
        assert budget_mod.detect_cgroup_memory_limit_bytes() == 32 * 1024 * _MIB

    def test_unreadable_cgroup_file_falls_back_to_no_limit(self, tmp_path) -> None:
        # A permission-denied / unreadable cgroup file must degrade to None
        # (host-RAM fallback), never raise out of budget detection. Reading a
        # directory raises IsADirectoryError (an OSError), exercising that branch.
        unreadable = tmp_path / "memory.max"
        unreadable.mkdir()
        assert budget_mod._read_cgroup_bytes_value(unreadable) is None


class TestCgroupNestedHierarchyMin:
    """A parent cgroup's `memory.max` bounds every descendant, so the
    effective limit is the min across the leaf..root chain, not just the
    leaf's own file."""

    def test_min_up_the_hierarchy_wins(self, tmp_path) -> None:
        root = tmp_path / "cgroup"
        parent = root / "parent"
        child = parent / "child"
        child.mkdir(parents=True)
        (parent / "memory.max").write_text("536870912")  # 512 MiB, tighter
        (child / "memory.max").write_text("1073741824")  # 1 GiB, looser
        assert budget_mod._cgroup_v2_effective_max_bytes(child, root) == 512 * _MIB

    def test_unlimited_levels_are_skipped_in_the_min(self, tmp_path) -> None:
        root = tmp_path / "cgroup"
        parent = root / "parent"
        child = parent / "child"
        child.mkdir(parents=True)
        (parent / "memory.max").write_text("max")
        (child / "memory.max").write_text("734003200")
        assert budget_mod._cgroup_v2_effective_max_bytes(child, root) == 734003200

    def test_all_unlimited_yields_none(self, tmp_path) -> None:
        root = tmp_path / "cgroup"
        parent = root / "parent"
        child = parent / "child"
        child.mkdir(parents=True)
        (parent / "memory.max").write_text("max")
        (child / "memory.max").write_text("max")
        assert budget_mod._cgroup_v2_effective_max_bytes(child, root) is None


class TestDiskSpillPreflight:
    """Does the scratch path's free disk cover a predicted footprint? Pure
    check-the-numbers unit here; the estimator (`predict_ooc_disk_bytes`)
    and the routing call site (`enforce_ooc_disk_preflight`, OOC-D) live in
    the sibling `out_of_core/_spill_estimate.py` and are covered in
    `test_out_of_core_spill_preflight.py`."""

    def test_passes_with_headroom_when_free_space_covers_the_spill(self, tmp_path) -> None:
        result = budget_mod.check_disk_spill_preflight(tmp_path, predicted_spill_bytes=1024)
        assert result.ok is True
        assert result.headroom_bytes == result.free_bytes - 1024
        assert result.predicted_bytes == 1024

    def test_fails_when_predicted_spill_exceeds_free_space(self, tmp_path) -> None:
        huge = budget_mod.shutil.disk_usage(tmp_path).free + (10 * _GIB)
        result = budget_mod.check_disk_spill_preflight(tmp_path, predicted_spill_bytes=huge)
        assert result.ok is False
        assert result.headroom_bytes < 0

    def test_checks_the_nearest_existing_ancestor_when_path_does_not_exist_yet(
        self, tmp_path
    ) -> None:
        not_yet_created = tmp_path / "job-scratch" / "nested"
        result = budget_mod.check_disk_spill_preflight(not_yet_created, predicted_spill_bytes=0)
        assert result.ok is True

    def test_rejects_negative_predicted_spill_bytes(self, tmp_path) -> None:
        with pytest.raises(ExecutionError) as excinfo:
            budget_mod.check_disk_spill_preflight(tmp_path, predicted_spill_bytes=-1)
        assert excinfo.value.code == "out_of_core_predicted_spill_bytes_invalid"


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
        from decoy_engine.execution.out_of_core import _stream_driver as driver_mod

        paths = write_large_fk_chain(tmp_path / "src", 400, width=1, batch_rows=200)
        sizes: list[int] = []
        real_mask_batch = driver_mod.mask_batch

        def spy(*args: object, **kwargs: object) -> pa.RecordBatch:
            sizes.append(args[2].num_rows)  # type: ignore[union-attr]
            return real_mask_batch(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(driver_mod, "mask_batch", spy)
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
