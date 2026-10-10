"""C6b-i acceptance: text_mask as an ARROW_PYTHON operator on the unified full-frame route.

text_mask is KEYED and handler-rich, so parity is proven three ways: byte-identical value + Arrow
type + warning multiset against the shipped `TextMaskHandler` (coordinator vs pandas oracle), the
production unified-slice `ExecutionResult` (flag-off vs flag-on, oracle poisoned), and the fail-closed
StrategyError raised PRE-fallback by the coordinator. The win is the table: a text_mask column no
longer drags its siblings onto the pandas oracle.
"""

from __future__ import annotations

import functools
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import run_pipeline
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._text_mask import TextMaskHandler
from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY
from decoy_engine.execution.native._chunked_evidence import (
    ARROW_PYTHON,
    RUST_COMPANION,
    RUST_POOL_SELECT,
)
from decoy_engine.execution.native._companion_status import KernelAvailability
from decoy_engine.execution.native._operator_config_rejections import text_mask_config_rejection
from decoy_engine.execution.native._operator_params import TextMaskParams, resolve_operator_params
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
from decoy_engine.execution.physical._shadow_snapshot import capture_shadow_snapshot
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.plan._types import ColumnSeed
from tests.native._c6b_i_support import (
    ADMITTED_CONFIGS,
    CORPUS,
    EXCLUDED_CONFIGS,
    SHAPES,
    SUB_FLOOR_TEXTS,
    tm_col,
)
from tests.physical import _shadow_helpers
from tests.physical._shadow_helpers import (
    ENGINE_VERSION,
    assert_every_node_bound,
    assert_shadow_matches_oracle,
    build_config,
    run_shadow_and_oracle,
    write_read_only_fixture,
)
from tests.physical.test_unified_route_evidence import (
    _FAKER,
    _HASH,
    NEEDS_COMPANION,
    _evidence_for,
    assert_exact_evidence,
    lane_nodes,
)

OPERATOR = "native_text_mask"
_PASS = {"name": "p", "strategy": "passthrough"}
_MASK_KEY = bytes(range(32))


def _kp(secret: bytes = _MASK_KEY) -> SecretKeyProvider:
    return SecretKeyProvider(secret=secret, key_version="v1")


@pytest.fixture(autouse=True)
def _pandas_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the shared harness's oracle call to the legacy pandas route, so every coordinator-vs-oracle
    assertion compares native against the shipped handler, not native against native."""
    monkeypatch.setattr(
        _shadow_helpers,
        "run_pipeline",
        functools.partial(run_pipeline, unified_slice_enabled=False),
    )


def _source(values: list[str | None]) -> pa.Table:
    return pa.table(
        {
            "s": pa.array(values, pa.string()),
            "p": pa.array(list(range(len(values))), pa.int64()),
        }
    )


def _one(values: list[str | None]) -> pa.Table:
    """A single-column `s` table: the unified coordinator requires every source column configured,
    so the coordinator-vs-oracle byte-match runs over one text_mask column at a time."""
    return pa.table({"s": pa.array(values, pa.string())})


def _assert_text_mask_evidence(nodes: dict[str, dict[str, Any]], *, calls: int = 1) -> None:
    assert_exact_evidence(
        _evidence_for(nodes, OPERATOR),
        operator=OPERATOR,
        compiled=False,
        planned=ARROW_PYTHON,
        executed=ARROW_PYTHON,
        calls=calls,
    )


# ---------------------------------------------------------------------------
# 1. Byte + warning parity against the shipped handler over every shape and config.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("batch_size_rows", [2, 50_000])
@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("config", sorted(ADMITTED_CONFIGS))
def test_coordinator_matches_oracle_byte_identical(
    tmp_path: Path, config: str, shape: str, batch_size_rows: int
) -> None:
    source = _one(SHAPES[shape])
    write_read_only_fixture(tmp_path, source, "tm")
    cfg = build_config(
        tmp_path, "t", tmp_path / "tm.parquet", [tm_col("s", **ADMITTED_CONFIGS[config])]
    )
    run = run_shadow_and_oracle(
        cfg, "t", source, key_provider=_kp(), batch_size_rows=batch_size_rows
    )
    assert_every_node_bound(run.plan)
    assert_shadow_matches_oracle(run)  # value + null + order + rows + field type + warnings


def test_native_route_taken(tmp_path: Path) -> None:
    source = _one(CORPUS)
    write_read_only_fixture(tmp_path, source, "tm")
    cfg = build_config(tmp_path, "t", tmp_path / "tm.parquet", [tm_col("s")])
    run = run_shadow_and_oracle(cfg, "t", source, key_provider=_kp(), batch_size_rows=7)
    (node,) = [n for t in run.plan.tables for n in t.nodes]
    assert node.execution is not None
    assert node.execution.operator_id == OPERATOR
    assert node.execution.key_binding is None  # bound like text_redact (no namespace KeyBinding)
    assert node.execution.pool_binding is None
    (evidence,) = (e for nid, e in run.shadow.route_evidence.items())
    assert evidence.actual_operator == OPERATOR
    assert evidence.compiled_kernel_executed is False
    assert_shadow_matches_oracle(run)


# ---------------------------------------------------------------------------
# 2. Table-level lift: a text_mask column keeps its siblings native, oracle poisoned.
# ---------------------------------------------------------------------------


def test_a_text_mask_only_table_runs_with_the_compiled_companion_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """text_mask needs no compiled kernel, so a host without the companion still admits it."""
    from decoy_engine.execution import _unified_slice_admission

    monkeypatch.setattr(
        _unified_slice_admission,
        "native_kernel_availability",
        lambda: KernelAvailability(crypto=False, index=False, raw_hex=False, fpe=False),
    )
    nodes = lane_nodes(
        tmp_path, pa.table({"s": pa.array(CORPUS, pa.string())}), [tm_col("s")], monkeypatch
    )
    assert len(nodes) == 1
    _assert_text_mask_evidence(nodes)


@NEEDS_COMPANION
def test_text_mask_beside_hash_and_faker_keeps_each_on_its_own_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = 9
    table = pa.table(
        {
            "h": pa.array([f"a{i}@x.com" for i in range(n)], pa.string()),
            "f": pa.array([f"src_{i % 3}" for i in range(n)], pa.string()),
            "s": pa.array(CORPUS[:n], pa.string()),
        }
    )
    nodes = lane_nodes(tmp_path, table, [_HASH, _FAKER, tm_col("s")], monkeypatch)
    _assert_text_mask_evidence(nodes)
    backends = {ev["operator"]: ev["executed_backend"] for ev in nodes.values()}
    assert backends == {
        "native_keyed_hash": RUST_COMPANION,
        "native_faker_select": RUST_POOL_SELECT,
        OPERATOR: ARROW_PYTHON,
    }


def test_text_mask_beside_redact_keeps_the_table_native(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = 7
    table = pa.table(
        {
            "r": pa.array([f"r{i}" for i in range(n)], pa.string()),
            "s": pa.array(CORPUS[:n], pa.string()),
        }
    )
    nodes = lane_nodes(
        tmp_path, table, [{"name": "r", "strategy": "redact"}, tm_col("s")], monkeypatch
    )
    assert {ev["operator"] for ev in nodes.values()} == {"native_redact", OPERATOR}
    _assert_text_mask_evidence(nodes)


# ---------------------------------------------------------------------------
# 3. Warnings parity: the sub-floor warning rides ExecutionResult.warnings, aggregated per column.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["redact", "synthetic"])
@pytest.mark.parametrize("batch_size_rows", [2, 50_000])
def test_sub_floor_warning_parity(tmp_path: Path, policy: str, batch_size_rows: int) -> None:
    # unmatched passthrough keeps the surrounding text; the point is the fpe sub-floor span handling.
    source = _one(SUB_FLOOR_TEXTS)
    write_read_only_fixture(tmp_path, source, "tm")
    cfg = build_config(
        tmp_path,
        "t",
        tmp_path / "tm.parquet",
        [tm_col("s", sub_floor_span=policy, unmatched_span_policy="passthrough")],
    )
    run = run_shadow_and_oracle(
        cfg, "t", source, key_provider=_kp(), batch_size_rows=batch_size_rows
    )
    assert_shadow_matches_oracle(run)  # compares the warning multisets across batches
    handled = [w for w in run.shadow.warnings if w.code == "text_mask_sub_floor_span_handled"]
    assert len(handled) == 1, run.shadow.warnings  # one aggregate warning per column
    (w,) = handled
    assert w.column == "s" and w.provider == "text_mask"
    assert w.detail["policy"] == policy
    assert w.detail["by_detector"] == {"us_zip": 4} and w.detail["total"] == 4


def test_two_text_mask_columns_isolate_their_warnings(tmp_path: Path) -> None:
    source = pa.table(
        {
            "a": pa.array(["zip 12345 x", "zip 67890 y", None], pa.string()),
            "b": pa.array(["no zip here", "zip 90210 z", "plain"], pa.string()),
        }
    )
    write_read_only_fixture(tmp_path, source, "tm")
    cfg = build_config(
        tmp_path,
        "t",
        tmp_path / "tm.parquet",
        [
            tm_col("a", sub_floor_span="redact", unmatched_span_policy="passthrough"),
            tm_col("b", sub_floor_span="redact", unmatched_span_policy="passthrough"),
        ],
    )
    run = run_shadow_and_oracle(cfg, "t", source, key_provider=_kp(), batch_size_rows=2)
    assert_shadow_matches_oracle(run)
    by_col = {
        w.column: w.detail["by_detector"]
        for w in run.shadow.warnings
        if w.code == "text_mask_sub_floor_span_handled"
    }
    assert by_col == {"a": {"us_zip": 2}, "b": {"us_zip": 1}}


# ---------------------------------------------------------------------------
# 4. Failure parity: a fail-closed span raises the same StrategyError on both lanes.
# ---------------------------------------------------------------------------


def _run_flagged(cfg: dict[str, Any], path: Path, *, flag: bool) -> ExecutionResult:
    return run_pipeline(
        cfg,
        {"t": pq.read_table(path)},
        engine_version=ENGINE_VERSION,
        key_provider=_kp(),
        unified_slice_enabled=flag,
    )


def test_fail_closed_parity_flag_on_and_off(tmp_path: Path) -> None:
    # A 5-digit us_zip under the DEFAULT config (us_zip -> fpe, no sub_floor_span) cannot be
    # FF1-encrypted and no policy is set, so both lanes raise the canonical StrategyError.
    source = _source(["home zip 12345 today", "123-45-6789"])
    path = write_read_only_fixture(tmp_path, source, "tm")
    cfg = build_config(tmp_path, "t", path, [tm_col("s"), _PASS])
    with pytest.raises(StrategyError) as off_exc:
        _run_flagged(cfg, path, flag=False)
    with pytest.raises(StrategyError) as on_exc:
        _run_flagged(cfg, path, flag=True)
    assert type(on_exc.value) is type(off_exc.value) is StrategyError
    assert on_exc.value.code == off_exc.value.code == "fpe_unencryptable_domain"
    assert on_exc.value.strategy == off_exc.value.strategy == "text_mask"


def _coordinator_error(tmp_path: Path, source: pa.Table, *, batch_rows: int) -> StrategyError:
    """Drive the shadow coordinator directly (no run_pipeline, no oracle fallback) and return the
    StrategyError it raises, so the NATIVE exception mapping is graded, not the oracle's."""
    path = write_read_only_fixture(tmp_path, source, "tm")
    cfg = build_config(tmp_path, "t", path, [tm_col("s")])
    inputs = capture_physical_plan_inputs(cfg, {"t": source}, engine_version=ENGINE_VERSION)
    plan = compile_physical_plan(inputs)
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=_kp(), batch_size_rows=batch_rows
    )
    snapshot = capture_shadow_snapshot({"t": source})
    with pytest.raises(StrategyError) as exc:
        ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(plan, snapshot)
    return exc.value


def _handler_error(values: list[Any]) -> StrategyError:
    """The StrategyError the SHIPPED `TextMaskHandler` raises for the same column (the grading
    oracle for the native route's pre-fallback exception)."""
    import pandas as pd

    seed = ColumnSeed(
        namespace=None,
        strategy="text_mask",
        provider=None,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        provider_config=(),
    )
    ctx = SimpleNamespace(
        mask_key=_MASK_KEY, row_errors=[], group_anchor_snapshots={}, current_table="t"
    )
    with pytest.raises(StrategyError) as exc:
        TextMaskHandler().run(
            pd.DataFrame({"s": pd.Series(values, dtype=object)}),
            "s",
            seed,
            ctx,  # type: ignore[arg-type]
        )
    return exc.value


@pytest.mark.parametrize("batch_rows", [1, 50_000])
def test_native_coordinator_exception_is_pre_fallback(tmp_path: Path, batch_rows: int) -> None:
    values = ["ok 123-45-6789", "home zip 12345 here"]
    native = _coordinator_error(tmp_path, _one(values), batch_rows=batch_rows)
    handler = _handler_error(values)
    assert type(native) is type(handler) is StrategyError
    assert native.code == handler.code == "fpe_unencryptable_domain"
    assert native.strategy == handler.strategy == "text_mask"


# ---------------------------------------------------------------------------
# 5. Source admission: a non-string source declines the lane, output equals the oracle.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("typ", [pa.int64(), pa.large_string()], ids=["int64", "large_string"])
def test_a_non_string_source_declines_the_lane(tmp_path: Path, typ: pa.DataType) -> None:
    values = [10, 22, None, 333] if typ == pa.int64() else ["a@b.com", None, "x 123-45-6789 y", ""]
    source = pa.table({"s": pa.array(values, typ), "p": pa.array(range(4), pa.int64())})
    path = write_read_only_fixture(tmp_path, source, "tm")
    cfg = build_config(tmp_path, "t", path, [tm_col("s"), _PASS])
    off = _run_flagged(cfg, path, flag=False)
    on = _run_flagged(cfg, path, flag=True)
    # The non-string source is unsupported on the native lane, so the whole table declines to the
    # pandas oracle: no unified quality metrics AND the flag-on output is byte-identical to the
    # flag-off oracle (schema + values + b"pandas" metadata), i.e. the decline perturbs nothing.
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert on.outputs["t"].equals(off.outputs["t"], check_metadata=True)


# ---------------------------------------------------------------------------
# 7. ner declines to the oracle (config gate), unlike token / detectors.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(EXCLUDED_CONFIGS))
def test_ner_config_declines_native(name: str) -> None:
    cfg, code = EXCLUDED_CONFIGS[name]
    assert text_mask_config_rejection("s", cfg) == f"{code}:s"


def test_date_shift_bounds_mirror_the_handlers_present_keys() -> None:
    # dennis LOW-1: the native params must distinguish an ABSENT min_days/max_days (the per-span
    # date_shift keeps its own default) from a PRESENT-but-null one (passed through, so date_shift
    # crashes identically to the oracle's `if key in cfg`), not collapse both to a default.
    def bounds(cfg: dict[str, Any]) -> tuple[tuple[str, int | None], ...]:
        params = resolve_operator_params(
            "text_mask", target="s", provider_config=cfg, namespace=None
        )
        assert isinstance(params, TextMaskParams)
        return params.date_shift_bounds

    assert bounds({}) == ()
    assert bounds({"min_days": 1, "max_days": 30}) == (("min_days", 1), ("max_days", 30))
    assert bounds({"min_days": None}) == (("min_days", None),)
    assert bounds({"max_days": 7}) == (("max_days", 7),)


def test_token_and_detectors_do_not_decline_unlike_text_redact() -> None:
    # text_mask normalizes a non-string token (str()) and a non-list detectors (all) the same as
    # the oracle, so neither declines (the text_redact divergence does not apply).
    assert text_mask_config_rejection("s", {"token": 7}) is None
    assert text_mask_config_rejection("s", {"detectors": "email"}) is None


# ---------------------------------------------------------------------------
# 1 (key provenance). text_mask is KEYED: seed-derived and secret-backed keys both match the
# oracle, and the masked output DEPENDS on the key (unlike unkeyed text_redact).
# ---------------------------------------------------------------------------


def _masked_values(
    tmp_path: Path, name: str, *, key_provider: SecretKeyProvider | None
) -> list[Any]:
    sub = tmp_path / name
    sub.mkdir()
    source = _source(CORPUS)
    path = write_read_only_fixture(sub, source, "tm")
    cfg = build_config(sub, "t", path, [tm_col("s"), _PASS])
    run = run_shadow_and_oracle(cfg, "t", source, key_provider=key_provider)
    assert_shadow_matches_oracle(run)  # native == oracle under THIS key
    return list(run.shadow.outputs["t"].column("s").to_pylist())


def test_seed_derived_and_secret_keys_both_match_oracle_and_output_is_keyed(tmp_path: Path) -> None:
    seed_based = _masked_values(tmp_path, "seed", key_provider=None)
    secret_a = _masked_values(tmp_path, "a", key_provider=_kp(bytes(range(32))))
    secret_a2 = _masked_values(tmp_path, "a2", key_provider=_kp(bytes(range(32))))
    secret_b = _masked_values(tmp_path, "b", key_provider=_kp(bytes(range(1, 33))))
    assert secret_a == secret_a2  # same key -> same masked output
    # text_mask is keyed: a different secret, and the seed-only run, change the fpe ciphertext.
    assert secret_a != secret_b
    assert secret_a != seed_based
