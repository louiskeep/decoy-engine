"""S-slate acceptance: native deterministic bucket_perturb.

Byte-identity (value AND Arrow FIELD TYPE) vs the pandas oracle is the merge
gate, proven at two boundaries: the shadow coordinator's assembled output
(`run_shadow_and_oracle` + `assert_shadow_matches_oracle`, which hard-compares
`schema.field(...).type`) and the production unified-slice `ExecutionResult`
(flag-off vs flag-on). The seam is proven both ways: the FULL-FRAME route
EXECUTES native bucket_perturb (positive route/kernel evidence), and the CHUNKED
route DECLINES it to the oracle (v1 scope). Admission declines are asserted
positively (no-date_format, non-string, missing-namespace, unsupported-bucket).

The native-shadow-path tests require the compiled companion (the `derive_index_
batch` kernel) and skip without it, exactly like the categorical suite; the
production companion-absent behavior (bucket_perturb declines to the pandas
oracle) is covered by the `run_pipeline` tests, which run without the companion.

Empty-input note: an EMPTY column's zero-row Arrow type label is the one place
the legacy per-strategy oracle (which types an empty bucket_perturb column
`null`, passing the source through) and the unified-slice finalizer (which
normalizes an empty masked column to `float64`, like every tokenizing operator)
disagree. Empty parity is therefore proven at the coordinator boundary, whose
oracle uses the same finalizer contract (`float64`); the unified-slice matrix
covers empty for VALUE parity and pins the finalizer's `float64` type
explicitly. No data is affected -- the difference is a zero-row type label only.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.determinism import derive
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._chunked_profile import first_chunk_profile
from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY
from decoy_engine.execution.native._bucket_perturb_ext import native_bucket_perturb
from decoy_engine.execution.native._companion_status import native_companion_status
from decoy_engine.execution.native._dispatch import plan_native_route
from decoy_engine.execution.native._index_ext import load_compiled_index_kernel
from decoy_engine.execution.native._plan import native_route_eligibility
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._plan import ExecutionBinding, KeyBinding
from decoy_engine.execution.physical._shadow_operators import OperatorCallEvidence, run_operator
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.transforms.bucket_perturb import apply_bucket_perturb
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
_BUCKETS = ("week", "month", "quarter")
_FMT = "%Y-%m-%d"


def _kp() -> SecretKeyProvider:
    return SecretKeyProvider(secret=_MASK_KEY, key_version="v1")


def _bp_column(
    pc: dict[str, Any] | None = None,
    *,
    bucket: str = "month",
    date_format: str | None = _FMT,
    namespace: str | None = "ns",
) -> dict:
    provider_config: dict[str, Any] = dict(pc) if pc is not None else {}
    provider_config.setdefault("bucket", bucket)
    if date_format is not None:
        provider_config.setdefault("date_format", date_format)
    col: dict[str, Any] = {
        "name": "c",
        "strategy": "bucket_perturb",
        "provider_config": provider_config,
    }
    if namespace is not None:
        col["namespace"] = namespace
    return col


# These tests drive the native shadow/coordinator path directly, which REQUIRES
# the compiled companion; skip when absent (the CI substrate(pandas) leg). The
# production companion-absent decline is covered by the run_pipeline tests below.
_NEEDS_COMPANION = pytest.mark.skipif(
    not native_companion_status().ok,
    reason="compiled decoy-engine-native companion unavailable; the native shadow path requires it",
)


# ── Byte-identity: coordinator vs oracle (value + Arrow field type) ──

# Leap + boundary edges: Feb 2024 = 29 days, Feb 2023 = 28; Q1 2024 = 91 vs
# Q1 2023 = 90; month/quarter first + last days; a duplicate + unicode + a
# non-ASCII parse-fail; nulls interleaved.
_RICH = [
    "2024-02-29",
    "2023-02-28",
    "2024-01-01",
    "2024-12-31",
    "2024-03-31",
    "2023-03-31",
    "2024-01-31",
    "2024-06-30",
    "2024-09-30",
    "2024-02-29",  # duplicate
    None,
    "not-a-date",
    "café",  # unicode parse-fail passes through unchanged
    "2020-02-29",
    "2000-02-29",
    "1999-11-15",
]

_SHAPES: list[tuple[str, list]] = [
    ("rich_leap_boundary", _RICH),
    ("populated", ["2024-05-15", "2023-08-20", "2021-01-10", "2024-11-30"]),
    ("nulls_only", [None, None, None]),
    ("parse_fails_only", ["x", "yy", "zzz"]),
    ("mixed", ["2024-02-15", None, "bad", "2023-11-30"]),
    ("dup_unicode", ["2024-01-01", "2024-01-01", "日付", None]),
    ("single", ["2024-07-04"]),
    ("empty", []),
    ("all_null", [None, None]),
    ("all_parse_fail", ["nope", "xxx"]),
]


@_NEEDS_COMPANION
@pytest.mark.parametrize("bucket", _BUCKETS)
@pytest.mark.parametrize("label, values", _SHAPES, ids=[s[0] for s in _SHAPES])
def test_coordinator_matches_oracle_byte_identical(
    tmp_path: Path, bucket: str, label: str, values: list
) -> None:
    source = pa.table({"c": pa.array(values, type=pa.string())})
    write_read_only_fixture(tmp_path, source, "bp")
    config = build_config(tmp_path, "t", tmp_path / "bp.parquet", [_bp_column(bucket=bucket)])
    run = run_shadow_and_oracle(config, "t", source, key_provider=_kp())
    assert_every_node_bound(run.plan)
    assert_shadow_matches_oracle(run)  # value + null + order + row-count + SCHEMA (field type)
    assert_route_evidence_matches_plan(run)


# ── Direct kernel differential vs the pandas oracle transform ────────
# Covers group-by-size scatter correctness (a month column spanning 28/29/30/31,
# a quarter spanning 90/91/92), parse strictness, and strftime across formats.

_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%Y%m%d", "%m/%d/%y", "%Y-%m-%dT%H:%M:%S", "%d-%b-%Y", "%j-%Y"]

# Spans every month length + a leap Feb, so month grouping exercises {28,29,30,31}
# and quarter grouping exercises {90,91,92} in one column.
_ALL_MONTH_LENGTHS = [
    "2024-02-29",  # 29
    "2023-02-28",  # 28
    "2024-04-30",  # 30
    "2024-01-31",  # 31
    "2024-11-30",  # 30
    "2024-08-31",  # 31
    "2023-04-30",  # 30
    "2024-05-31",  # 31
]


@_NEEDS_COMPANION
@pytest.mark.parametrize("bucket", _BUCKETS)
@pytest.mark.parametrize("fmt", _FORMATS)
def test_native_kernel_matches_oracle_transform(bucket: str, fmt: str) -> None:
    kernel = load_compiled_index_kernel()
    # Reformat the leap/length corpus into `fmt`, add null + parse-fail + dup.
    base = [pd.Timestamp(d).strftime(fmt) for d in _ALL_MONTH_LENGTHS]
    values = [*base, base[0], None, "not-a-date", "", "café"]
    array = pa.array(values, type=pa.string())
    native = native_bucket_perturb(
        array,
        bucket=bucket,
        date_format=fmt,
        mask_key=_MASK_KEY,
        namespace="diff_ns",
        index_kernel=kernel,
    )
    oracle = apply_bucket_perturb(
        pd.Series(values, dtype=object), bucket, _MASK_KEY, "diff_ns", fmt
    )
    assert native.to_pylist() == list(oracle)


@_NEEDS_COMPANION
def test_native_kernel_parse_strictness_matches_oracle() -> None:
    """A pandas parse-strictness corpus: an oracle-valid date must never become
    a native parse-fail (or vice versa) -- both call the SAME
    pd.to_datetime(errors='coerce')."""
    kernel = load_compiled_index_kernel()
    corpus = [
        "2024-1-1",
        "2024-01-1",
        " 2024-01-01",
        "2024-01-01 ",
        "2024/01/01",
        "2024-00-10",
        "2024-01-00",
        "2024-02-30",
        "0001-01-01",
        "9999-12-31",
        "2024-01-01x",
        "not",
        "",
        "   ",
        None,
        "2024-01-01",
    ]
    for bucket in _BUCKETS:
        native = native_bucket_perturb(
            pa.array(corpus, type=pa.string()),
            bucket=bucket,
            date_format="%Y-%m-%d",
            mask_key=_MASK_KEY,
            namespace="strict_ns",
            index_kernel=kernel,
        )
        oracle = apply_bucket_perturb(
            pd.Series(corpus, dtype=object), bucket, _MASK_KEY, "strict_ns", "%Y-%m-%d"
        )
        assert native.to_pylist() == list(oracle), bucket


# ── derive_index_batch(pool_size=size) == oracle offset differential ──


@_NEEDS_COMPANION
@pytest.mark.parametrize("size", [7, 28, 29, 30, 31, 90, 91, 92])
def test_derive_index_batch_equals_oracle_offset(size: int) -> None:
    """The keyed offset the kernel draws for `pool_size=size` is byte-identical
    to the oracle's per-row `int.from_bytes(derive(...)[:8], "big") % size`
    (incl. the shared canonicalizer)."""
    kernel = load_compiled_index_kernel()
    values = [f"2024-value-{i}" for i in range(50)] + ["café", "日付", "dup", "dup"]
    ns = "off_ns"
    idx = kernel.derive_index_batch(
        pa.array(values, type=pa.string()), mask_key=_MASK_KEY, namespace=ns, pool_size=size
    )
    expected = [
        int.from_bytes(derive(_MASK_KEY, ns, _canonicalize_source(v))[:8], "big") % size
        for v in values
    ]
    assert idx.to_pylist() == expected


# ── KAT: fixed config+seed corpus per bucket, guarding drift ─────────

_KAT_CORPUS = [
    "2024-01-15",
    "2024-02-29",
    "2023-02-28",
    "2024-07-01",
    "2024-12-31",
    "2020-11-30",
    "1999-03-14",
]
_KAT_EXPECTED = {
    "week": [
        "2024-01-21",
        "2024-02-26",
        "2023-02-28",
        "2024-07-04",
        "2024-12-30",
        "2020-12-04",
        "1999-03-09",
    ],
    "month": [
        "2024-01-12",
        "2024-02-08",
        "2023-02-16",
        "2024-07-18",
        "2024-12-26",
        "2020-11-20",
        "1999-03-01",
    ],
    "quarter": [
        "2024-02-04",
        "2024-02-12",
        "2023-03-25",
        "2024-07-20",
        "2024-10-08",
        "2020-11-19",
        "1999-03-02",
    ],
}


@_NEEDS_COMPANION
@pytest.mark.parametrize("bucket", _BUCKETS)
def test_kat_vector(bucket: str) -> None:
    kernel = load_compiled_index_kernel()
    native = native_bucket_perturb(
        pa.array(_KAT_CORPUS, type=pa.string()),
        bucket=bucket,
        date_format="%Y-%m-%d",
        mask_key=_MASK_KEY,
        namespace="kat_ns",
        index_kernel=kernel,
    )
    assert native.to_pylist() == _KAT_EXPECTED[bucket]


# ── Seam proof: full-frame EXECUTES native bucket_perturb ────────────


@_NEEDS_COMPANION
def test_full_frame_executes_native_bucket_perturb(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["2024-02-15", "2023-11-30", None, "bad"], type=pa.string())})
    write_read_only_fixture(tmp_path, source, "bp")
    config = build_config(tmp_path, "t", tmp_path / "bp.parquet", [_bp_column()])
    run = run_shadow_and_oracle(config, "t", source, key_provider=_kp())
    (evidence,) = run.shadow.route_evidence.values()
    assert evidence.actual_operator == "native_bucket_perturb"
    assert evidence.executed is True
    # Positive index-kernel-call evidence, never inferred from success alone.
    assert evidence.compiled_kernel_executed is True


# ── Seam proof: chunked route DECLINES bucket_perturb to the oracle ──


def test_chunked_route_declines_bucket_perturb(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["2024-01-01", "2024-02-02", "2024-03-03"], type=pa.string())})
    write_read_only_fixture(tmp_path, source, "bp")
    config = build_config(tmp_path, "t", tmp_path / "bp.parquet", [_bp_column()])
    profile = first_chunk_profile(source, table="t", engine_version=ENGINE_VERSION)
    preflight = plan_native_route(
        config, profile, table="t", engine_version=ENGINE_VERSION, first_schema=source.schema
    )
    assert preflight.evidence.native_admitted is False
    assert "bucket_perturb_not_native_chunked_route:c" in (preflight.evidence.reroute_reason or "")


# ── Admission declines (positive assertions) ─────────────────────────


def test_no_date_format_declines_on_native_route(tmp_path: Path) -> None:
    """A bucket_perturb column WITHOUT date_format (autodetect) is an
    order-dependent parity hazard and declines to the oracle."""
    config = build_config(tmp_path, "t", tmp_path / "x.parquet", [_bp_column(date_format=None)])
    result = native_route_eligibility(config, table="t")
    assert not result.accepted
    assert any(r == "bucket_perturb_requires_date_format:c" for r in result.rejections), (
        result.rejections
    )


def test_empty_date_format_declines_on_native_route(tmp_path: Path) -> None:
    config = build_config(
        tmp_path, "t", tmp_path / "x.parquet", [_bp_column({"date_format": ""}, date_format=None)]
    )
    result = native_route_eligibility(config, table="t")
    assert not result.accepted
    assert any(r == "bucket_perturb_requires_date_format:c" for r in result.rejections), (
        result.rejections
    )


def test_missing_namespace_declines_on_native_route(tmp_path: Path) -> None:
    config = build_config(tmp_path, "t", tmp_path / "x.parquet", [_bp_column(namespace=None)])
    result = native_route_eligibility(config, table="t")
    assert not result.accepted
    assert any(r == "bucket_perturb_requires_namespace:c" for r in result.rejections), (
        result.rejections
    )


def test_unsupported_bucket_declines_on_native_route(tmp_path: Path) -> None:
    config = build_config(tmp_path, "t", tmp_path / "x.parquet", [_bp_column(bucket="fortnight")])
    result = native_route_eligibility(config, table="t")
    assert not result.accepted
    assert any(r == "bucket_perturb_unsupported_bucket:c" for r in result.rejections), (
        result.rejections
    )


def _compile_plan_for(config: dict, source: pa.Table):
    inputs = capture_physical_plan_inputs(config, {"t": source}, engine_version=ENGINE_VERSION)
    return compile_physical_plan(inputs)


def test_no_date_format_leaves_node_unbound_on_full_frame(tmp_path: Path) -> None:
    """Boundary 2: the compiled full-frame binding leaves a no-date_format node
    UNBOUND (`execution is None`), so the coordinator declines it to the oracle."""
    source = pa.table({"c": pa.array(["2024-01-01"], type=pa.string())})
    write_read_only_fixture(tmp_path, source, "x")
    config = build_config(tmp_path, "t", tmp_path / "x.parquet", [_bp_column(date_format=None)])
    plan = _compile_plan_for(config, source)
    nodes = [n for tbl in plan.tables for n in tbl.nodes if n.strategy == "bucket_perturb"]
    assert nodes, "expected a bucket_perturb node in the compiled plan"
    assert all(n.execution is None for n in nodes), "no-date_format bucket_perturb must NOT bind"


# ── Runtime fail-closed guards at dispatch ───────────────────────────


def _binding(**overrides: Any) -> ExecutionBinding:
    base: dict[str, Any] = dict(
        operator_id="native_bucket_perturb",
        operator_reason="test",
        resolved_config=(),
        input_schema=pa.schema([pa.field("c", pa.string())]),
        output_schema=pa.schema([pa.field("c", pa.string())]),
        determinism_family="source_keyed_hmac",
        determinism_version=1,
        key_binding=KeyBinding(key_source="mask_key", namespace="ns"),
        diagnostic_obligations=(),
        required_prepasses=(),
        batch_estimate=None,
        bucket_perturb_bucket="month",
        bucket_perturb_date_format="%Y-%m-%d",
    )
    base.update(overrides)
    return ExecutionBinding(**base)


def test_run_operator_asserts_key_binding_present() -> None:
    binding = _binding(key_binding=None)
    ctx = SimpleNamespace(mask_key=_MASK_KEY, native_threads=None)
    evidence = OperatorCallEvidence(planned_operator="native_bucket_perturb")
    with pytest.raises(AssertionError, match="no KeyBinding"):
        run_operator(
            pa.array(["2024-01-01"], type=pa.string()),
            binding=binding,
            ctx=ctx,  # type: ignore[arg-type]
            evidence=evidence,
            index_kernel=None,
        )


def test_run_operator_asserts_resolved_config_present() -> None:
    binding = _binding(bucket_perturb_date_format=None)
    ctx = SimpleNamespace(mask_key=_MASK_KEY, native_threads=None)
    evidence = OperatorCallEvidence(planned_operator="native_bucket_perturb")
    with pytest.raises(AssertionError, match="no resolved bucket/date_format"):
        run_operator(
            pa.array(["2024-01-01"], type=pa.string()),
            binding=binding,
            ctx=ctx,  # type: ignore[arg-type]
            evidence=evidence,
            index_kernel=object(),  # type: ignore[arg-type]
        )


# ── Unified-slice ExecutionResult boundary (flag-off vs flag-on) ─────


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


# Non-empty shapes: full value AND field-type parity at the final boundary. Empty
# is covered separately (its zero-row type label is a finalizer artifact).
_NONEMPTY_SHAPES = [(label, vals) for label, vals in _SHAPES if label != "empty"]


@pytest.mark.parametrize("bucket", _BUCKETS)
@pytest.mark.parametrize("label, values", _NONEMPTY_SHAPES, ids=[s[0] for s in _NONEMPTY_SHAPES])
def test_unified_slice_execution_result_byte_identical(
    tmp_path: Path, bucket: str, label: str, values: list
) -> None:
    source = pa.table({"c": pa.array(values, type=pa.string())})
    off, on = _run_both(tmp_path, source, [_bp_column(bucket=bucket)])
    ot, nt = off.outputs["t"], on.outputs["t"]
    assert ot.schema.field("c").type == nt.schema.field("c").type
    assert ot.column("c").to_pylist() == nt.column("c").to_pylist()
    assert ot.schema.equals(nt.schema, check_metadata=True)
    if native_companion_status().ok:
        # The lane actually ACTIVATED for bucket_perturb (not a decline that would
        # pass parity against itself); the node ran native bucket_perturb.
        assert QUALITY_METRICS_KEY in on.quality_metrics
        node_ev = on.quality_metrics[QUALITY_METRICS_KEY]["nodes"]
        assert node_ev, "expected D7 node evidence"
        for ev in node_ev.values():
            assert ev["operator"] == "native_bucket_perturb"
            assert ev["executed"] is True


def test_unified_slice_empty_value_parity_and_finalizer_type(tmp_path: Path) -> None:
    """An EMPTY column: VALUE parity holds (both zero-row). When the companion is
    present the lane activates and the unified-slice finalizer normalizes the
    zero-row masked column to `float64` (the tokenizing-family contract), while
    the legacy per-strategy oracle types the passed-through empty source `null`.
    Without the companion the lane declines and both sides are the legacy oracle,
    so the two agree. Either way no data is affected -- a zero-row type label."""
    source = pa.table({"c": pa.array([], type=pa.string())})
    off, on = _run_both(tmp_path, source, [_bp_column()])
    ot, nt = off.outputs["t"], on.outputs["t"]
    assert ot.column("c").to_pylist() == nt.column("c").to_pylist() == []
    if native_companion_status().ok:
        assert nt.schema.field("c").type == pa.float64()
    else:
        assert nt.schema.field("c").type == ot.schema.field("c").type


def test_unified_slice_parquet_round_trip(tmp_path: Path) -> None:
    source = pa.table(
        {"c": pa.array(["2024-02-29", "2023-02-28", None, "bad", "2024-12-31"], type=pa.string())}
    )
    off, on = _run_both(tmp_path, source, [_bp_column(bucket="quarter")])
    off_path, on_path = tmp_path / "off.parquet", tmp_path / "on.parquet"
    pq.write_table(off.outputs["t"], off_path)
    pq.write_table(on.outputs["t"], on_path)
    off_back, on_back = pq.read_table(off_path), pq.read_table(on_path)
    assert off_back.schema.equals(on_back.schema, check_metadata=True)
    assert off_back.column("c").to_pylist() == on_back.column("c").to_pylist()


def test_non_string_source_declines_to_oracle(tmp_path: Path) -> None:
    """A non-string bucket_perturb SOURCE is outside the native operator's
    admitted resident type, so both arms take the legacy route and agree (no
    activation)."""
    source = pa.table({"c": pa.array([20240101, 20231231, 20240229], type=pa.int64())})
    off, on = _run_both(tmp_path, source, [_bp_column()])
    ot, nt = off.outputs["t"], on.outputs["t"]
    assert ot.column("c").to_pylist() == nt.column("c").to_pylist()
    assert ot.schema.field("c").type == nt.schema.field("c").type
    assert QUALITY_METRICS_KEY not in on.quality_metrics
