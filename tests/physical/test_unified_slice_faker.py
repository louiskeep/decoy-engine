"""C5a acceptance: the deterministic-reuse pooled Faker column on the unified slice.

The unified slice (`_unified_slice.py`) must run an in-scope Faker column
through the 4.4 shadow coordinator and return output identical to the pandas
full-frame oracle (`FakerStrategyHandler.run`). Plan:
`docs/plans/2026-10-05-c5a-unified-slice-faker.md` rev 4.1, section 5.

Two rules run through every test here:

- Non-vacuity. Every positive lane case poisons `PandasExecutionAdapter.run`
  for the duration of the lane run, so a silent decline to the oracle cannot
  pass. The reference output always comes from a separate lane-off run.
- Strict equality. Lane-vs-oracle compares reuse the strictest helpers in
  `test_unified_slice_parity.py` (values, Arrow field types, column order,
  `b"pandas"` schema metadata, warnings, row errors, every non-leaf
  quality-metrics key).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import _pandas_adapter, _unified_slice_admission, run_pipeline
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY, UnifiedSliceInvariantError
from decoy_engine.execution.native._companion_status import (
    KernelAvailability,
    native_companion_status,
)
from decoy_engine.execution.native._index_ext import load_compiled_index_kernel
from decoy_engine.execution.physical import _shadow_coordinator
from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
from decoy_engine.execution.physical._shadow_diff_codes import CELL_VALUE_DIFF, ShadowDifference
from decoy_engine.generation.pool import PoolBuilder, PoolCache
from decoy_engine.generation.pool._errors import PoolCapacityError
from decoy_engine.generation.pool._value_pool import estimate_pool_bytes
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.providers_v2 import CapabilityMatrix, ProviderRegistry, get_default_registry
from decoy_engine.providers_v2._errors import ProviderError
from decoy_engine.relationships._namespace import NamespaceConfigError
from tests.physical._shadow_helpers import (
    assert_every_node_bound,
    assert_shadow_matches_oracle,
    build_config,
    run_shadow_and_oracle,
    write_read_only_fixture,
)
from tests.physical.test_unified_slice_parity import _assert_full_parity

ENGINE_VERSION = "c5a-unified-faker-test"
FAKER_OP = "native_faker_select"
_MASK_KEY = bytes(range(32))

NEEDS_COMPANION = pytest.mark.skipif(
    not native_companion_status().ok,
    reason="compiled decoy-engine-native companion unavailable",
)


def _key_provider(secret: bytes = _MASK_KEY) -> SecretKeyProvider:
    return SecretKeyProvider(secret=secret, key_version="v1")


def faker_column(
    name: str = "c",
    *,
    provider: str = "person_first_name",
    namespace: str = "ns_faker",
    pool_size: int = 30,
    **extra: Any,
) -> dict[str, Any]:
    """The one in-scope shape: deterministic, reuse (the config default once
    deterministic is set), explicit namespace, explicit pool_size."""
    col: dict[str, Any] = {
        "name": name,
        "strategy": "faker",
        "provider": provider,
        "deterministic": True,
        "namespace": namespace,
        "pool_size": pool_size,
    }
    col.update(extra)
    return col


def string_source(n: int = 40, *, mod: int = 7, null_every: int | None = None) -> pa.Table:
    values = [None if null_every and i % null_every == 0 else f"src_{i % mod}" for i in range(n)]
    return pa.table({"c": pa.array(values, type=pa.string())})


class Case:
    """One config + resident source, runnable on either route with the same
    inputs. The source object itself is passed to both arms (a `pa.Table` is
    immutable), so a ragged multi-chunk source survives intact."""

    def __init__(
        self,
        tmp_path: Path,
        source: pa.Table,
        columns: list[dict[str, Any]],
        *,
        seed: int = 20260914,
        mutate: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        path = write_read_only_fixture(tmp_path, source, "fixture")
        config = build_config(tmp_path, "t", path, columns, seed=seed)
        if mutate is not None:
            mutate(config)
        self.config = config
        self.source = source

    def run(
        self,
        *,
        lane: bool,
        registry: ProviderRegistry | None = None,
        secret: bytes = _MASK_KEY,
        **kwargs: Any,
    ) -> ExecutionResult:
        return run_pipeline(
            self.config,
            {"t": self.source},
            engine_version=ENGINE_VERSION,
            key_provider=_key_provider(secret),
            registry=registry,
            unified_slice_enabled=lane,
            **kwargs,
        )


@contextmanager
def poisoned_oracle() -> Iterator[None]:
    def _boom(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the legacy oracle ran on a run that must stay on the unified lane")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_pandas_adapter.PandasExecutionAdapter, "run", _boom)
        yield


def lane_run(case: Case, **kwargs: Any) -> ExecutionResult:
    """The lane run, with the oracle poisoned (non-vacuity rule)."""
    with poisoned_oracle():
        return case.run(lane=True, **kwargs)


def assert_lane_parity(case: Case, **kwargs: Any) -> dict[str, Any]:
    off = case.run(lane=False, **kwargs)
    on = lane_run(case, **kwargs)
    return _assert_full_parity(off, on)


def evidence_by_operator(leaf: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(leaf["nodes"].values(), key=lambda e: e["operator"])


class _KernelSpy:
    """Wraps the real compiled index kernel; records every batch call."""

    def __init__(self) -> None:
        self.inner = load_compiled_index_kernel()
        self.calls: list[dict[str, Any]] = []

    def derive_index_batch(
        self,
        values: Any,
        *,
        mask_key: bytes,
        namespace: str,
        pool_size: int,
        native_threads: int | None = None,
    ) -> Any:
        self.calls.append(
            {"rows": len(values), "namespace": namespace, "native_threads": native_threads}
        )
        return self.inner.derive_index_batch(
            values,
            mask_key=mask_key,
            namespace=namespace,
            pool_size=pool_size,
            native_threads=native_threads,
        )


def install_kernel_spy(monkeypatch: pytest.MonkeyPatch) -> _KernelSpy:
    spy = _KernelSpy()
    monkeypatch.setattr(_shadow_coordinator, "load_compiled_index_kernel", lambda: spy)
    return spy


class CountingAdapter:
    """A rebound provider adapter that counts pool builds (one `generate_batch`
    call per `PoolBuilder.build`) and yields values from `make`, or raises
    `fail` from the build."""

    backend_type = "faker"
    backend_version = "counting-1"

    def __init__(self, make: Callable[[int], Any], *, fail: Exception | None = None) -> None:
        self.make = make
        self.fail = fail
        self.builds = 0

    def generate(self, provider: str, *, spec: object, source_value: bytes | None = None) -> Any:
        return self.make(0)

    def generate_batch(self, provider: str, *, spec: object, count: int) -> list[Any]:
        self.builds += 1
        if self.fail is not None:
            raise self.fail
        return [self.make(i) for i in range(count)]

    def capability_matrix(self, provider: str) -> CapabilityMatrix:
        return get_default_registry().get_capabilities(provider)


def rebound_registry(adapter: CountingAdapter) -> ProviderRegistry:
    default = get_default_registry()
    return default.override(
        "person_first_name", adapter, default.get_capabilities("person_first_name")
    )


# ---------------------------------------------------------------------------
# 1. Admits and runs native.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_1_admits_and_runs_native(tmp_path: Path) -> None:
    source = pa.table(
        {
            "c": pa.array([f"src_{i % 7}" for i in range(40)], type=pa.string()),
            "p": pa.array([f"keep_{i}" for i in range(40)], type=pa.string()),
        }
    )
    case = Case(tmp_path, source, [faker_column(), {"name": "p", "strategy": "passthrough"}])
    leaf = assert_lane_parity(case)
    faker_evidence = [e for e in leaf["nodes"].values() if e["operator"] == FAKER_OP]
    assert faker_evidence == [
        {"operator": FAKER_OP, "executed": True, "compiled_kernel_executed": True}
    ]


# ---------------------------------------------------------------------------
# 2. Mixed operators.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_2_mixed_operators(tmp_path: Path) -> None:
    n = 40
    source = pa.table(
        {
            "f": pa.array([f"src_{i % 7}" for i in range(n)], type=pa.string()),
            "h": pa.array([f"user{i}@example.com" for i in range(n)], type=pa.string()),
            "k": pa.array([f"cat_{i % 3}" for i in range(n)], type=pa.string()),
            "r": pa.array([f"5{i:03d}-11-2222" for i in range(n)], type=pa.string()),
        }
    )
    columns = [
        faker_column("f"),
        {"name": "h", "strategy": "hash", "namespace": "ns_h"},
        {
            "name": "k",
            "strategy": "categorical",
            "namespace": "ns_k",
            "deterministic": True,
            "provider_config": {"categories": ["a", "b", "c"]},
        },
        {"name": "r", "strategy": "redact"},
    ]
    leaf = assert_lane_parity(Case(tmp_path, source, columns))
    assert [e["operator"] for e in evidence_by_operator(leaf)] == sorted(
        [FAKER_OP, "native_keyed_hash", "native_categorical", "native_redact"]
    )
    assert all(e["executed"] for e in leaf["nodes"].values())


# ---------------------------------------------------------------------------
# 3 / 4. Pool identity sharing.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_3_shared_pool_identity_builds_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = pa.table(
        {
            "c1": pa.array(["a", "b", "c", "a", None, "d"], type=pa.string()),
            "c2": pa.array(["a", "b", "c", "a", None, "d"], type=pa.string()),
        }
    )
    case = Case(
        tmp_path,
        source,
        [faker_column("c1", namespace="ns_shared"), faker_column("c2", namespace="ns_shared")],
    )
    off = case.run(lane=False)
    real_build = PoolBuilder.build
    builds: list[int] = []

    def _counting_build(self: PoolBuilder, *args: Any, **kwargs: Any) -> Any:
        builds.append(1)
        return real_build(self, *args, **kwargs)

    monkeypatch.setattr(PoolBuilder, "build", _counting_build)
    on = lane_run(case)
    _assert_full_parity(off, on)
    assert builds == [1]


@NEEDS_COMPANION
def test_4_different_namespaces(tmp_path: Path) -> None:
    source = pa.table(
        {
            "c1": pa.array([f"src_{i % 6}" for i in range(30)], type=pa.string()),
            "c2": pa.array([f"src_{i % 6}" for i in range(30)], type=pa.string()),
        }
    )
    case = Case(
        tmp_path,
        source,
        [faker_column("c1", namespace="ns_one"), faker_column("c2", namespace="ns_two")],
    )
    assert_lane_parity(case)
    on = lane_run(case)
    assert on.outputs["t"].column("c1").to_pylist() != on.outputs["t"].column("c2").to_pylist()


# ---------------------------------------------------------------------------
# 5. Nulls, all-null, zero rows.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_5_interleaved_nulls_kept_by_position(tmp_path: Path) -> None:
    source = string_source(40, null_every=3)
    case = Case(tmp_path, source, [faker_column()])
    assert_lane_parity(case)
    out = lane_run(case).outputs["t"].column("c").to_pylist()
    assert [v is None for v in out] == [v is None for v in source.column("c").to_pylist()]


@NEEDS_COMPANION
def test_5_all_null_column(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array([None, None, None, None], type=pa.string())})
    assert_lane_parity(Case(tmp_path, source, [faker_column()]))


@NEEDS_COMPANION
def test_5_zero_row_table_is_float64_like_the_oracle(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array([], type=pa.string())})
    case = Case(tmp_path, source, [faker_column()])
    off = case.run(lane=False)
    on = lane_run(case)
    _assert_full_parity(off, on)
    assert on.outputs["t"].schema.field("c").type == pa.float64()
    assert off.outputs["t"].schema.field("c").type == pa.float64()


# ---------------------------------------------------------------------------
# 6. Batch boundaries.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_6_production_lane_crosses_the_50k_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = string_source(50_001, mod=97)
    case = Case(tmp_path, source, [faker_column()])
    off = case.run(lane=False)
    spy = install_kernel_spy(monkeypatch)
    on = lane_run(case)
    _assert_full_parity(off, on)
    assert [c["rows"] for c in spy.calls] == [50_000, 1]


@NEEDS_COMPANION
def test_6_ragged_multi_chunk_source(tmp_path: Path) -> None:
    values = [None if i % 11 == 0 else f"src_{i % 13}" for i in range(7_000)]
    arr = pa.array(values, type=pa.string())
    chunked = pa.chunked_array([arr[:10], arr[10:2_500], arr[2_500:2_501], arr[2_501:]])
    assert chunked.num_chunks == 4
    source = pa.Table.from_arrays([chunked], names=["c"])
    assert_lane_parity(Case(tmp_path, source, [faker_column()]))


@NEEDS_COMPANION
@pytest.mark.parametrize("batch_size", [7, 1000, None], ids=["batch_7", "batch_1000", "default"])
def test_6_coordinator_seam_batch_sizes(tmp_path: Path, batch_size: int | None) -> None:
    source = string_source(2_345, mod=41, null_every=9)
    path = write_read_only_fixture(tmp_path, source, "seam")
    config = build_config(tmp_path, "t", path, [faker_column()])
    kwargs: dict[str, Any] = {} if batch_size is None else {"batch_size_rows": batch_size}
    run = run_shadow_and_oracle(config, "t", source, key_provider=_key_provider(), **kwargs)
    assert_every_node_bound(run.plan)
    assert_shadow_matches_oracle(run)


# ---------------------------------------------------------------------------
# 7. Thread invariance and thread-budget forwarding (3g).
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_7_faker_thread_invariance_and_forwarding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = Case(tmp_path, string_source(300, mod=23, null_every=10), [faker_column()])
    spy = install_kernel_spy(monkeypatch)
    one = lane_run(case, native_threads=1)
    assert {c["native_threads"] for c in spy.calls} == {1}
    spy.calls.clear()
    four = lane_run(case, native_threads=4)
    assert spy.calls, "the compiled kernel was never called on the second run"
    assert {c["native_threads"] for c in spy.calls} == {4}
    assert one.outputs["t"].equals(four.outputs["t"], check_metadata=True)


@NEEDS_COMPANION
def test_7_non_faker_index_operator_also_receives_the_thread_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = pa.table({"k": pa.array([f"cat_{i % 3}" for i in range(60)], type=pa.string())})
    columns = [
        {
            "name": "k",
            "strategy": "categorical",
            "namespace": "ns_k",
            "deterministic": True,
            "provider_config": {"categories": ["a", "b", "c"]},
        }
    ]
    case = Case(tmp_path, source, columns)
    spy = install_kernel_spy(monkeypatch)
    one = lane_run(case, native_threads=1)
    spy.calls.clear()
    four = lane_run(case, native_threads=4)
    assert spy.calls, "the compiled kernel was never called on the second run"
    assert {c["native_threads"] for c in spy.calls} == {4}
    assert one.outputs["t"].equals(four.outputs["t"], check_metadata=True)


# ---------------------------------------------------------------------------
# 8. Locale and pool size.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_8_non_default_locale(tmp_path: Path) -> None:
    columns = [faker_column(provider_config={"locale": "de_DE"})]
    assert_lane_parity(Case(tmp_path, string_source(60, mod=17), columns))


@NEEDS_COMPANION
@pytest.mark.parametrize("pool_size", [5, 200])
def test_8_pool_sizes(tmp_path: Path, pool_size: int) -> None:
    columns = [faker_column(pool_size=pool_size)]
    assert_lane_parity(Case(tmp_path, string_source(80, mod=31), columns))


# ---------------------------------------------------------------------------
# 9. Determinism.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_9_determinism_mask_key_and_job_seed(tmp_path: Path) -> None:
    source = string_source(200, mod=50)
    case = Case(tmp_path, source, [faker_column(pool_size=64)])
    first = lane_run(case)
    second = lane_run(case)
    assert first.outputs["t"].equals(second.outputs["t"], check_metadata=True)

    other_key = lane_run(case, secret=bytes(reversed(range(32))))
    assert (
        other_key.outputs["t"].column("c").to_pylist() != first.outputs["t"].column("c").to_pylist()
    )

    # job_seed governs pool CONTENT: the set of values the column can draw from
    # changes, not only which one a row picks.
    (tmp_path / "s2").mkdir()
    other_seed = Case(tmp_path / "s2", source, [faker_column(pool_size=64)], seed=777)
    seeded = lane_run(other_seed)
    assert set(seeded.outputs["t"].column("c").to_pylist()) != set(
        first.outputs["t"].column("c").to_pylist()
    )


# ---------------------------------------------------------------------------
# 10. Declines to the oracle, job output equals the lane-off run.
# ---------------------------------------------------------------------------


def _when(config: dict[str, Any]) -> None:
    config["tables"][0]["columns"][0]["when"] = "c == 'src_1'"


_DECLINE_CASES: dict[str, tuple[pa.Table, list[dict[str, Any]], Callable[..., None] | None]] = {
    "non_deterministic": (
        string_source(),
        [
            {
                "name": "c",
                "strategy": "faker",
                "provider": "person_first_name",
                "namespace": "ns_faker",
                "pool_size": 30,
            }
        ],
        None,
    ),
    "cardinality_not_reuse": (
        string_source(),
        [faker_column(cardinality_mode="match_source_cardinality")],
        None,
    ),
    "missing_pool_size": (
        string_source(),
        [
            {
                "name": "c",
                "strategy": "faker",
                "provider": "person_first_name",
                "deterministic": True,
                "namespace": "ns_faker",
            }
        ],
        None,
    ),
    "provider_outside_allowlist": (
        string_source(),
        [faker_column(provider="person_email")],
        None,
    ),
    "when_set": (string_source(), [faker_column()], _when),
    "vault_set": (string_source(), [faker_column(vault=True)], None),
    "large_string_source": (
        pa.table({"c": pa.array([f"src_{i % 7}" for i in range(40)], type=pa.large_string())}),
        [faker_column()],
        None,
    ),
    "int64_source": (
        pa.table({"c": pa.array(list(range(40)), type=pa.int64())}),
        [faker_column()],
        None,
    ),
}


@pytest.mark.parametrize("name", sorted(_DECLINE_CASES))
def test_10_declines_to_the_oracle(tmp_path: Path, name: str) -> None:
    source, columns, mutate = _DECLINE_CASES[name]
    case = Case(tmp_path, source, columns, mutate=mutate)
    off = case.run(lane=False)
    on = case.run(lane=True)
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert on.outputs["t"].equals(off.outputs["t"], check_metadata=True)
    assert tuple(on.warnings) == tuple(off.warnings)


def test_10_missing_namespace_is_rejected_identically_before_routing(tmp_path: Path) -> None:
    """A deterministic column without a namespace never reaches the lane: the
    namespace registry rejects it first, on both routes, with the same error."""
    column = {
        "name": "c",
        "strategy": "faker",
        "provider": "person_first_name",
        "deterministic": True,
        "pool_size": 30,
    }
    case = Case(tmp_path, string_source(), [column])
    with pytest.raises(NamespaceConfigError) as off_info:
        case.run(lane=False)
    with pytest.raises(NamespaceConfigError) as on_info:
        case.run(lane=True)
    assert str(on_info.value) == str(off_info.value)


@NEEDS_COMPANION
def test_10_declines_when_the_index_kernel_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = Case(tmp_path, string_source(), [faker_column()])
    off = case.run(lane=False)
    monkeypatch.setattr(
        _unified_slice_admission,
        "native_kernel_availability",
        lambda: KernelAvailability(crypto=True, index=False, raw_hex=True),
    )
    on = case.run(lane=True)
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert on.outputs["t"].equals(off.outputs["t"], check_metadata=True)


def test_10_declines_an_fk_participating_table(tmp_path: Path) -> None:
    parent = pa.table(
        {
            "id": pa.array(["p1", "p2", "p3"], type=pa.string()),
            "name_p": pa.array(["alice", "bob", "carol"], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "pid": pa.array(["p1", "p2", "p1"], type=pa.string()),
            "name_c": pa.array(["dave", "erin", "frank"], type=pa.string()),
        }
    )
    pq.write_table(parent, tmp_path / "parent.parquet")
    pq.write_table(child, tmp_path / "child.parquet")
    raw = {
        "version": 1,
        "global_settings": {"seed": 5},
        "sources": {
            n: {"type": "file", "format": "parquet", "path": str(tmp_path / f"{n}.parquet")}
            for n in ("parent", "child")
        },
        "targets": {
            n: {
                "type": "file",
                "format": "parquet",
                "path": str(tmp_path / f"{n}.out.parquet"),
            }
            for n in ("parent", "child")
        },
        "tables": [
            {
                "name": "parent",
                "columns": [
                    {"name": "id", "strategy": "passthrough"},
                    faker_column("name_p", namespace="ns_p"),
                ],
            },
            {
                "name": "child",
                "columns": [
                    {"name": "pid", "strategy": "passthrough"},
                    faker_column("name_c", namespace="ns_c"),
                ],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["pid"]}],
                "orphan_policy": "preserve",
                "namespace": "fk_ns",
            }
        ],
    }
    config = PipelineConfig.model_validate(raw).model_dump()
    sources = {"parent": parent, "child": child}
    off = run_pipeline(
        config,
        sources,
        engine_version=ENGINE_VERSION,
        key_provider=_key_provider(),
        unified_slice_enabled=False,
    )
    on = run_pipeline(
        config,
        sources,
        engine_version=ENGINE_VERSION,
        key_provider=_key_provider(),
        unified_slice_enabled=True,
    )
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    for table in ("parent", "child"):
        assert on.outputs[table].equals(off.outputs[table], check_metadata=True)


# ---------------------------------------------------------------------------
# 11. Single build under a rebound provider (3c).
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_11_rebound_non_string_provider_reroutes_with_one_build(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    case = Case(tmp_path, string_source(40, mod=9), [faker_column()])
    off_adapter = CountingAdapter(lambda i: i)
    off = case.run(lane=False, registry=rebound_registry(off_adapter))
    assert off_adapter.builds == 1

    adapter = CountingAdapter(lambda i: i)
    with caplog.at_level("INFO", logger="decoy_engine.execution._unified_slice"):
        on = case.run(lane=True, registry=rebound_registry(adapter))
    assert adapter.builds == 1, "the lane's build and the oracle's build must be one build"
    # Pin the path taken: the reroute came from FAKER_POOL_NON_STRING_OUTPUT, not another decline.
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("unified_slice_faker_non_string_pool_reroute") for m in messages)
    assert not any(m.startswith("unified_slice_unexpected_exception_reroute") for m in messages)
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert on.outputs["t"].equals(off.outputs["t"], check_metadata=True)


@NEEDS_COMPANION
def test_11_v2_custom_function_override_builds_once(tmp_path: Path) -> None:
    from decoy_engine.providers_v2._faker_adapter import (
        _unregister_faker_provider_v2,
        register_faker_provider_v2,
    )

    case = Case(tmp_path, string_source(40, mod=9), [faker_column()])
    calls: list[int] = []

    def _custom(fake: Any) -> int:
        calls.append(1)
        return 7

    register_faker_provider_v2("person_first_name", _custom)
    try:
        off = case.run(lane=False)
        per_build = len(calls)
        assert per_build > 0
        calls.clear()
        on = case.run(lane=True)
        assert len(calls) == per_build, "the custom function ran for more than one pool build"
    finally:
        _unregister_faker_provider_v2("person_first_name")
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert on.outputs["t"].equals(off.outputs["t"], check_metadata=True)


@NEEDS_COMPANION
def test_11_rebound_string_provider_runs_native_and_builds_once(tmp_path: Path) -> None:
    case = Case(tmp_path, string_source(40, mod=9), [faker_column()])
    off = case.run(lane=False, registry=rebound_registry(CountingAdapter(lambda i: f"OVR-{i}")))
    adapter = CountingAdapter(lambda i: f"OVR-{i}")
    on = lane_run(case, registry=rebound_registry(adapter))
    assert adapter.builds == 1
    _assert_full_parity(off, on)
    assert all(v.startswith("OVR-") for v in on.outputs["t"].column("c").to_pylist())


@NEEDS_COMPANION
def test_11_any_other_coded_difference_stays_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise ShadowDifference(code=CELL_VALUE_DIFF, detail="synthetic")

    monkeypatch.setattr(ShadowCoordinator, "run", _raise)
    case = Case(tmp_path, string_source(), [faker_column()])
    with pytest.raises(UnifiedSliceInvariantError):
        case.run(lane=True)


@NEEDS_COMPANION
def test_11_coordinator_without_an_injected_cache_uses_a_fresh_run_scoped_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dormant shadow corpus constructs the coordinator with no injected
    cache. Its pool must then land in a cache of its own, never one shared with
    the oracle that runs beside it, so the one identity is built twice."""
    source = string_source(20)
    path = write_read_only_fixture(tmp_path, source, "nocache")
    config = build_config(tmp_path, "t", path, [faker_column()])
    builds: list[int] = []
    real_build = PoolBuilder.build

    def _counting_build(self: PoolBuilder, *a: Any, **k: Any) -> Any:
        builds.append(1)
        return real_build(self, *a, **k)

    monkeypatch.setattr(PoolBuilder, "build", _counting_build)
    run = run_shadow_and_oracle(config, "t", source, key_provider=_key_provider())
    assert_shadow_matches_oracle(run)
    assert builds == [1, 1]


# ---------------------------------------------------------------------------
# 12. D7 positive kernel evidence for Faker (3e).
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_12_faker_node_without_kernel_evidence_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_run = ShadowCoordinator.run

    def _strip_evidence(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = real_run(self, *args, **kwargs)
        for evidence in result.route_evidence.values():
            if evidence.actual_operator == FAKER_OP:
                evidence.compiled_kernel_executed = False
        return result

    monkeypatch.setattr(ShadowCoordinator, "run", _strip_evidence)
    case = Case(tmp_path, string_source(), [faker_column()])
    with pytest.raises(UnifiedSliceInvariantError, match="positive"):
        case.run(lane=True)


# ---------------------------------------------------------------------------
# 12b / 13 / 13b. Shared cache behavior: LRU order, capacity, provider failure.
# ---------------------------------------------------------------------------


class _CacheRecorder:
    """Records every `PoolCache` built while a test runs, and can lower the
    default budget for all of them identically (both routes)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, max_bytes: int | None = None) -> None:
        self.instances: list[PoolCache] = []
        real_init = PoolCache.__init__
        recorder = self

        def _init(self_: PoolCache, **kwargs: Any) -> None:
            if max_bytes is not None:
                kwargs.setdefault("max_bytes", max_bytes)
            real_init(self_, **kwargs)
            recorder.instances.append(self_)

        monkeypatch.setattr(PoolCache, "__init__", _init)

    def populated(self) -> list[PoolCache]:
        return [c for c in self.instances if c._entries]


def _four_column_abac() -> tuple[pa.Table, list[dict[str, Any]]]:
    # The oracle visits nodes in sorted-name order (a, b, c, d), so the namespaces read A B A C
    # on that order. The config is declared in REVERSE-sorted order (d, c, b, a), which is the
    # plan order the coordinator used to follow (C A B A): a lane that kept plan order ends with
    # a different LRU residue than the oracle.
    n = 24
    source = pa.table(
        {
            name: pa.array([f"src_{i % 5}" for i in range(n)], type=pa.string())
            for name in ("a", "b", "c", "d")
        }
    )
    columns = [
        faker_column("d", namespace="ns_C"),
        faker_column("c", namespace="ns_A"),
        faker_column("b", namespace="ns_B"),
        faker_column("a", namespace="ns_A"),
    ]
    return source, columns


@NEEDS_COMPANION
def test_12b_lru_state_matches_the_oracle_for_a_b_a_c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, columns = _four_column_abac()
    case = Case(tmp_path, source, columns)
    probe = PoolBuilder(get_default_registry()).build(
        provider="person_first_name",
        size=30,
        job_seed=b"\x00" * 8,
        locale="en_US",
        config={},
        namespace="probe",
    )
    budget = int(estimate_pool_bytes(probe) * 2.5)

    oracle_cache = _CacheRecorder(monkeypatch, budget)
    case.run(lane=False)
    (oracle_cached,) = oracle_cache.populated()
    oracle_keys = set(oracle_cached._entries)
    monkeypatch.undo()

    lane_cache = _CacheRecorder(monkeypatch, budget)
    lane_run(case)
    (lane_cached,) = lane_cache.populated()
    assert set(lane_cached._entries) == oracle_keys
    assert len(oracle_keys) == 2


@NEEDS_COMPANION
def test_12c_two_stateful_columns_in_reverse_sorted_order_match_the_oracle(
    tmp_path: Path,
) -> None:
    # A stateful (impure) adapter hands out a different value per call, so the two columns
    # only agree lane-on vs lane-off when both routes visit the nodes in the same order.
    source = pa.table(
        {
            "a": pa.array([f"s{i % 4}" for i in range(12)], type=pa.string()),
            "b": pa.array([f"s{i % 4}" for i in range(12)], type=pa.string()),
        }
    )
    case = Case(
        tmp_path,
        source,
        [
            faker_column("b", provider="person_first_name", namespace="ns_b"),
            faker_column("a", provider="person_last_name", namespace="ns_a"),
        ],
    )

    def _registry() -> ProviderRegistry:
        counter = iter(range(10_000))
        default = get_default_registry()
        stateful = CountingAdapter(lambda i: f"v{next(counter)}")
        reg = default.override(
            "person_first_name", stateful, default.get_capabilities("person_first_name")
        )
        return reg.override(
            "person_last_name", stateful, default.get_capabilities("person_last_name")
        )

    off = case.run(lane=False, registry=_registry())
    on = lane_run(case, registry=_registry())
    assert on.outputs["t"].equals(off.outputs["t"], check_metadata=True)
    assert on.outputs["t"].column_names == off.outputs["t"].column_names


@NEEDS_COMPANION
def test_12d_two_failing_columns_raise_the_identical_error(tmp_path: Path) -> None:
    source = pa.table(
        {
            "a": pa.array(["x"] * 5, type=pa.string()),
            "b": pa.array(["y"] * 5, type=pa.string()),
        }
    )
    case = Case(
        tmp_path,
        source,
        [
            faker_column("b", provider="person_first_name", namespace="ns_b"),
            faker_column("a", provider="person_last_name", namespace="ns_a"),
        ],
    )

    def _registry() -> ProviderRegistry:
        default = get_default_registry()

        def _failing(code: str) -> CountingAdapter:
            return CountingAdapter(
                lambda i: "x", fail=ProviderError(code=code, message=f"{code} message")
            )

        reg = default.override(
            "person_first_name",
            _failing("first_name_fail"),
            default.get_capabilities("person_first_name"),
        )
        return reg.override(
            "person_last_name",
            _failing("last_name_fail"),
            default.get_capabilities("person_last_name"),
        )

    with pytest.raises(ProviderError) as off_info:
        case.run(lane=False, registry=_registry())
    with poisoned_oracle(), pytest.raises(ProviderError) as on_info:
        case.run(lane=True, registry=_registry())
    assert type(on_info.value) is type(off_info.value)
    assert on_info.value.code == off_info.value.code == "last_name_fail"
    assert str(on_info.value) == str(off_info.value)


@NEEDS_COMPANION
def test_13_oversized_pool_raises_the_oracles_error_after_one_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = Case(tmp_path, string_source(30, mod=5), [faker_column()])
    _CacheRecorder(monkeypatch, 64)  # far below any pool

    off_adapter = CountingAdapter(lambda i: f"N{i}")
    with pytest.raises(PoolCapacityError) as off_info:
        case.run(lane=False, registry=rebound_registry(off_adapter))
    assert off_adapter.builds == 1

    adapter = CountingAdapter(lambda i: f"N{i}")
    with poisoned_oracle(), pytest.raises(PoolCapacityError) as on_info:
        case.run(lane=True, registry=rebound_registry(adapter))
    assert adapter.builds == 1
    assert type(on_info.value) is type(off_info.value)
    assert on_info.value.code == off_info.value.code == "pool_exceeds_cache_budget"
    assert str(on_info.value) == str(off_info.value)


class _RecordingSink:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def write(self, table: str, data: Any) -> None:
        self.calls.append("write")

    def write_batches(self, table: str, batches: Any, *, schema: Any) -> None:
        self.calls.append("write_batches")

    def commit(self) -> None:
        self.calls.append("commit")

    def abort(self) -> None:
        self.calls.append("abort")


@NEEDS_COMPANION
def test_13b_provider_failure_is_invoked_once_and_raised_unwrapped(tmp_path: Path) -> None:
    case = Case(tmp_path, string_source(30, mod=5), [faker_column()])

    def _failing() -> CountingAdapter:
        # A raised-from chain, so the lane's re-raise must keep __cause__ and the
        # suppress flag as the provider set them (a `from None` re-raise drops both).
        try:
            raise ValueError("root cause", 7)
        except ValueError as root:
            err = ProviderError(code="synthetic_provider_failure", message="boom")
            err.__cause__ = root
        return CountingAdapter(lambda i: f"N{i}", fail=err)

    off_adapter = _failing()
    with pytest.raises(ProviderError) as off_info:
        case.run(lane=False, registry=rebound_registry(off_adapter))
    assert off_adapter.builds == 1

    adapter = _failing()
    sink = _RecordingSink()
    with poisoned_oracle(), pytest.raises(ProviderError) as on_info:
        case.run(lane=True, registry=rebound_registry(adapter), sink=sink)
    assert adapter.builds == 1
    assert type(on_info.value) is type(off_info.value)
    assert on_info.value.code == off_info.value.code == "synthetic_provider_failure"
    assert str(on_info.value) == str(off_info.value)
    assert type(on_info.value.__cause__) is type(off_info.value.__cause__) is ValueError
    assert on_info.value.__cause__.args == off_info.value.__cause__.args == ("root cause", 7)
    assert on_info.value.__suppress_context__ == off_info.value.__suppress_context__
    assert [c for c in sink.calls if c in ("write", "write_batches", "commit")] == []


# ---------------------------------------------------------------------------
# 14. Warnings and evidence parity (3f), non-vacuous.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_14_no_pool_cache_warnings_are_read_on_either_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[int] = []
    real_warnings = PoolCache.warnings

    def _spy(self: PoolCache) -> Any:
        reads.append(1)
        return real_warnings(self)

    monkeypatch.setattr(PoolCache, "warnings", _spy)
    case = Case(tmp_path, string_source(40, mod=7, null_every=5), [faker_column()])
    off = case.run(lane=False)
    on = lane_run(case)
    _assert_full_parity(off, on)
    assert tuple(on.warnings) == tuple(off.warnings)
    assert reads == [], "a full-frame route read PoolCache.warnings()"
    # Proves the spy is live: a direct read is recorded.
    PoolCache().warnings()
    assert reads == [1]


# ---------------------------------------------------------------------------
# 15. Companion-absent clean env (ci-mirror).
# ---------------------------------------------------------------------------


def test_15_all_kernels_unavailable_declines_faker_to_the_oracle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = Case(tmp_path, string_source(), [faker_column()])
    off = case.run(lane=False)
    monkeypatch.setattr(
        _unified_slice_admission,
        "native_kernel_availability",
        lambda: KernelAvailability(crypto=False, index=False, raw_hex=False),
    )
    on = case.run(lane=True)
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert on.outputs["t"].equals(off.outputs["t"], check_metadata=True)


@pytest.mark.skipif(
    native_companion_status().ok, reason="only meaningful where the companion is absent"
)
def test_15_companion_absent_env_declines_faker(tmp_path: Path) -> None:
    case = Case(tmp_path, string_source(), [faker_column()])
    off = case.run(lane=False)
    on = case.run(lane=True)
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert on.outputs["t"].equals(off.outputs["t"], check_metadata=True)
