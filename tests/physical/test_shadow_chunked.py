"""Task 4.6 slice 2: single-table CHUNKED masking parity in the SHADOW test
path (design doc's Task 4.6 slice-2 plan). Extends the Task 4.4/4.6-slice-1
corpus (`test_shadow_corpus.py`) with the one disposition it never exercised:
`run_shadow_and_oracle` compiling and running a job the compiler stamps
`DriverId.CHUNKED`, compared against the pinned pandas oracle's OWN chunked
route (`run_pipeline(auto_chunk=True, ...)`) over identical chunk boundaries.

SHADOW-ONLY, like every file in this package: `run_shadow_and_oracle` runs
the compiler + `ShadowCoordinator` directly and never touches
`cheap_admission` / `resident_contract_admission`, so this file does not
exercise (or change) the production unified-slice admission lane. It is a
strict superset test: the shadow corpus's five strategies (passthrough,
redact, truncate, hash, faker) stay a superset of the production
admission's four -- this file adds no sixth strategy, just a new
disposition (chunked vs. full_frame) for the strategies already admitted.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.execution.native._companion_status import native_companion_status
from decoy_engine.execution.physical._types import DriverId
from decoy_engine.keyprovider import SecretKeyProvider
from tests.physical._shadow_helpers import (
    ShadowRun,
    assert_every_node_bound,
    assert_route_evidence_matches_plan,
    assert_shadow_matches_oracle,
    build_config,
    run_shadow_and_oracle,
    write_read_only_fixture,
)

_MASK_KEY = bytes(range(32))

# Lowered from production defaults (100_000 rows / 50_000-row chunks) so a
# few-hundred-row fixture genuinely crosses several chunk boundaries instead
# of needing a multi-hundred-thousand-row corpus fixture.
_CHUNK_THRESHOLD_ROWS = 100
_CHUNK_SIZE_ROWS = 50
# Not a multiple of _CHUNK_SIZE_ROWS: the final chunk is deliberately ragged
# (30 rows, not 50), so the parity proof covers a partial final chunk, not
# only evenly-sized ones.
_N_ROWS = 230
_EXPECTED_CHUNK_COUNT = 5
_EXPECTED_LAST_CHUNK_SIZE = 30


def _key_provider() -> SecretKeyProvider:
    return SecretKeyProvider(secret=_MASK_KEY, key_version="v1")


_UNKEYED_COLUMNS = [
    {"name": "pt_amount", "strategy": "passthrough"},
    {"name": "pt_note", "strategy": "passthrough"},
    {"name": "rd_ssn", "strategy": "redact"},
    {"name": "tr_phone", "strategy": "truncate", "provider_config": {"length": 3, "keep": "head"}},
]

_KEYED_EXTRA_COLUMNS = [
    {"name": "h_email", "strategy": "hash", "namespace": "ns_chunk_email"},
    {
        "name": "fk_first",
        "strategy": "faker",
        "provider": "person_first_name",
        "deterministic": True,
        "namespace": "ns_chunk_faker",
        "pool_size": 30,
    },
]


def _build_multi_chunk_source(n_rows: int, *, keyed: bool = False) -> pa.Table:
    idx = list(range(n_rows))
    _str = pa.string()
    columns: dict[str, pa.Array] = {
        "pt_amount": pa.array([(i * 7) % 1_000_000 for i in idx], type=pa.int64()),
        # Null density on a STRING column is chunk-stable (only int+null
        # widens per-frame under pandas); this is the passthrough all-null-
        # density case at CHUNKED scale, distinct from the dedicated
        # wholly-all-null degenerate test below.
        "pt_note": pa.array([None if i % 9 == 0 else f"note-{i}" for i in idx], type=_str),
        "rd_ssn": pa.array([f"5{i % 900:03d}-11-2222" for i in idx], type=_str),
        "tr_phone": pa.array([f"512{i % 9000:04d}" for i in idx], type=_str),
    }
    if keyed:
        columns["h_email"] = pa.array([f"user{i}@example.com" for i in idx], type=_str)
        columns["fk_first"] = pa.array(
            [None if i % 11 == 0 else f"first_src_{i % 6}" for i in idx], type=_str
        )
    return pa.table(columns)


def _run_chunked(tmp_path: Path, name: str, source: pa.Table, columns: list[dict]) -> ShadowRun:
    path = write_read_only_fixture(tmp_path, source, name)
    config = build_config(tmp_path, "t", path, columns)
    return run_shadow_and_oracle(
        config,
        "t",
        source,
        key_provider=_key_provider(),
        auto_chunk=True,
        auto_chunk_threshold_rows=_CHUNK_THRESHOLD_ROWS,
        chunk_size_rows=_CHUNK_SIZE_ROWS,
    )


# ---------------------------------------------------------------------------
# The core proof: unkeyed strategies, ALWAYS-RUN (no companion guard). This
# is what proves the CHUNKED disposition parity holds at all; it must not be
# skippable, or a companion-absent CI run would never exercise this slice.
# ---------------------------------------------------------------------------


def test_unkeyed_multi_chunk_parity_always_run(tmp_path: Path) -> None:
    assert _N_ROWS % _CHUNK_SIZE_ROWS != 0  # the fixture's whole point: a ragged final chunk

    run = _run_chunked(
        tmp_path, "unkeyed_multi_chunk", _build_multi_chunk_source(_N_ROWS), _UNKEYED_COLUMNS
    )
    assert_every_node_bound(run.plan)
    assert_shadow_matches_oracle(run)
    assert_route_evidence_matches_plan(run)

    for table in run.plan.tables:
        assert table.driver == DriverId.CHUNKED

    auto_chunk_block = run.oracle.quality_metrics["auto_chunk"]
    assert auto_chunk_block["mode"] == "chunked"
    assert auto_chunk_block["chunk_count"] == _EXPECTED_CHUNK_COUNT
    last_chunk_size = _N_ROWS - (_EXPECTED_CHUNK_COUNT - 1) * _CHUNK_SIZE_ROWS
    assert last_chunk_size == _EXPECTED_LAST_CHUNK_SIZE


# ---------------------------------------------------------------------------
# Keyed strategies (hash + faker) over the same multi-chunk shape: these
# route through the compiled kernel, so they skip companion-absent.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not native_companion_status().ok,
    reason="compiled decoy-engine-native companion unavailable",
)
def test_keyed_multi_chunk_parity(tmp_path: Path) -> None:
    run = _run_chunked(
        tmp_path,
        "keyed_multi_chunk",
        _build_multi_chunk_source(_N_ROWS, keyed=True),
        _UNKEYED_COLUMNS + _KEYED_EXTRA_COLUMNS,
    )
    assert_every_node_bound(run.plan)
    assert_shadow_matches_oracle(run)
    assert_route_evidence_matches_plan(run)

    for table in run.plan.tables:
        assert table.driver == DriverId.CHUNKED
    assert run.oracle.quality_metrics["auto_chunk"]["mode"] == "chunked"


# ---------------------------------------------------------------------------
# Degenerate shape: a non-empty all-null column at CHUNKED scale. Proves the
# shadow's all-null reconciliation (`_assemble_column`) tracks the oracle's
# own pandas round-trip under a REAL multi-chunk run, not only the small
# scale the pre-existing FULL_FRAME degenerate tests cover. An empty-table
# CHUNKED case is impossible by construction: the threshold is positive, so
# a zero-row source never gets the auto-CHUNKED disposition.
# ---------------------------------------------------------------------------


def test_chunked_degenerate_all_null_column_parity(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array([None] * _N_ROWS, type=pa.string())})
    columns = [{"name": "c", "strategy": "passthrough"}]

    run = _run_chunked(tmp_path, "chunked_allnull", source, columns)
    assert_every_node_bound(run.plan)
    assert_shadow_matches_oracle(run)
    assert_route_evidence_matches_plan(run)

    for table in run.plan.tables:
        assert table.driver == DriverId.CHUNKED
    assert run.oracle.quality_metrics["auto_chunk"]["mode"] == "chunked"


# ---------------------------------------------------------------------------
# Chunk-boundary dtype instability: this is a DRIVER-SELECTION decline, not
# a node-binding or unified-slice-admission decline. An int64 column
# carrying even one null is chunk-unstable (pandas widens int+null to
# float64 PER FRAME, so which chunk a null lands in changes that chunk's
# dtype); the compiler must fall back to FULL_FRAME and record why, and the
# coordinator must never be run as a CHUNKED plan for this table.
# ---------------------------------------------------------------------------


def test_chunk_boundary_dtype_instability_declines_chunked_driver(tmp_path: Path) -> None:
    idx = list(range(_N_ROWS))
    amounts: list[int | None] = list(idx)
    amounts[7] = None  # one null is enough to trip the int+null widening gate
    source = pa.table(
        {
            "tr_phone": pa.array([f"512{i % 9000:04d}" for i in idx], type=pa.string()),
            "pt_amount": pa.array(amounts, type=pa.int64()),
        }
    )
    columns = [
        {
            "name": "tr_phone",
            "strategy": "truncate",
            "provider_config": {"length": 3, "keep": "head"},
        },
        {"name": "pt_amount", "strategy": "passthrough"},
    ]

    run = _run_chunked(tmp_path, "dtype_unstable", source, columns)
    assert_every_node_bound(run.plan)
    assert_shadow_matches_oracle(run)
    assert_route_evidence_matches_plan(run)

    for table in run.plan.tables:
        assert table.driver == DriverId.FULL_FRAME
        chunked_alternative = next(
            alt for alt in table.rejected_alternatives if alt.driver == DriverId.CHUNKED
        )
        assert "chunked_source_dtype_unstable" in chunked_alternative.reason

    assert run.oracle.quality_metrics["auto_chunk"]["mode"] == "full_frame"


# ---------------------------------------------------------------------------
# FULL_FRAME no-regression: a source below the (lowered) threshold stays
# FULL_FRAME even with auto_chunk=True and the same chunk_size_rows wired
# through -- the CHUNKED additions never reclassify a full_frame-sized job.
# ---------------------------------------------------------------------------


def test_full_frame_no_regression_below_threshold(tmp_path: Path) -> None:
    small_n = _CHUNK_THRESHOLD_ROWS - 1
    run = _run_chunked(
        tmp_path, "below_threshold", _build_multi_chunk_source(small_n), _UNKEYED_COLUMNS
    )
    assert_every_node_bound(run.plan)
    assert_shadow_matches_oracle(run)
    assert_route_evidence_matches_plan(run)

    for table in run.plan.tables:
        assert table.driver == DriverId.FULL_FRAME
    assert run.oracle.quality_metrics["auto_chunk"]["mode"] == "full_frame"
