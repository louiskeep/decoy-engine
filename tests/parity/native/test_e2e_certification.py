"""T6 end-to-end gate hardening: the one piece the fixed-formula Phase 2 gate
cannot show on its own.

`test_phase2_gate.py` already carries the batch-size/order space over an
all-{passthrough,redact,truncate,hash} admitted table, and (after this batch)
its mixed-route test asserts the full route-proof for the admitted/
non-admitted boundary (every node tagged oracle, zero native kernels ran,
output matches). This file does not re-explode either of those. It adds only
the genuinely missing piece: a property (not an example) over randomized
per-row null placement across every nullable admitted column at once, run
through the real `run_native_or_oracle_chunked` dispatch against the pandas
oracle. The gate already pins the two shapes at the ends of that space (a
whole-column all-null, and no nulls at all); this proves the space between
them -- an arbitrary subset of rows nulled, at chunk boundaries the property
varies -- never diverges. This is NOT T3's kernel-level null parity (a single
kernel vs its handler): it is the whole dispatch-vs-oracle pipeline.

It also carries the sampled-parity leg of the 100M-row certification
(`docs/plans/native-testing-T6-e2e.md`): the wall/RSS run at 100M rows is
measured by `scripts/native-baseline/bench_driver.py` (the harness proven
honest in Task 2.7: out-of-band Parquet generation, a lazy
`ParquetFile.iter_batches` read, one dropped output batch at a time), which
never holds a byte-identical oracle comparison at that scale in memory. Exact
logical parity at 100M rows is instead proven on a SAMPLE: this test rebuilds
the identical rows the 100M-row Parquet file would carry at three positions
(the head, and two positions deep in the range) straight from the frozen
per-row formula (`build_w2_parquet.build_batch`, seeded only by absolute row
index, so it reproduces those exact rows without touching a 100M-row file),
then runs that sample through both the real dispatch and the oracle.
"""

from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

import pyarrow as pa
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.parity.native.test_phase2_gate import (
    _assert_gate_parity,
    _build_config,
    _build_source,
    _run_native,
    _run_oracle,
)

_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None
_NEEDS_COMPANION = pytest.mark.skipif(
    not _COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed; the companion-present CI job covers this",
)

_NATIVE_BASELINE_DIR = Path(__file__).resolve().parents[3] / "scripts" / "native-baseline"
sys.path.insert(0, str(_NATIVE_BASELINE_DIR))
from build_w2_parquet import build_batch  # noqa: E402 -- path set up just above

# The frozen W2 workload's 10-column shape (PHASE2-BASELINE.md), matching
# `build_w2_parquet.build_batch` and `bench_worker_native.build_config`
# exactly -- the certification's actual wall/RSS workload, not the gate's own
# extended 11-column fixture (which adds a legacy truncate variant no bench
# tier carries).
_W2_10_COLUMNS = [
    {"name": "h_email", "strategy": "hash", "namespace": "ns_email"},
    {"name": "h_token", "strategy": "hash", "namespace": "ns_token"},
    {"name": "h_uid", "strategy": "hash", "namespace": "ns_uid"},
    {"name": "pt_amount", "strategy": "passthrough"},
    {"name": "pt_flag", "strategy": "passthrough"},
    {"name": "pt_ts", "strategy": "passthrough"},
    {"name": "rd_ssn", "strategy": "redact"},
    {"name": "rd_notes", "strategy": "redact"},
    {
        "name": "tr_phone",
        "strategy": "truncate",
        "provider_config": {"length": 3, "keep": "head"},
    },
    {"name": "tr_card", "strategy": "truncate", "provider_config": {"length": 4, "keep": "tail"}},
]

_100M = 100_000_000
_SAMPLE_ROWS = 2_000
_SAMPLE_RANGES = (
    (0, _SAMPLE_ROWS),  # the head of the file
    (_100M // 2, _100M // 2 + _SAMPLE_ROWS),  # a scattered slice at the midpoint
    (_100M - _SAMPLE_ROWS, _100M),  # the tail
)

# Every nullable admitted column the gate's fixed source carries a real value
# for on every row; `tr_legacy` and the passthrough/int columns are left
# value-bearing so the property isolates null PLACEMENT, not a fifth shape.
_NULLABLE_STRING_COLUMNS = ("h_email", "h_token", "rd_ssn", "rd_notes", "tr_phone", "tr_card")


def _build_source_with_scattered_nulls(n_rows: int, null_rows: frozenset[int]) -> pa.Table:
    """The gate's own row-shaped source, with an ARBITRARY subset of rows
    nulled on every nullable string column at once. A real streaming source
    can null any subset of rows on any admitted column; the gate's fixed
    formula never does (only a whole-column-null case, tested separately in
    `test_native_route_parity_on_all_null_column`)."""
    base = _build_source(n_rows)
    for name in _NULLABLE_STRING_COLUMNS:
        col = base.column(name).combine_chunks().to_pylist()
        nulled = [None if i in null_rows else v for i, v in enumerate(col)]
        field_index = base.schema.get_field_index(name)
        base = base.set_column(field_index, name, pa.array(nulled, type=pa.string()))
    return base


@_NEEDS_COMPANION
@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    n_rows=st.integers(min_value=1, max_value=23),
    null_seed=st.integers(min_value=0, max_value=2**32 - 1),
    batch_size=st.sampled_from((1, 5, 11)),
)
def test_native_route_matches_oracle_with_scattered_nulls(
    n_rows: int, null_seed: int, batch_size: int
) -> None:
    rng = random.Random(null_seed)
    null_rows = frozenset(i for i in range(n_rows) if rng.random() < 0.3)

    source = _build_source_with_scattered_nulls(n_rows, null_rows)
    config = _build_config(source, key=f"e2e_scattered_null_{n_rows}_{null_seed}_{batch_size}")
    oracle = _run_oracle(config, source)
    native_table, evidence = _run_native(config, source, batch_size)

    assert evidence.native_admitted is True
    _assert_gate_parity(native_table, oracle)


@_NEEDS_COMPANION
def test_100m_certified_workload_sampled_slice_matches_oracle() -> None:
    """The sampled-parity leg of the 100M-row certification (criterion a).

    6,000 rows total, reconstructed from the exact 100M-row Parquet formula
    at the head, the midpoint, and the tail of the range -- not a fresh
    smaller table with its own row count, which would prove parity for a
    DIFFERENT workload than the one the wall/RSS run actually certifies.
    """
    sample = pa.concat_tables(
        [build_batch(start, end) for start, end in _SAMPLE_RANGES]
    ).combine_chunks()
    config = _build_config(sample, key="100m_sampled_slice", columns=_W2_10_COLUMNS)
    oracle = _run_oracle(config, sample)
    native_table, evidence = _run_native(config, sample, batch_size=1_000)

    assert evidence.native_admitted is True
    assert all(r.route == "native_kernel" for r in evidence.node_routes)
    assert evidence.compiled_kernel_executed is True
    assert evidence.kernel_calls.get("hash", 0) > 0
    _assert_gate_parity(native_table, oracle)
