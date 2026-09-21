"""S-slate acceptance: native deterministic group_key.

Byte-identity (value AND Arrow FIELD TYPE) vs the pandas oracle is the merge
gate, proven at two boundaries: the shadow coordinator's assembled output
(`run_shadow_and_oracle` + `assert_shadow_matches_oracle`) and the production
unified-slice `ExecutionResult` (flag-off vs flag-on). The seam is proven both
ways: the FULL-FRAME route EXECUTES native group_key (positive route/kernel
evidence), and the CHUNKED route DECLINES it to the oracle (group_key is
sibling-keyed, kept out of the chunk-safe set).

group_key keys on a SIBLING `group_by` column, not the target: v1 admits ONLY
an UNMASKED (passthrough) sibling of a safe type (string, large_string, int*,
bool, date, timestamp; float/decimal/dictionary excluded). A masked sibling, a
non-resident sibling, or an unsafe-typed sibling declines to the oracle. The
canonicalization-free differential proves the raw path is taken: on a
decomposed-unicode / int / bool / date sibling the native key matches the raw
oracle, where a canonicalizing derive would DIVERGE.

The native-shadow-path tests require the compiled companion (the
`derive_hex_raw_batch` kernel) and skip without it, like the categorical /
bucket_perturb suites; the production companion-absent behavior (group_key
declines to the pandas oracle) is covered by the `run_pipeline` tests, which run
without the companion.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.determinism._derive import derive
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._chunked_profile import first_chunk_profile
from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY
from decoy_engine.execution.native._companion_status import native_companion_status
from decoy_engine.execution.native._dispatch import plan_native_route
from decoy_engine.execution.native._group_key_kernel import native_group_key
from decoy_engine.execution.native._plan import native_route_eligibility
from decoy_engine.execution.physical._plan import ExecutionBinding, KeyBinding
from decoy_engine.execution.physical._shadow_operators import OperatorCallEvidence, run_operator
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.transforms.group_key import GroupKeyConfig, apply_group_key
from tests.physical._shadow_helpers import (
    ENGINE_VERSION,
    assert_every_node_bound,
    assert_route_evidence_matches_plan,
    assert_shadow_matches_oracle,
    build_config,
    run_shadow_and_oracle,
    write_read_only_fixture,
)

_MASK_KEY = bytes(range(32))
_TARGET = "key"
_GB = "gb"


def _kp() -> SecretKeyProvider:
    return SecretKeyProvider(secret=_MASK_KEY, key_version="v1")


def _gk_columns(
    *,
    length: int = 16,
    prefix: str = "",
    group_by: str = _GB,
    gb_strategy: str = "passthrough",
    gb_provider_config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Config for a 2-column table: the group_by SIBLING (gb_strategy, default
    passthrough) and the target group_key column keyed on it."""
    gb_col: dict[str, Any] = {"name": _GB, "strategy": gb_strategy}
    if gb_provider_config is not None:
        gb_col["provider_config"] = gb_provider_config
    target_col: dict[str, Any] = {
        "name": _TARGET,
        "strategy": "group_key",
        "provider_config": {"group_by": group_by, "length": length, "prefix": prefix},
    }
    return [gb_col, target_col]


def _source(gb: pa.Array) -> pa.table:
    # The target column's own source values are irrelevant (group_key overwrites
    # them); a plain string placeholder keeps it native-admissible as passthrough
    # only when itself unmasked -- but here the target IS the group_key node, so
    # its source values never survive.
    return pa.table({_GB: gb, _TARGET: pa.array(["seed"] * len(gb), type=pa.string())})


# The native shadow path REQUIRES the compiled companion; skip when absent.
_NEEDS_COMPANION = pytest.mark.skipif(
    not native_companion_status().ok,
    reason="compiled decoy-engine-native companion unavailable; the native group_key path requires it",
)


# ── Operator-level byte-parity vs the oracle across the full sibling matrix ──
# The unified-slice route can only ACTIVATE a string/int64/bool sibling (the
# passthrough sibling's own resident-type gate), so the full admitted type matrix
# (large_string / int32 / uint64 / date / timestamp / tz) is proven here, at the
# operator, against the pandas oracle transform directly.


def _oracle_keys(gb: pa.Array, *, length: int, prefix: str) -> list[str]:
    # Build the oracle's frame the SAME way the pandas adapter does for a
    # group_key sibling: `to_pandas_fk_safe` (lossless nullable typing for an
    # integer sibling), never a plain `to_pandas()` (which would widen int+null
    # to float64 and diverge). This is what the production run_pipeline oracle
    # actually reads through, so it is the faithful operator-level oracle.
    from decoy_engine.execution._fk_keys import to_pandas_fk_safe

    df = to_pandas_fk_safe(_source(gb), {_GB})
    cfg = GroupKeyConfig(group_by=_GB, length=length, prefix=prefix)
    return apply_group_key(cfg, df, seed=_MASK_KEY, namespace=f"group_key/{_TARGET}")


_SIBLINGS: dict[str, pa.Array] = {
    "string": pa.array(["alice", "bob", "alice", ""], type=pa.string()),
    "string_nonNFC": pa.array(["é", "é", "café"], type=pa.string()),
    "string_null": pa.array(["a", None, "b", None], type=pa.string()),
    "large_string": pa.array(["x", "y", "x"], type=pa.large_string()),
    "int64": pa.array([1, 2, 1, 3], type=pa.int64()),
    "int64_null": pa.array([1, None, 2, 1], type=pa.int64()),
    "int64_big_null": pa.array([2**60, None, 2**53 + 1], type=pa.int64()),
    "int32_null": pa.array([5, None, 6], type=pa.int32()),
    "uint64_big_null": pa.array([2**63 + 5, None, 10], type=pa.uint64()),
    "bool": pa.array([True, False, True], type=pa.bool_()),
    "bool_null": pa.array([True, None, False], type=pa.bool_()),
    "date32": pa.array([dt.date(2020, 1, 1), dt.date(1999, 12, 31), dt.date(2020, 1, 1)], type=pa.date32()),
    "timestamp_us": pa.array(
        [dt.datetime(2020, 1, 1, 12), dt.datetime(1999, 12, 31, 23, 59, 59)], type=pa.timestamp("us")
    ),
    "timestamp_tz": pa.array(
        [dt.datetime(2020, 1, 1, 12), dt.datetime(1999, 6, 1)], type=pa.timestamp("us", tz="US/Eastern")
    ),
    "all_null": pa.array([None, None], type=pa.string()),
    "single": pa.array(["solo"], type=pa.string()),
}


@_NEEDS_COMPANION
@pytest.mark.parametrize("length", [8, 16, 64])
@pytest.mark.parametrize("prefix", ["", "gk_"])
@pytest.mark.parametrize("label", list(_SIBLINGS))
def test_operator_matches_oracle_byte_identical(label: str, length: int, prefix: str) -> None:
    gb = _SIBLINGS[label]
    native = native_group_key(
        gb,
        length=length,
        prefix=prefix,
        mask_key=_MASK_KEY,
        namespace=f"group_key/{_TARGET}",
        native_threads=1,
    )
    oracle = _oracle_keys(gb, length=length, prefix=prefix)
    assert native.type == pa.string()
    assert native.to_pylist() == oracle
    # group_key NEVER emits a null (a null cell keys on "None").
    assert native.null_count == 0


@_NEEDS_COMPANION
def test_empty_sibling_operator_is_empty_string_array() -> None:
    native = native_group_key(
        pa.array([], type=pa.string()),
        length=16,
        prefix="p",
        mask_key=_MASK_KEY,
        namespace=f"group_key/{_TARGET}",
        native_threads=1,
    )
    assert native.type == pa.string()
    assert len(native) == 0


@_NEEDS_COMPANION
def test_same_group_same_key_and_distinct_differ() -> None:
    gb = pa.array(["h1", "h2", "h1", "h3", "h2"], type=pa.string())
    out = native_group_key(
        gb, length=16, prefix="", mask_key=_MASK_KEY, namespace=f"group_key/{_TARGET}"
    ).to_pylist()
    assert out[0] == out[2]  # both h1
    assert out[1] == out[4]  # both h2
    assert len({out[0], out[1], out[3]}) == 3  # distinct groups -> distinct keys


# ── Canonicalization-free proof (raw path is taken) ──────────────────────────


@_NEEDS_COMPANION
@pytest.mark.parametrize(
    "gb, diverges",
    [
        (pa.array(["é", "café"], type=pa.string()), True),  # decomposed unicode (NFC)
        (pa.array([1, 2, 3], type=pa.int64()), True),  # int length-prefix
        (pa.array([True, False], type=pa.bool_()), True),  # bool special-encode
        (pa.array([dt.date(2021, 6, 1), dt.date(1990, 1, 2)], type=pa.date32()), False),  # str-equal
    ],
    ids=["nonNFC", "int", "bool", "date"],
)
def test_canonicalization_free_differential(gb: pa.Array, diverges: bool) -> None:
    """Native must match the RAW oracle (derive over str(value).encode()) on
    every input -- proving the raw path is taken, not a canonicalizing one. For
    the inputs where the engine's MASK canonicalizer (`canonicalize_derive_
    source`) actually changes the bytes (non-NFC / int / bool), native must also
    DIFFER from a canonicalizing derivation. (A date's canonicalized form is its
    own `str()`, so it does not diverge at the Python layer; the Rust
    canonicalize-free property is pinned separately in the crate tests.)"""
    from decoy_engine.kernel._canonicalize import canonicalize_derive_source

    length = 16
    native = native_group_key(
        gb, length=length, prefix="", mask_key=_MASK_KEY, namespace=f"group_key/{_TARGET}"
    ).to_pylist()
    df = _source(gb).to_pandas()
    raw = [
        derive(_MASK_KEY, f"group_key/{_TARGET}", str(v).encode())[: length // 2].hex()
        for v in df[_GB]
    ]
    assert native == raw
    canon = [
        derive(_MASK_KEY, f"group_key/{_TARGET}", canonicalize_derive_source(v))[: length // 2].hex()
        for v in df[_GB]
    ]
    if diverges:
        assert native != canon, "native must differ from a canonicalizing derivation (raw path proof)"


# ── Stringify parity: astype(str) == oracle element-wise str() ───────────────


@_NEEDS_COMPANION
@pytest.mark.parametrize("label", list(_SIBLINGS))
def test_stringify_parity(label: str) -> None:
    gb = _SIBLINGS[label]
    series = _source(gb).to_pandas()[_GB]
    assert list(series.astype(str)) == [str(v) for v in series]


# ── Operator KAT (drift guard) ───────────────────────────────────────────────


@_NEEDS_COMPANION
def test_operator_kat() -> None:
    gb = pa.array(["alice", "bob", "alice", "carol"], type=pa.string())
    out = native_group_key(
        gb, length=16, prefix="H-", mask_key=bytes(range(32)), namespace="group_key/household_id"
    )
    assert out.to_pylist() == [
        "H-ba38a11936bfef1a",
        "H-5a27b088b50251d3",
        "H-ba38a11936bfef1a",
        "H-24de696ee019b77e",
    ]


# ── Coordinator byte-parity: full pipeline, sibling passthrough + target ─────
# String/int/bool siblings whose passthrough node the coordinator also binds.

_COORD_SHAPES: list[tuple[str, pa.Array]] = [
    ("string_dup_empty", pa.array(["a", "b", "a", "", "c"], type=pa.string())),
    ("string_null", pa.array(["a", None, "b", None], type=pa.string())),
    ("int64", pa.array([1, 2, 1, 3], type=pa.int64())),
    ("bool", pa.array([True, False, True], type=pa.bool_())),
    ("single", pa.array(["solo"], type=pa.string())),
    ("empty", pa.array([], type=pa.string())),
    ("all_null", pa.array([None, None], type=pa.string())),
]


@_NEEDS_COMPANION
@pytest.mark.parametrize("length", [8, 16, 64])
@pytest.mark.parametrize("prefix", ["", "gk_"])
@pytest.mark.parametrize("label, gb", _COORD_SHAPES, ids=[s[0] for s in _COORD_SHAPES])
def test_coordinator_matches_oracle_byte_identical(
    tmp_path: Path, length: int, prefix: str, label: str, gb: pa.Array
) -> None:
    source = _source(gb)
    write_read_only_fixture(tmp_path, source, "gk")
    config = build_config(
        tmp_path, "t", tmp_path / "gk.parquet", _gk_columns(length=length, prefix=prefix)
    )
    run = run_shadow_and_oracle(config, "t", source, key_provider=_kp())
    assert_every_node_bound(run.plan)
    assert_shadow_matches_oracle(run)
    assert_route_evidence_matches_plan(run)


# ── Seam proof: full-frame EXECUTES native group_key ─────────────────────────


@_NEEDS_COMPANION
def test_full_frame_executes_native_group_key(tmp_path: Path) -> None:
    source = _source(pa.array(["a", "b", "a"], type=pa.string()))
    write_read_only_fixture(tmp_path, source, "gk")
    config = build_config(tmp_path, "t", tmp_path / "gk.parquet", _gk_columns())
    run = run_shadow_and_oracle(config, "t", source, key_provider=_kp())
    gk_ev = [
        ev for ev in run.shadow.route_evidence.values() if ev.actual_operator == "native_group_key"
    ]
    assert len(gk_ev) == 1
    assert gk_ev[0].executed is True
    assert gk_ev[0].compiled_kernel_executed is True


# ── Seam proof: chunked route DECLINES group_key to the oracle ───────────────


def test_chunked_route_declines_group_key(tmp_path: Path) -> None:
    source = _source(pa.array(["a", "b", "c"], type=pa.string()))
    write_read_only_fixture(tmp_path, source, "gk")
    config = build_config(tmp_path, "t", tmp_path / "gk.parquet", _gk_columns())
    profile = first_chunk_profile(source, table="t", engine_version=ENGINE_VERSION)
    preflight = plan_native_route(
        config, profile, table="t", engine_version=ENGINE_VERSION, first_schema=source.schema
    )
    assert preflight.evidence.native_admitted is False
    assert "group_key_not_native_chunked_route:key" in (preflight.evidence.reroute_reason or "")


# ── Admission declines (config-only native_route_eligibility) ────────────────


def _eligibility(tmp_path: Path, source: pa.Table, columns: list[dict]):
    write_read_only_fixture(tmp_path, source, "x")
    config = build_config(tmp_path, "t", tmp_path / "x.parquet", columns)
    profile = first_chunk_profile(source, table="t", engine_version=ENGINE_VERSION)
    return native_route_eligibility(config, table="t", profile=profile)


def test_float_sibling_declines(tmp_path: Path) -> None:
    source = pa.table(
        {_GB: pa.array([1.0, 2.0, 1.0], type=pa.float64()), _TARGET: pa.array(["s", "s", "s"])}
    )
    result = _eligibility(tmp_path, source, _gk_columns())
    assert not result.accepted
    assert any(r.startswith("group_key_group_by_type_not_native:key:gb:") for r in result.rejections), (
        result.rejections
    )


def test_missing_group_by_declines(tmp_path: Path) -> None:
    # A group_key config with no group_by is rejected at plan-compile normally;
    # the native-eligibility query reports the coded decline from raw config.
    source = _source(pa.array(["a", "b"], type=pa.string()))
    write_read_only_fixture(tmp_path, source, "x")
    columns = [
        {"name": _GB, "strategy": "passthrough"},
        {"name": _TARGET, "strategy": "group_key", "provider_config": {"length": 16}},
    ]
    config = build_config(tmp_path, "t", tmp_path / "x.parquet", columns)
    result = native_route_eligibility(config, table="t")
    assert not result.accepted
    assert any(r == "group_key_requires_group_by:key" for r in result.rejections), result.rejections


# ── Unified-slice ExecutionResult boundary (flag-off vs flag-on) ─────────────


def _run_both(tmp_path: Path, source: pa.Table, columns: list[dict]):
    path = write_read_only_fixture(tmp_path, source, "fixture")
    config = build_config(tmp_path, "t", path, columns)
    off = run_pipeline(
        config,
        {"t": pq.read_table(path)},
        engine_version=ENGINE_VERSION,
        key_provider=_kp(),
        unified_slice_enabled=False,
    )
    on = run_pipeline(
        config,
        {"t": pq.read_table(path)},
        engine_version=ENGINE_VERSION,
        key_provider=_kp(),
        unified_slice_enabled=True,
    )
    return off, on


_PROD_SHAPES: list[tuple[str, pa.Array]] = [
    ("string", pa.array(["a", "b", "a", "c", ""], type=pa.string())),
    ("string_null", pa.array(["a", None, "b"], type=pa.string())),
    ("int64", pa.array([1, 2, 1, 3], type=pa.int64())),
    ("bool", pa.array([True, False, True], type=pa.bool_())),
    ("single", pa.array(["solo"], type=pa.string())),
    ("all_null", pa.array([None, None], type=pa.string())),
]


@pytest.mark.parametrize("length", [8, 16, 64])
@pytest.mark.parametrize("prefix", ["", "gk_"])
@pytest.mark.parametrize("label, gb", _PROD_SHAPES, ids=[s[0] for s in _PROD_SHAPES])
def test_unified_slice_execution_result_byte_identical(
    tmp_path: Path, length: int, prefix: str, label: str, gb: pa.Array
) -> None:
    source = _source(gb)
    off, on = _run_both(tmp_path, source, _gk_columns(length=length, prefix=prefix))
    ot, nt = off.outputs["t"], on.outputs["t"]
    # The HARD gate is value + Arrow field type parity per column (the plan's
    # byte-identity gate), for BOTH the group_key target and the passthrough
    # sibling. The `pandas` schema-metadata sidecar is NOT compared: for an
    # integer sibling the legacy adapter routes group_key siblings through
    # lossless nullable typing (numpy_type "Int64"), while the native passthrough
    # keeps plain "int64" -- the ARROW data (values, field type, null bitmap) is
    # identical, only the pandas reconstruction hint differs (a known benign
    # artifact the shadow comparator also never compares).
    for name in ot.column_names:
        assert ot.schema.field(name).type == nt.schema.field(name).type, name
        assert ot.column(name).to_pylist() == nt.column(name).to_pylist(), name
    if native_companion_status().ok:
        # The lane actually ACTIVATED (not a decline that would pass against
        # itself); the group_key node ran native.
        assert QUALITY_METRICS_KEY in on.quality_metrics
        node_ev = on.quality_metrics[QUALITY_METRICS_KEY]["nodes"]
        gk = [ev for ev in node_ev.values() if ev["operator"] == "native_group_key"]
        assert len(gk) == 1 and gk[0]["executed"] is True


def test_unified_slice_empty_column_is_float64_on_both_arms(tmp_path: Path) -> None:
    """The empty-frame golden: an empty group_key column is Arrow float64 on
    BOTH arms (the tokenizing empty rule), pinned as a concrete type."""
    source = _source(pa.array([], type=pa.string()))
    off, on = _run_both(tmp_path, source, _gk_columns())
    ot, nt = off.outputs["t"], on.outputs["t"]
    assert ot.column(_TARGET).to_pylist() == nt.column(_TARGET).to_pylist() == []
    assert ot.schema.field(_TARGET).type == pa.float64()
    assert nt.schema.field(_TARGET).type == pa.float64()
    assert ot.schema.equals(nt.schema, check_metadata=True)


def test_unified_slice_all_null_sibling_yields_non_null_keys(tmp_path: Path) -> None:
    """An all-null group_by sibling yields NON-null string keys (str(None) =
    "None"), never a null column -- on both arms."""
    source = _source(pa.array([None, None, None], type=pa.string()))
    off, on = _run_both(tmp_path, source, _gk_columns())
    ot, nt = off.outputs["t"], on.outputs["t"]
    assert ot.column(_TARGET).to_pylist() == nt.column(_TARGET).to_pylist()
    assert all(v is not None for v in nt.column(_TARGET).to_pylist())
    assert nt.schema.field(_TARGET).type == pa.string()


def test_unified_slice_masked_group_by_declines(tmp_path: Path) -> None:
    """Order-dependence: when the group_by sibling is itself MASKED (redact, not
    passthrough), the native route DECLINES -- both arms take the oracle and
    agree, with no activation. Proves the masked-before decline."""
    source = _source(pa.array(["a", "b", "a"], type=pa.string()))
    columns = _gk_columns(gb_strategy="redact")
    off, on = _run_both(tmp_path, source, columns)
    ot, nt = off.outputs["t"], on.outputs["t"]
    assert ot.column(_TARGET).to_pylist() == nt.column(_TARGET).to_pylist()
    assert ot.column(_GB).to_pylist() == nt.column(_GB).to_pylist()
    assert ot.schema.field(_TARGET).type == nt.schema.field(_TARGET).type
    # DECLINED: the unified slice never activated for this table.
    assert QUALITY_METRICS_KEY not in on.quality_metrics


def test_unified_slice_float_sibling_declines(tmp_path: Path) -> None:
    """A float group_by sibling is outside the safe set; the native route
    declines and both arms agree (no activation)."""
    source = pa.table(
        {_GB: pa.array([1.5, 2.5, 1.5], type=pa.float64()), _TARGET: pa.array(["s", "s", "s"])}
    )
    # gb must be configured with an in-slice strategy for cheap admission; a
    # float passthrough is itself not in passthrough's admitted resident set, so
    # the table declines regardless -- either way, both arms must agree.
    off, on = _run_both(tmp_path, source, _gk_columns())
    ot, nt = off.outputs["t"], on.outputs["t"]
    assert ot.column(_TARGET).to_pylist() == nt.column(_TARGET).to_pylist()
    assert QUALITY_METRICS_KEY not in on.quality_metrics


def test_unified_slice_int_null_sibling_declines(tmp_path: Path) -> None:
    """An integer group_by sibling that carries a NULL declines to the oracle at
    the production boundary (the pre-existing cheap-admission gate: an int+null
    column round-trips lossy through plain pandas), so both arms take the legacy
    route and agree byte-for-byte with no activation. The operator-level
    stringify parity (native `to_pandas_fk_safe` == oracle) is proven separately;
    this pins that the end-to-end lane fails CLOSED for the shape it cannot yet
    admit rather than diverging."""
    source = pa.table(
        {_GB: pa.array([1, None, 2**60, 1], type=pa.int64()), _TARGET: pa.array(["s"] * 4)}
    )
    off, on = _run_both(tmp_path, source, _gk_columns())
    ot, nt = off.outputs["t"], on.outputs["t"]
    for name in ot.column_names:
        assert ot.schema.field(name).type == nt.schema.field(name).type, name
        assert ot.column(name).to_pylist() == nt.column(name).to_pylist(), name
    assert QUALITY_METRICS_KEY not in on.quality_metrics


def test_unified_slice_parquet_round_trip(tmp_path: Path) -> None:
    source = _source(pa.array(["a", "b", None, "a", "c"], type=pa.string()))
    off, on = _run_both(tmp_path, source, _gk_columns(length=32, prefix="Z"))
    off_path, on_path = tmp_path / "off.parquet", tmp_path / "on.parquet"
    pq.write_table(off.outputs["t"], off_path)
    pq.write_table(on.outputs["t"], on_path)
    off_back, on_back = pq.read_table(off_path), pq.read_table(on_path)
    assert off_back.schema.equals(on_back.schema, check_metadata=True)
    assert off_back.column(_TARGET).to_pylist() == on_back.column(_TARGET).to_pylist()


# ── Runtime fail-closed guards at dispatch ───────────────────────────────────


def _binding(**overrides: Any) -> ExecutionBinding:
    base: dict[str, Any] = dict(
        operator_id="native_group_key",
        operator_reason="test",
        resolved_config=(),
        input_schema=pa.schema([pa.field(_GB, pa.string())]),
        output_schema=pa.schema([pa.field(_TARGET, pa.string())]),
        determinism_family="source_keyed_hmac",
        determinism_version=1,
        key_binding=KeyBinding(key_source="mask_key", namespace=f"group_key/{_TARGET}"),
        diagnostic_obligations=(),
        required_prepasses=(),
        batch_estimate=None,
        group_key_group_by=_GB,
        group_key_length=16,
        group_key_prefix="",
    )
    base.update(overrides)
    return ExecutionBinding(**base)


def test_run_operator_asserts_group_key_binding_present() -> None:
    binding = _binding(key_binding=None)
    ctx = SimpleNamespace(mask_key=_MASK_KEY, native_threads=None)
    evidence = OperatorCallEvidence(planned_operator="native_group_key")
    with pytest.raises(AssertionError, match="no KeyBinding/group_by/length"):
        run_operator(
            pa.array(["a"], type=pa.string()),
            binding=binding,
            ctx=ctx,  # type: ignore[arg-type]
            evidence=evidence,
        )
