"""Acceptance: native FPE (FF1) on the unified full-frame route (C6a plan §4).

Byte parity (value AND Arrow field type) against the pinned pandas oracle at three boundaries:
the coordinator's assembled output vs the oracle `run_pipeline` (`run_shadow_and_oracle`), the
production unified-slice `ExecutionResult` (flag-off vs flag-on), and the operator's values +
per-row error set vs the pure-Python reference.

Warnings: fpe's residual-risk warnings are computed in Python at the whole-column scope and ride
`ExecutionResult.warnings`, never the output; `assert_shadow_matches_oracle` compares them as
order-independent multisets. Fail-closed: a per-row failure raises a `StrategyError` in the
coordinator; the unified slice reroutes to the oracle, which raises the canonical error (class +
code), so flag-on and flag-off fail identically. Checksum modes and a missing companion decline
to the oracle.
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
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._fpe import FpeStrategyHandler
from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY
from decoy_engine.execution.native._companion_status import (
    KernelAvailability,
    native_companion_status,
)
from decoy_engine.execution.native._crypto_ext import FpeConfig
from decoy_engine.execution.native._crypto_reference import reference_fpe
from decoy_engine.execution.native._fpe_ext import native_fpe
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
from decoy_engine.execution.physical._shadow_snapshot import capture_shadow_snapshot
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.plan._types import ColumnSeed
from tests.physical import _shadow_helpers
from tests.physical._shadow_helpers import (
    ENGINE_VERSION,
    assert_every_node_bound,
    assert_shadow_matches_oracle,
    build_config,
    run_shadow_and_oracle,
    write_read_only_fixture,
)

_MASK_KEY = bytes(range(32))
_NS = "people.ssn"
_SSNS = ["123456789", "987654321", "123-45-6789", "555112222", "123456789", "000000001"]

_NEEDS_COMPANION = pytest.mark.skipif(
    not native_companion_status().ok,
    reason="compiled decoy-engine-native companion unavailable; the native shadow path requires it",
)


@pytest.fixture(autouse=True)
def _pandas_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the shared harness's oracle call to the legacy pandas route, so every
    coordinator-vs-oracle assertion compares native against pandas, not native against native
    (run_pipeline defaults `unified_slice_enabled=True`, which would itself take the fpe lane)."""
    monkeypatch.setattr(
        _shadow_helpers,
        "run_pipeline",
        functools.partial(run_pipeline, unified_slice_enabled=False),
    )


def _kp() -> SecretKeyProvider:
    return SecretKeyProvider(secret=_MASK_KEY, key_version="v1")


def _fpe_column(name: str = "c", *, namespace: str | None = _NS, **pc: Any) -> dict[str, Any]:
    cfg = {"charset": "digits", **pc}
    col: dict[str, Any] = {"name": name, "strategy": "fpe", "provider_config": cfg}
    if namespace is not None:
        col["namespace"] = namespace
    return col


def _run(config: dict[str, Any], path: Path, *, flag: bool) -> Any:
    return run_pipeline(
        config,
        {"t": pq.read_table(path)},
        engine_version=ENGINE_VERSION,
        key_provider=_kp(),
        unified_slice_enabled=flag,
    )


# ── Operator differential vs the reference (values + per-row errors) ──


@_NEEDS_COMPANION
@pytest.mark.parametrize(
    "charset, values",
    [
        ("digits", [*_SSNS, None, ""]),
        ("ALPHANUM", ["AB12CD34EF", None, "ZZ99XX88YY", "", "AB12CD34EF"]),
    ],
)
def test_operator_matches_reference(charset: str, values: list[Any]) -> None:
    cfg = FpeConfig(charset=charset)
    native = native_fpe(
        pa.array(values, type=pa.string()),
        mask_key=_MASK_KEY,
        namespace=_NS,
        tweak_column="c",
        config=cfg,
    )
    ref = reference_fpe().encrypt_batch(
        pa.array(values, type=pa.string()),
        mask_key=_MASK_KEY,
        namespace=_NS,
        tweak_column="c",
        config=cfg,
    )
    assert native.values.to_pylist() == ref.values.to_pylist()
    assert native.errors == ref.errors


# ── Coordinator vs oracle, byte-identical over clean + degenerate shapes ──

_SHAPES: list[tuple[str, list[Any]]] = [
    ("populated", _SSNS),
    ("with_nulls", ["123456789", None, "987654321", None]),
    ("dup", ["123456789", "123456789", None, "123456789"]),
    ("single", ["123456789"]),
    ("empty", []),
    ("all_null", [None, None, None]),
    ("all_empty", ["", "", ""]),
    ("null_and_empty", [None, "", "123456789", None, ""]),
]


@_NEEDS_COMPANION
@pytest.mark.parametrize("batch_size_rows", [2, 50_000])
@pytest.mark.parametrize("label, values", _SHAPES, ids=[s[0] for s in _SHAPES])
def test_coordinator_matches_oracle_byte_identical(
    tmp_path: Path, label: str, values: list[Any], batch_size_rows: int
) -> None:
    source = pa.table({"c": pa.array(values, type=pa.string())})
    write_read_only_fixture(tmp_path, source, "fpe")
    config = build_config(tmp_path, "t", tmp_path / "fpe.parquet", [_fpe_column()])
    run = run_shadow_and_oracle(
        config, "t", source, key_provider=_kp(), batch_size_rows=batch_size_rows
    )
    assert_every_node_bound(run.plan)
    assert_shadow_matches_oracle(run)  # value + null + order + rows + field type + warnings
    assert run.shadow.row_errors == ()


@_NEEDS_COMPANION
def test_assembled_type_per_shape(tmp_path: Path) -> None:
    """The degenerate reconciliation pinned concretely (plan §3i): value-bearing -> string,
    empty -> double, all-null -> null, all-empty -> string."""
    expected = {
        "populated": pa.string(),
        "empty": pa.float64(),
        "all_null": pa.null(),
        "all_empty": pa.string(),
    }
    for label, values in _SHAPES:
        if label not in expected:
            continue
        source = pa.table({"c": pa.array(values, type=pa.string())})
        sub = tmp_path / label
        sub.mkdir()
        write_read_only_fixture(sub, source, "fpe")
        config = build_config(sub, "t", sub / "fpe.parquet", [_fpe_column()])
        run = run_shadow_and_oracle(config, "t", source, key_provider=_kp())
        assert run.shadow.outputs["t"].schema.field("c").type == expected[label]
        assert run.oracle.outputs["t"].schema.field("c").type == expected[label]


@_NEEDS_COMPANION
def test_native_route_taken(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(_SSNS, type=pa.string())})
    write_read_only_fixture(tmp_path, source, "fpe")
    config = build_config(tmp_path, "t", tmp_path / "fpe.parquet", [_fpe_column()])
    run = run_shadow_and_oracle(config, "t", source, key_provider=_kp(), batch_size_rows=2)
    (node,) = [n for t in run.plan.tables for n in t.nodes]
    assert node.execution is not None
    assert node.execution.operator_id == "native_fpe"
    assert node.execution.pool_binding is None
    assert node.execution.needs_index_kernel is False
    (evidence,) = run.shadow.route_evidence.values()
    assert evidence.actual_operator == "native_fpe"
    assert evidence.compiled_kernel_executed is True
    assert_shadow_matches_oracle(run)


# ── Warnings parity (§3e): partial plaintext + join-group multiplicity ──


@_NEEDS_COMPANION
def test_partial_plaintext_warning_parity(tmp_path: Path) -> None:
    # An alphanumeric out-of-charset prefix retained under preserve_separators -> one warning,
    # with affected/total counts the whole-column denominator produces.
    source = pa.table({"c": pa.array(["M000001", "M000002", "000003", None], type=pa.string())})
    write_read_only_fixture(tmp_path, source, "fpe")
    config = build_config(
        tmp_path, "t", tmp_path / "fpe.parquet", [_fpe_column(preserve_separators=True)]
    )
    run = run_shadow_and_oracle(config, "t", source, key_provider=_kp(), batch_size_rows=2)
    assert_shadow_matches_oracle(run)  # compares the warning multisets
    codes = [w.code for w in run.shadow.warnings]
    assert codes.count("fpe_partial_plaintext_disclosure") == 1
    (w,) = [w for w in run.shadow.warnings if w.code == "fpe_partial_plaintext_disclosure"]
    assert w.detail["affected_values"] == 2 and w.detail["total_values"] == 3


@_NEEDS_COMPANION
def test_join_group_warning_and_shared_ciphertext(tmp_path: Path) -> None:
    source = pa.table(
        {"a": pa.array(_SSNS[:3], type=pa.string()), "b": pa.array(_SSNS[:3], type=pa.string())}
    )
    write_read_only_fixture(tmp_path, source, "fpe")
    config = build_config(
        tmp_path,
        "t",
        tmp_path / "fpe.parquet",
        [_fpe_column("a", fpe_join_group="grp"), _fpe_column("b", fpe_join_group="grp")],
    )
    run = run_shadow_and_oracle(config, "t", source, key_provider=_kp())
    assert_shadow_matches_oracle(run)
    jg = [w for w in run.shadow.warnings if w.code == "fpe_join_group_active"]
    assert len(jg) == 2  # one per member column
    out = run.shadow.outputs["t"]
    assert out.column("a").to_pylist() == out.column("b").to_pylist()


# ── Fail-closed parity: flag-on reroutes, both arms raise the same code (§3d) ──


@_NEEDS_COMPANION
@pytest.mark.parametrize(
    "pc, values, expected_code",
    [
        ({"preserve_separators": False}, ["12ab34", "999999999"], "fpe_unencryptable_value"),
        ({}, ["123", "456"], "fpe_unencryptable_domain"),
        ({}, ["1" * 300, "123456789"], "fpe_unencryptable_length"),
    ],
    ids=["unencryptable_value", "unencryptable_domain", "unencryptable_length"],
)
def test_fail_closed_parity(
    tmp_path: Path, pc: dict[str, Any], values: list[Any], expected_code: str
) -> None:
    source = pa.table({"c": pa.array(values, type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "fpe")
    config = build_config(tmp_path, "t", path, [_fpe_column(**pc)])
    with pytest.raises(StrategyError) as off_exc:
        _run(config, path, flag=False)
    with pytest.raises(StrategyError) as on_exc:
        _run(config, path, flag=True)
    assert type(on_exc.value) is type(off_exc.value)
    assert on_exc.value.code == off_exc.value.code == expected_code


# ── Native PRE-FALLBACK fail-closed: the coordinator's own StrategyError (MEDIUM-2) ──
# The flag-on tests above only observe the exception AFTER the unified slice's generic boundary
# reroutes to the oracle, so they pass even if the native adapter's error mapping is wrong. These
# drive the coordinator directly (no run_pipeline, no reroute) and assert the native exception
# itself, against the SHIPPED handler, so the native first-failure selection + code mapping are
# actually exercised.


def _coordinator_fail_closed(
    tmp_path: Path, columns: list[dict[str, Any]], source: pa.Table, *, batch_rows: int
) -> StrategyError:
    """Run the shadow coordinator directly over `source` and return the StrategyError it raises
    BEFORE any oracle fallback, or fail if it does not raise."""
    path = write_read_only_fixture(tmp_path, source, "fpe")
    config = build_config(tmp_path, "t", path, columns)
    inputs = capture_physical_plan_inputs(config, {"t": source}, engine_version=ENGINE_VERSION)
    plan = compile_physical_plan(inputs)
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=_kp(), batch_size_rows=batch_rows
    )
    snapshot = capture_shadow_snapshot({"t": source})
    with pytest.raises(StrategyError) as exc:
        ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(plan, snapshot)
    return exc.value


def _handler_error(values: list[Any], pc: dict[str, Any]) -> StrategyError:
    """The StrategyError the SHIPPED `FpeStrategyHandler` raises for the same column (the §3d
    grading oracle for the route exception)."""
    import pandas as pd

    seed = ColumnSeed(
        namespace=_NS,
        strategy="fpe",
        provider=None,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        provider_config=tuple(sorted({"charset": "digits", **pc}.items())),
    )
    ctx = SimpleNamespace(
        mask_key=_MASK_KEY, row_errors=[], group_anchor_snapshots={}, current_table="t"
    )
    with pytest.raises(StrategyError) as exc:
        FpeStrategyHandler().run(
            pd.DataFrame({"c": pd.Series(values, dtype=object)}),
            "c",
            seed,
            ctx,  # type: ignore[arg-type]
        )
    return exc.value


@_NEEDS_COMPANION
@pytest.mark.parametrize(
    "pc, values, expected_code",
    [
        ({"preserve_separators": False}, ["12ab34", "999999999"], "fpe_unencryptable_value"),
        ({}, ["123", "456"], "fpe_unencryptable_domain"),
        ({}, ["1" * 300, "123456789"], "fpe_unencryptable_length"),
        # mixed codes across rows: the FIRST failing row (domain@0) wins, not the later value@1.
        ({"preserve_separators": False}, ["123", "12ab34"], "fpe_unencryptable_domain"),
    ],
    ids=["value", "domain", "length", "mixed_first_failure"],
)
def test_native_coordinator_exception_is_pre_fallback(
    tmp_path: Path, pc: dict[str, Any], values: list[Any], expected_code: str
) -> None:
    source = pa.table({"c": pa.array(values, type=pa.string())})
    native = _coordinator_fail_closed(tmp_path, [_fpe_column(**pc)], source, batch_rows=50_000)
    handler = _handler_error(values, pc)
    assert type(native) is type(handler) is StrategyError
    assert native.code == handler.code == expected_code


@_NEEDS_COMPANION
def test_native_coordinator_first_failure_across_batches(tmp_path: Path) -> None:
    """With one row per batch, the first batch carrying a failure decides the code: row 1 (domain)
    precedes row 3 (value), so the coordinator raises domain, matching the handler."""
    values = ["123456789", "123", "987654321", "12ab34"]
    source = pa.table({"c": pa.array(values, type=pa.string())})
    native = _coordinator_fail_closed(
        tmp_path, [_fpe_column(preserve_separators=False)], source, batch_rows=1
    )
    handler = _handler_error(values, {"preserve_separators": False})
    assert native.code == handler.code == "fpe_unencryptable_domain"


@_NEEDS_COMPANION
@pytest.mark.parametrize("mutation", ["wrong_code", "wrong_first_row"])
def test_pre_fallback_assertion_kills_error_mapping_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Mutation-kill proof for MEDIUM-2: a wrong-code or wrong-first-row `fpe_fail_closed_error`
    changes the coordinator's PRE-FALLBACK exception, so the direct assertion above would fail.
    The public flag-on/flag-off tests cannot see this (the oracle fallback supplies the right
    error); these drive the coordinator directly and prove the mutation is observable."""
    from decoy_engine.execution.physical import _shadow_operators

    def _mutant(errors: tuple[Any, ...], column: str) -> StrategyError:
        if mutation == "wrong_code":
            return StrategyError(code="fpe_wrong_code_mutant", strategy="fpe", message="x")
        # wrong_first_row: pick the LAST error instead of the first-by-index.
        last = max(errors, key=lambda e: e.row_index)
        return StrategyError(code=last.code, strategy="fpe", message="x")

    monkeypatch.setattr(_shadow_operators, "fpe_fail_closed_error", _mutant)
    # domain@0 then value@1: the correct first-failure code is domain; the mutations yield a
    # different code, which the direct coordinator exception now carries.
    values = ["123", "12ab34"]
    source = pa.table({"c": pa.array(values, type=pa.string())})
    native = _coordinator_fail_closed(
        tmp_path, [_fpe_column(preserve_separators=False)], source, batch_rows=50_000
    )
    handler = _handler_error(values, {"preserve_separators": False})
    assert handler.code == "fpe_unencryptable_domain"
    # The mutation is observable pre-fallback: the native code no longer equals the handler's.
    assert native.code != handler.code
    assert native.code == (
        "fpe_wrong_code_mutant" if mutation == "wrong_code" else "fpe_unencryptable_value"
    )


# ── Unified-slice ExecutionResult boundary (flag-off vs flag-on) ──


@pytest.mark.parametrize("label, values", _SHAPES, ids=[s[0] for s in _SHAPES])
def test_unified_slice_execution_result_byte_identical(
    tmp_path: Path, label: str, values: list[Any]
) -> None:
    source = pa.table({"c": pa.array(values, type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "fpe")
    config = build_config(tmp_path, "t", path, [_fpe_column()])
    off, on = _run(config, path, flag=False), _run(config, path, flag=True)
    ot, nt = off.outputs["t"], on.outputs["t"]
    assert ot.schema.field("c").type == nt.schema.field("c").type
    assert ot.column("c").to_pylist() == nt.column("c").to_pylist()
    assert ot.schema.equals(nt.schema, check_metadata=True)
    assert sorted(w.code for w in off.warnings) == sorted(w.code for w in on.warnings)
    if native_companion_status().ok:
        assert QUALITY_METRICS_KEY in on.quality_metrics
        nodes = on.quality_metrics[QUALITY_METRICS_KEY]["nodes"]
        assert nodes
        for ev in nodes.values():
            assert ev["operator"] == "native_fpe"
            assert ev["executed"] is True
            # Like hash, the FF1 kernel is invoked for every chunk, empty and all-null included,
            # so positive compiled-kernel evidence is always present.
            assert ev["compiled_kernel_executed"] is True
    else:
        assert QUALITY_METRICS_KEY not in on.quality_metrics


def test_unified_slice_parquet_round_trip(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(_SSNS, type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "fpe")
    config = build_config(tmp_path, "t", path, [_fpe_column()])
    off, on = _run(config, path, flag=False), _run(config, path, flag=True)
    pq.write_table(off.outputs["t"], tmp_path / "off.parquet")
    pq.write_table(on.outputs["t"], tmp_path / "on.parquet")
    off_back, on_back = (
        pq.read_table(tmp_path / "off.parquet"),
        pq.read_table(tmp_path / "on.parquet"),
    )
    assert off_back.schema.equals(on_back.schema, check_metadata=True)
    assert off_back.column("c").to_pylist() == on_back.column("c").to_pylist()


# ── Checksum / companion / source-type declines ──


def test_checksum_declines_to_oracle(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["4111111111111111"], type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "fpe")
    config = build_config(tmp_path, "t", path, [_fpe_column(checksum="luhn")])
    off, on = _run(config, path, flag=False), _run(config, path, flag=True)
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert off.outputs["t"].column("c").to_pylist() == on.outputs["t"].column("c").to_pylist()


def test_companion_absent_declines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution import _unified_slice_admission as admission

    monkeypatch.setattr(
        admission,
        "native_kernel_availability",
        lambda: KernelAvailability(crypto=True, index=True, raw_hex=True, fpe=False),
    )
    source = pa.table({"c": pa.array(_SSNS, type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "fpe")
    config = build_config(tmp_path, "t", path, [_fpe_column()])
    off, on = _run(config, path, flag=False), _run(config, path, flag=True)
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert off.outputs["t"].equals(on.outputs["t"])


@pytest.mark.parametrize(
    "typ, values",
    [
        (pa.large_string(), ["123456789", "987654321"]),
        (pa.int64(), [123456789, 987654321]),
    ],
    ids=["large_string", "int64"],
)
def test_nonstring_source_declines(tmp_path: Path, typ: pa.DataType, values: list[Any]) -> None:
    source = pa.table({"c": pa.array(values, type=typ)})
    path = write_read_only_fixture(tmp_path, source, "fpe")
    config = build_config(tmp_path, "t", path, [_fpe_column()])
    on = _run(config, path, flag=True)
    assert QUALITY_METRICS_KEY not in on.quality_metrics


def test_fpe_requires_the_fpe_kernel() -> None:
    from decoy_engine.execution import _unified_slice_admission as admission

    assert admission._OPERATOR_REQUIRED_KERNEL["native_fpe"] == "fpe"
    assert "native_fpe" in admission._COMPANION_DEPENDENT_OPERATOR_IDS
