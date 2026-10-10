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
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import run_pipeline
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY
from decoy_engine.execution.native._companion_status import (
    KernelAvailability,
    native_companion_status,
)
from decoy_engine.execution.native._crypto_ext import FpeConfig
from decoy_engine.execution.native._crypto_reference import reference_fpe
from decoy_engine.execution.native._fpe_ext import native_fpe
from decoy_engine.keyprovider import SecretKeyProvider
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
