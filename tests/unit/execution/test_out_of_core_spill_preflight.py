"""OOC-D: disk-aware out-of-core routing (conservative estimate, ADVISORY gate).

The estimator is a conservative UPPER bound on the temp-disk a job needs, in
two contributions (see `out_of_core/_spill_estimate.py`):

  SPILL  = SPILL_OVERHEAD x (base + parent_relations x max_per_table_edge_count)
           (transient temp: FK KEY tokens only; the edge multiplier applies
            ONLY to the small parent-relation term, never each table's
            one-time staged keys -- the k^2 star-blowup fix)
  OUTPUT = sum_tables(masked_full_row_width x rows)   (committed on-disk output)
  disk_needed = (SPILL + OUTPUT) x DISK_SAFETY_MARGIN

Widths are MASKED widths (a hash key stages its 64-hex digest, not the source
int) from the real profile + config, NOT a fixture-fit compression factor. The
preflight is ADVISORY: it WARNS on a tight estimate and lets the job proceed;
the runtime `check_temp_disk_budget` (threaded into the runner, fired at every
table boundary) is the real enforcer. Tests assert the prediction is `>=` a
realistic lower bound of the actual footprint (conservative), that the
preflight warns rather than rejects, and that the runtime cap enforces.

Coverage:
1. Benchmark-shape SANITY -- the prediction exceeds the measured 50M/100M
   footprint (not a tight band; conservative).
2. A wide (500B) high-cardinality string MASK column is priced at its real
   width; the advisory WARNS (does not raise) on an insufficient disk.
3. A narrow multi-FK star/fact table: the fact's key staging is linear in
   fan-in (only the small parent relations carry the multiplier), no k^2.
4. A zero-payload link table prices without crashing (keys only).
5. The output-filesystem gate: OUTPUT is dropped only when it provably lands
   on a different filesystem than the temp root.
5b/5c. Masked hash-key widths, and per-table (not O(N^2)) chain scaling.
6. Routing-level wiring end to end through `run_pipeline`: the advisory warns
   and the RUNTIME cap (out_of_core_temp_disk_exceeded) enforces; a
   sufficient-disk job proceeds AND threads a non-None temp_disk_budget into
   the runner; an OOC-INCOMPATIBLE (cyclic FK) job is untouched by the gate.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import fk_join_key
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution.out_of_core import _budget as budget_mod
from decoy_engine.execution.out_of_core import _spill_estimate as spill_mod
from decoy_engine.execution.out_of_core._spill_estimate import (
    DISK_SAFETY_MARGIN,
    HASH_DIGEST_HEX_BYTES,
    SPILL_OVERHEAD,
    UNKNOWN_WIDTH_CEILING_BYTES,
    default_ooc_temp_root,
    max_per_table_edge_count,
    predict_ooc_disk_bytes,
)
from decoy_engine.profile._types import ColumnProfile, Profile, Relationship, TableProfile
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

_N = 20
_GIB = 1024 * 1024 * 1024


# ---------------------------------------------------------------------------
# Profile / graph builders (real objects -- exercise the real width adapter).
# ---------------------------------------------------------------------------


def _col(
    name: str,
    dtype: str,
    *,
    rows: int,
    max_length: int | None = None,
    is_fk: bool = False,
    fk_target: tuple[str, str] | None = None,
) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype=dtype,
        row_count=rows,
        null_count=0,
        distinct_count=None,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=is_fk,
        fk_target=fk_target,
        pii_class=None,
        max_length=max_length,
    )


def _profile(
    tables: tuple[TableProfile, ...], relationships: tuple[Relationship, ...] = ()
) -> Profile:
    return Profile(
        schema_version=1,
        tables=tables,
        relationships=relationships,
        profiled_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        decoy_engine_version="0.1.0",
    )


def _edge(parent_table: str, parent_col: str, child_table: str, child_col: str) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table=parent_table,
        parent_columns=(parent_col,),
        child_table=child_table,
        child_columns=(child_col,),
        namespace=f"ns_{parent_table}_{child_table}",
        orphan_policy=OrphanPolicy.PRESERVE,
    )


def _all_mask(*names: str) -> dict[str, str]:
    return {name: "mask" for name in names}


# ---------------------------------------------------------------------------
# 1. Benchmark-shape sanity: prediction >= measured footprint (conservative).
# ---------------------------------------------------------------------------


def _benchmark_profile_and_graph(rows_per_table: int) -> tuple[Profile, RelationshipGraph]:
    """The measured benchmark schema: parent->child->grandchild, 16 payload
    string columns/table (max_length 12), short string keys (max_length 9)."""

    def payload() -> list[ColumnProfile]:
        return [
            _col(f"payload_{i:02d}", "object", rows=rows_per_table, max_length=12)
            for i in range(16)
        ]

    parent = TableProfile(
        name="parent",
        row_count=rows_per_table,
        columns=(_col("id", "object", rows=rows_per_table, max_length=9), *payload()),
    )
    child = TableProfile(
        name="child",
        row_count=rows_per_table,
        columns=(
            _col("id", "object", rows=rows_per_table, max_length=9),
            _col(
                "parent_id",
                "object",
                rows=rows_per_table,
                max_length=9,
                is_fk=True,
                fk_target=("parent", "id"),
            ),
            *payload(),
        ),
    )
    grandchild = TableProfile(
        name="grandchild",
        row_count=rows_per_table,
        columns=(
            _col(
                "child_id",
                "object",
                rows=rows_per_table,
                max_length=9,
                is_fk=True,
                fk_target=("child", "id"),
            ),
            *payload(),
        ),
    )
    graph = RelationshipGraph(
        edges=(
            _edge("parent", "id", "child", "parent_id"),
            _edge("child", "id", "grandchild", "child_id"),
        ),
        ordering=(),
    )
    return _profile((parent, child, grandchild)), graph


def _benchmark_config() -> dict[str, Any]:
    """The measured benchmark hash-masked its FK keys, so the real 64-hex
    digest tokens are what spilled/committed -- the config must say so for the
    masked-width model to reflect the measured footprint."""

    def hash_col(name: str, ns: str) -> dict[str, Any]:
        return {"name": name, "strategy": "hash", "namespace": ns}

    return {
        "tables": [
            {"name": "parent", "columns": [hash_col("id", "n1")]},
            {"name": "child", "columns": [hash_col("id", "n2"), hash_col("parent_id", "n1")]},
            {"name": "grandchild", "columns": [hash_col("child_id", "n2")]},
        ]
    }


class TestBenchmarkShapeSanity:
    @pytest.mark.parametrize(
        "total_rows,measured_spill_gib,measured_output_gib",
        [
            (50_000_000, 5.55, 5.92),
            (100_000_000, 11.1, 11.8),
        ],
    )
    def test_prediction_exceeds_measured_footprint(
        self, total_rows: int, measured_spill_gib: float, measured_output_gib: float
    ) -> None:
        profile, graph = _benchmark_profile_and_graph(total_rows // 3)
        pred = predict_ooc_disk_bytes(
            profile,
            graph=graph,
            table_kinds=_all_mask("parent", "child", "grandchild"),
            config=_benchmark_config(),
            include_output=True,
        )
        # Conservative direction: >= the real measured footprint, not a band.
        assert pred.spill_bytes >= measured_spill_gib * _GIB
        assert pred.output_bytes >= measured_output_gib * _GIB
        assert pred.total_bytes >= (measured_spill_gib + measured_output_gib) * _GIB


# ---------------------------------------------------------------------------
# 2. Wide high-cardinality string mask column -> priced real, rejects.
# ---------------------------------------------------------------------------


class TestWideStringColumnIsPricedAtRealWidth:
    def _wide_profile_graph(self, rows: int, notes_width: int) -> tuple[Profile, RelationshipGraph]:
        parent = TableProfile(
            name="parent",
            row_count=rows,
            columns=(
                _col("id", "object", rows=rows, max_length=8),
                _col("notes", "object", rows=rows, max_length=notes_width),
            ),
        )
        child = TableProfile(
            name="child",
            row_count=rows,
            columns=(
                _col("id", "object", rows=rows, max_length=8),
                _col(
                    "parent_id",
                    "object",
                    rows=rows,
                    max_length=8,
                    is_fk=True,
                    fk_target=("parent", "id"),
                ),
            ),
        )
        graph = RelationshipGraph(edges=(_edge("parent", "id", "child", "parent_id"),), ordering=())
        return _profile((parent, child)), graph

    def test_output_term_counts_the_wide_column_at_its_real_width(self) -> None:
        rows = 10_000_000
        profile, graph = self._wide_profile_graph(rows, notes_width=500)
        pred = predict_ooc_disk_bytes(
            profile,
            graph=graph,
            table_kinds=_all_mask("parent", "child"),
            config={},  # passthrough -> source (max_length) widths
            include_output=True,
        )
        # The 500-byte column dominates the output term -- not a 12-byte fallback.
        assert pred.output_bytes >= 500 * rows
        # And it is materially larger than the blocked 12-byte-fallback model
        # would have produced (which was ~ (8+12)*rows*2 tables ~ 0.4 GB).
        assert pred.total_bytes > 5 * _GIB

    def test_preflight_warns_but_does_not_raise_when_footprint_exceeds_free_disk(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        rows = 10_000_000
        profile, graph = self._wide_profile_graph(rows, notes_width=500)
        # Insufficient free disk (3 GiB) vs a ~7 GiB estimate: the advisory
        # WARNS but never raises -- the runtime cap is the enforcer.
        _patch_free_disk(monkeypatch, 3 * _GIB)
        with caplog.at_level(logging.WARNING, logger="decoy_engine.execution.out_of_core"):
            result = spill_mod.enforce_ooc_disk_preflight(
                profile,
                graph=graph,
                table_kinds=_all_mask("parent", "child"),
                config={},  # no file target -> output included (conservative)
            )
        assert result is not None and result.ok is False  # advisory result surfaced
        assert any("out-of-core disk advisory" in r.message for r in caplog.records)

    def test_preflight_is_silent_when_disk_is_generous(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        rows = 10_000_000
        profile, graph = self._wide_profile_graph(rows, notes_width=500)
        _patch_free_disk(monkeypatch, 500 * _GIB)
        with caplog.at_level(logging.WARNING, logger="decoy_engine.execution.out_of_core"):
            result = spill_mod.enforce_ooc_disk_preflight(
                profile, graph=graph, table_kinds=_all_mask("parent", "child"), config={}
            )
        assert result is not None and result.ok is True
        assert not any("disk advisory" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 3. Narrow multi-FK star/fact table -> spill x edge multiplicity.
# ---------------------------------------------------------------------------


class TestStarFactEdgeMultiplicity:
    def _star_profile_graph(
        self, rows: int, n_dims: int, dim_rows: int | None = None
    ) -> tuple[Profile, RelationshipGraph]:
        drows = rows if dim_rows is None else dim_rows
        dims = tuple(
            TableProfile(
                name=f"dim{i}",
                row_count=drows,
                columns=(_col("id", "int64", rows=drows),),
            )
            for i in range(n_dims)
        )
        fact = TableProfile(
            name="fact",
            row_count=rows,
            columns=(
                *(
                    _col(f"d{i}", "int64", rows=rows, is_fk=True, fk_target=(f"dim{i}", "id"))
                    for i in range(n_dims)
                ),
                _col("amount", "int64", rows=rows),
            ),
        )
        edges = tuple(_edge(f"dim{i}", "id", "fact", f"d{i}") for i in range(n_dims))
        graph = RelationshipGraph(edges=edges, ordering=())
        return _profile((*dims, fact)), graph

    def test_max_per_table_edge_count_is_the_fact_fan_in(self) -> None:
        _, graph = self._star_profile_graph(rows=1000, n_dims=3)
        # fact is the child of 3 edges (incoming 3), each dim a parent of 1.
        assert max_per_table_edge_count(graph) == 3

    def test_spill_scales_with_the_edge_multiplier(self) -> None:
        rows = 5_000_000
        profile3, graph3 = self._star_profile_graph(rows, n_dims=3)
        table_kinds3 = _all_mask("fact", "dim0", "dim1", "dim2")
        pred3 = predict_ooc_disk_bytes(
            profile3,
            graph=graph3,
            table_kinds=table_kinds3,
            config={},  # passthrough int64 keys -> source width 8
            include_output=False,
        )
        # Edge-mult applies ONLY to the parent-side relations, not the fact's
        # one-time staged keys (the k^2-blowup fix). base: fact stages its 3
        # child-side FK keys + row_nr once; each dim stages just its row_nr.
        # parent_relations: each dim's id relation, scaled by edge fan-in (3).
        tok = spill_mod._staged_key_token_bytes
        rownr = float(spill_mod.ROW_NR_BYTES)
        base = (rownr + 3 * tok(8.0, "int64")) * rows + 3 * (rownr * rows)
        parent_relations = 3 * (tok(8.0, "int64") * rows)
        expected_spill = int((base + parent_relations * 3) * SPILL_OVERHEAD)
        assert pred3.spill_bytes == expected_spill

    def test_fact_key_staging_is_linear_in_fan_in_not_quadratic(self) -> None:
        # Realistic star: a large fact + small dims. The old flat scalar
        # multiplied the fact's k staged FK keys by k (the ~50x k^2 blowup).
        # Now the fact's large staging is 1x (linear in its k key columns) and
        # only the tiny dim relations carry the multiplier -- so doubling the
        # dimension count ~doubles spill (dominated by the fact), never ~4x.
        fact_rows, dim_rows = 10_000_000, 1_000
        p5, g5 = self._star_profile_graph(fact_rows, n_dims=5, dim_rows=dim_rows)
        p10, g10 = self._star_profile_graph(fact_rows, n_dims=10, dim_rows=dim_rows)
        s5 = predict_ooc_disk_bytes(
            p5,
            graph=g5,
            table_kinds=_all_mask("fact", *(f"dim{i}" for i in range(5))),
            config={},
            include_output=False,
        )
        s10 = predict_ooc_disk_bytes(
            p10,
            graph=g10,
            table_kinds=_all_mask("fact", *(f"dim{i}" for i in range(10))),
            config={},
            include_output=False,
        )
        ratio = s10.spill_bytes / s5.spill_bytes
        assert 1.5 <= ratio <= 2.5  # linear in fan-in, not the old ~4x (k^2)

    def test_single_edge_has_no_multiplier(self) -> None:
        _, graph1 = self._star_profile_graph(rows=1000, n_dims=1)
        assert max_per_table_edge_count(graph1) == 1


# ---------------------------------------------------------------------------
# 4. Zero-payload link table -> keys only, no crash.
# ---------------------------------------------------------------------------


class TestZeroPayloadLinkTable:
    def test_link_table_prices_keys_only(self) -> None:
        rows = 1_000_000
        left = TableProfile(name="left", row_count=rows, columns=(_col("id", "int64", rows=rows),))
        right = TableProfile(
            name="right", row_count=rows, columns=(_col("id", "int64", rows=rows),)
        )
        link = TableProfile(
            name="link",
            row_count=rows,
            columns=(
                _col("left_id", "int64", rows=rows, is_fk=True, fk_target=("left", "id")),
                _col("right_id", "int64", rows=rows, is_fk=True, fk_target=("right", "id")),
            ),
        )
        graph = RelationshipGraph(
            edges=(
                _edge("left", "id", "link", "left_id"),
                _edge("right", "id", "link", "right_id"),
            ),
            ordering=(),
        )
        pred = predict_ooc_disk_bytes(
            _profile((left, right, link)),
            graph=graph,
            table_kinds=_all_mask("left", "right", "link"),
            config={},
            include_output=True,
        )
        # Output is just the fixed-width int64 columns (8 bytes each), no payload.
        # left.id + right.id + link.left_id + link.right_id, each x its rows.
        expected_output = int((8 * rows) + (8 * rows) + ((8 + 8) * rows))
        assert pred.output_bytes == expected_output
        assert pred.spill_bytes > 0


# ---------------------------------------------------------------------------
# 5. Output-filesystem gate.
# ---------------------------------------------------------------------------


class TestOutputFilesystemGate:
    def test_include_output_false_drops_the_output_term(self) -> None:
        profile, graph = _benchmark_profile_and_graph(1000)
        kinds = _all_mask("parent", "child", "grandchild")
        with_out = predict_ooc_disk_bytes(
            profile, graph=graph, table_kinds=kinds, config={}, include_output=True
        )
        without_out = predict_ooc_disk_bytes(
            profile, graph=graph, table_kinds=kinds, config={}, include_output=False
        )
        assert with_out.output_bytes > 0
        assert without_out.output_bytes == 0
        assert without_out.output_included is False
        assert with_out.spill_bytes == without_out.spill_bytes  # spill unchanged

    def test_no_file_target_conservatively_includes_output(self, tmp_path: Path) -> None:
        assert spill_mod._output_shares_temp_filesystem({}, tmp_path) is True

    def test_same_device_includes_output(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            spill_mod.os, "stat", lambda p: SimpleNamespace(st_dev=42), raising=True
        )
        config = {"targets": {"t": {"type": "file", "path": str(tmp_path / "out.parquet")}}}
        assert spill_mod._output_shares_temp_filesystem(config, tmp_path) is True

    def test_provably_different_device_omits_output(self, tmp_path: Path, monkeypatch) -> None:
        target_dir = tmp_path / "othervol"
        target_dir.mkdir()

        def fake_stat(path: object) -> SimpleNamespace:
            # temp root on device 1, the target dir on device 2.
            return SimpleNamespace(st_dev=2 if str(target_dir) in str(path) else 1)

        monkeypatch.setattr(spill_mod.os, "stat", fake_stat, raising=True)
        config = {"targets": {"t": {"type": "file", "path": str(target_dir / "out.parquet")}}}
        assert spill_mod._output_shares_temp_filesystem(config, tmp_path) is False


class TestConstantsArePinned:
    def test_conservative_constants(self) -> None:
        assert pytest.approx(2.0) == SPILL_OVERHEAD
        assert UNKNOWN_WIDTH_CEILING_BYTES == 256
        assert pytest.approx(1.25) == DISK_SAFETY_MARGIN

    def test_unknown_width_column_prices_at_the_ceiling_not_a_low_default(self) -> None:
        rows = 1000
        # An object column with NO max_length (e.g. all-null sample) is unknown.
        parent = TableProfile(
            name="parent", row_count=rows, columns=(_col("id", "int64", rows=rows),)
        )
        child = TableProfile(
            name="child",
            row_count=rows,
            columns=(
                _col("parent_id", "int64", rows=rows, is_fk=True, fk_target=("parent", "id")),
                _col("mystery", "object", rows=rows, max_length=None),
            ),
        )
        graph = RelationshipGraph(edges=(_edge("parent", "id", "child", "parent_id"),), ordering=())
        pred = predict_ooc_disk_bytes(
            _profile((parent, child)),
            graph=graph,
            table_kinds=_all_mask("parent", "child"),
            config={},
            include_output=True,
        )
        # The unknown 'mystery' column is priced at the 256-byte ceiling, so the
        # child's output term includes 256 * rows for it (plus the 8-byte FK).
        assert pred.output_bytes >= UNKNOWN_WIDTH_CEILING_BYTES * rows


# ---------------------------------------------------------------------------
# 5b. Masked-side widths: a hash-masked key stages its 64-hex digest, not the
#     source int -- the exact under-predict Fable found.
# ---------------------------------------------------------------------------


class TestMaskedHashKeyWidth:
    def _int_fk_profile_graph(self, rows: int) -> tuple[Profile, RelationshipGraph]:
        parent = TableProfile(
            name="parent", row_count=rows, columns=(_col("id", "int64", rows=rows),)
        )
        child = TableProfile(
            name="child",
            row_count=rows,
            columns=(
                _col("id", "int64", rows=rows),
                _col("parent_id", "int64", rows=rows, is_fk=True, fk_target=("parent", "id")),
            ),
        )
        graph = RelationshipGraph(edges=(_edge("parent", "id", "child", "parent_id"),), ordering=())
        return _profile((parent, child)), graph

    def _hash_keys_config(self) -> dict[str, Any]:
        return {
            "tables": [
                {
                    "name": "parent",
                    "columns": [{"name": "id", "strategy": "hash", "namespace": "n"}],
                },
                {
                    "name": "child",
                    "columns": [{"name": "parent_id", "strategy": "hash", "namespace": "n"}],
                },
            ]
        }

    def test_hash_masked_key_priced_at_digest_width_not_source_int(self) -> None:
        rows = 50_000_000
        profile, graph = self._int_fk_profile_graph(rows)
        kinds = _all_mask("parent", "child")
        masked = predict_ooc_disk_bytes(
            profile,
            graph=graph,
            table_kinds=kinds,
            config=self._hash_keys_config(),
            include_output=True,
        )
        source = predict_ooc_disk_bytes(
            profile, graph=graph, table_kinds=kinds, config={}, include_output=True
        )
        # The FK key hashes to a 64-char digest, so the masked estimate is
        # materially larger than the source-int (8-byte) estimate.
        assert masked.total_bytes > source.total_bytes
        assert masked.output_bytes >= HASH_DIGEST_HEX_BYTES * rows  # parent_id digest

    def test_warns_on_disk_sized_to_the_source_width_estimate(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        rows = 50_000_000
        profile, graph = self._int_fk_profile_graph(rows)
        kinds = _all_mask("parent", "child")
        cfg = self._hash_keys_config()
        masked = predict_ooc_disk_bytes(
            profile, graph=graph, table_kinds=kinds, config=cfg, include_output=True
        )
        source = predict_ooc_disk_bytes(
            profile, graph=graph, table_kinds=kinds, config={}, include_output=True
        )
        # A disk sized between the two: the source-width (pre-fix) model would
        # have judged it fine, but the masked-width estimate exceeds it -- so
        # the advisory WARNS (it does not raise; the runtime cap enforces).
        disk = (source.total_bytes + masked.total_bytes) // 2
        assert source.total_bytes < disk < masked.total_bytes
        _patch_free_disk(monkeypatch, disk)
        with caplog.at_level(logging.WARNING, logger="decoy_engine.execution.out_of_core"):
            result = spill_mod.enforce_ooc_disk_preflight(
                profile, graph=graph, table_kinds=kinds, config=cfg
            )
        assert result is not None and result.ok is False
        assert any("out-of-core disk advisory" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 5c. Per-table row scaling: a wide many-table chain must not blow up O(N^2).
# ---------------------------------------------------------------------------


class TestManyTableChainNoQuadraticBlowup:
    def _chain(self, n_tables: int, rows: int) -> tuple[Profile, RelationshipGraph]:
        tables: list[TableProfile] = []
        edges: list[RelationshipEdge] = []
        for i in range(n_tables):
            cols = [_col("id", "int64", rows=rows)]
            if i > 0:
                cols.append(
                    _col("prev_id", "int64", rows=rows, is_fk=True, fk_target=(f"t{i - 1}", "id"))
                )
            tables.append(TableProfile(name=f"t{i}", row_count=rows, columns=tuple(cols)))
            if i > 0:
                edges.append(_edge(f"t{i - 1}", "id", f"t{i}", "prev_id"))
        return _profile(tuple(tables)), RelationshipGraph(edges=tuple(edges), ordering=())

    def _kinds(self, n: int) -> dict[str, str]:
        return _all_mask(*(f"t{i}" for i in range(n)))

    def test_spill_scales_linearly_in_table_count_not_quadratically(self) -> None:
        rows = 100_000
        p4, g4 = self._chain(4, rows)
        p8, g8 = self._chain(8, rows)
        s4 = predict_ooc_disk_bytes(
            p4, graph=g4, table_kinds=self._kinds(4), config={}, include_output=False
        )
        s8 = predict_ooc_disk_bytes(
            p8, graph=g8, table_kinds=self._kinds(8), config={}, include_output=False
        )
        # Per-table scaling: doubling the chain length ~doubles spill. The old
        # cross-product (all key widths x all rows) would ~quadruple it.
        ratio = s8.spill_bytes / s4.spill_bytes
        assert 1.8 <= ratio <= 2.5
        # Edge multiplicity is a chain constant (interior table = 2), not N-dependent.
        assert max_per_table_edge_count(g8) == max_per_table_edge_count(g4) == 2

    def test_wide_chain_estimate_stays_bounded(self) -> None:
        rows = 1_000_000
        profile, graph = self._chain(30, rows)
        pred = predict_ooc_disk_bytes(
            profile, graph=graph, table_kinds=self._kinds(30), config={}, include_output=True
        )
        # 30 tables x 1M int-key rows: the per-table model keeps this in the
        # low-GiB range; the old O(N^2) cross-product predicted ~52 GiB.
        assert pred.total_bytes < 15 * _GIB


# ---------------------------------------------------------------------------
# 6. Routing-level wiring end to end (run_pipeline).
# ---------------------------------------------------------------------------


def _write_source(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _hash_col(name: str, namespace: str) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": namespace}


def _fk_ooc_config(tmp_path: Path) -> dict[str, Any]:
    parent = pa.table({"id": pa.array([f"p{i}" for i in range(_N)], type=pa.string())})
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(_N)], type=pa.string()),
            "parent_id": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
        }
    )
    parent_src = _write_source(tmp_path, parent, "parent")
    child_src = _write_source(tmp_path, child, "child")
    return {
        "version": 1,
        "global_settings": {"job_name": "ooc-d-disk-preflight", "seed": 7},
        "sources": {
            "parent": {"type": "file", "path": parent_src, "format": "parquet"},
            "child": {"type": "file", "path": child_src, "format": "parquet"},
        },
        "targets": {
            "parent": {
                "type": "file",
                "path": str(tmp_path / "parent.out.parquet"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": str(tmp_path / "child.out.parquet"),
                "format": "parquet",
            },
        },
        "tables": [
            {"name": "parent", "columns": [_hash_col("id", "ns")]},
            {"name": "child", "columns": [_hash_col("cid", "cns"), _hash_col("parent_id", "ns")]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            }
        ],
    }


def _cyclic_fk_config(tmp_path: Path) -> dict[str, Any]:
    a = pa.table(
        {
            "id": pa.array([f"a{i}" for i in range(_N)], type=pa.string()),
            "ref_b": pa.array([f"b{i}" for i in range(_N)], type=pa.string()),
        }
    )
    b = pa.table(
        {
            "id": pa.array([f"b{i}" for i in range(_N)], type=pa.string()),
            "ref_a": pa.array([f"a{i}" for i in range(_N)], type=pa.string()),
        }
    )
    a_src = _write_source(tmp_path, a, "a")
    b_src = _write_source(tmp_path, b, "b")
    return {
        "version": 1,
        "global_settings": {"job_name": "ooc-d-cyclic-unaffected", "seed": 7},
        "sources": {
            "a": {"type": "file", "path": a_src, "format": "parquet"},
            "b": {"type": "file", "path": b_src, "format": "parquet"},
        },
        "targets": {
            "a": {"type": "file", "path": str(tmp_path / "a.out.parquet"), "format": "parquet"},
            "b": {"type": "file", "path": str(tmp_path / "b.out.parquet"), "format": "parquet"},
        },
        "tables": [
            {"name": "a", "columns": [_hash_col("id", "na"), _hash_col("ref_b", "nb")]},
            {"name": "b", "columns": [_hash_col("id", "nb"), _hash_col("ref_a", "na")]},
        ],
        "relationships": [
            {
                "parent": {"table": "a", "columns": ["id"]},
                "children": [{"table": "b", "columns": ["ref_a"]}],
                "orphan_policy": "preserve",
                "namespace": "na",
            },
            {
                "parent": {"table": "b", "columns": ["id"]},
                "children": [{"table": "a", "columns": ["ref_b"]}],
                "orphan_policy": "preserve",
                "namespace": "nb",
            },
        ],
    }


def _sources(config: dict[str, Any]) -> dict[str, pa.Table]:
    return {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}


def _patch_free_disk(monkeypatch: pytest.MonkeyPatch, free_bytes: int) -> None:
    usage = SimpleNamespace(total=free_bytes * 2, used=free_bytes, free=free_bytes)
    monkeypatch.setattr(budget_mod.shutil, "disk_usage", lambda path: usage)


class TestRunPipelineDiskPreflight:
    def test_forced_out_of_core_warns_advisory_and_runtime_cap_enforces(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = _fk_ooc_config(tmp_path)
        sources = _sources(config)
        _patch_free_disk(monkeypatch, 100)  # 100 bytes free
        with caplog.at_level(logging.WARNING, logger="decoy_engine.execution.out_of_core"):
            with pytest.raises(ExecutionError) as excinfo:
                run_pipeline(config, sources, engine_version="0.1.0", execution_mode="out_of_core")
        # The advisory WARNED up front (did not reject the route)...
        assert any("out-of-core disk advisory" in r.message for r in caplog.records)
        # ...and the RUNTIME cap (check_temp_disk_budget, the real enforcer) is
        # what aborted the run at a table boundary -- NOT a preflight reject.
        assert excinfo.value.code == "out_of_core_temp_disk_exceeded"

    def test_auto_large_fk_job_also_gets_the_advisory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = _fk_ooc_config(tmp_path)
        sources = _sources(config)
        _patch_free_disk(monkeypatch, 100)
        with caplog.at_level(logging.WARNING, logger="decoy_engine.execution.out_of_core"):
            with pytest.raises(ExecutionError) as excinfo:
                run_pipeline(
                    config,
                    sources,
                    engine_version="0.1.0",
                    out_of_core_threshold_rows=10,
                    use_byte_estimate_routing=False,
                )
        assert any("out-of-core disk advisory" in r.message for r in caplog.records)
        assert excinfo.value.code == "out_of_core_temp_disk_exceeded"

    def test_sufficient_disk_proceeds_and_threads_temp_disk_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import decoy_engine.execution.out_of_core as ooc_pkg

        config = _fk_ooc_config(tmp_path)
        sources = _sources(config)
        captured: dict[str, Any] = {}
        real_run_fk_out_of_core = ooc_pkg.run_fk_out_of_core

        def spy(*args: object, **kwargs: object) -> object:
            captured["temp_disk_budget_bytes"] = kwargs.get("temp_disk_budget_bytes")
            return real_run_fk_out_of_core(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(ooc_pkg, "run_fk_out_of_core", spy)
        # Real disk (not patched): a 20-row job's tiny footprint passes.
        result = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="out_of_core")

        assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"
        assert captured.get("temp_disk_budget_bytes") is not None
        assert captured["temp_disk_budget_bytes"] > 0


class TestOocIncompatibleJobUnaffected:
    def test_cyclic_fk_job_never_reaches_the_disk_preflight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _cyclic_fk_config(tmp_path)
        sources = _sources(config)
        _patch_free_disk(monkeypatch, 100)  # catastrophically short -- must not matter
        result = run_pipeline(
            config, sources, engine_version="0.1.0", use_byte_estimate_routing=False
        )
        assert result.quality_metrics["execution"]["execution_mode"] == "full_frame"
        assert result.quality_metrics["execution"]["route_reason"] == "cross_table_cycle"

    def test_cyclic_fk_behavior_identical_with_and_without_hostile_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _cyclic_fk_config(tmp_path)
        sources = _sources(config)
        baseline = run_pipeline(
            config, sources, engine_version="0.1.0", use_byte_estimate_routing=False
        )
        _patch_free_disk(monkeypatch, 100)
        hostile = run_pipeline(
            config, sources, engine_version="0.1.0", use_byte_estimate_routing=False
        )
        assert baseline.quality_metrics["execution"] == hostile.quality_metrics["execution"]
        for table in ("a", "b"):
            assert baseline.outputs[table].equals(hostile.outputs[table])


class TestDefaultTempRoot:
    def test_matches_tempfile_gettempdir(self) -> None:
        import tempfile

        assert default_ooc_temp_root() == Path(tempfile.gettempdir())


class TestFloatFkTokenSizing:
    """Finding #13: the spill estimator must not under-count a FLOAT FK key's
    staged join token. The RI fix (2026-07-25) renders a fractional float as its
    exact decimal expansion and a whole-valued float as a folded int, either of
    which dwarfs the ~28-byte int-key floor. int/string keys are unaffected."""

    def test_float_key_priced_at_float_bound_not_int_floor(self) -> None:
        # A float64/float32 key takes the wide float bound, well above the
        # 28-byte int floor a fixed-width numeric key would otherwise get.
        for dtype in ("float64", "float32"):
            got = spill_mod._staged_key_token_bytes(8.0, dtype)
            assert got == float(spill_mod._FLOAT_FK_TOKEN_MAX_BYTES)
            assert got > float(spill_mod.MIN_KEY_TOKEN_BYTES)

    def test_float_bound_covers_real_worst_case_tokens(self) -> None:
        # The estimator's per-key float bound (token text + framing) must not be
        # below any real staged token fk_join_key mints for a double -- the
        # no-under-count invariant, and a drift guard on the tokenizer. Covers
        # the folded-int extremes (near-max magnitude, both signs), a subnormal,
        # and fractional values (the DEC branch).
        import math

        extremes = [
            sys.float_info.max,
            -sys.float_info.max,
            5e-324,  # smallest positive subnormal
            -5e-324,
            1e308,
            0.1,
            math.pi,
            1e-10,
            123456789.0,
        ]
        bound = spill_mod._staged_key_token_bytes(8.0, "float64")
        for v in extremes:
            real_staged = len(fk_join_key(v).encode()) + spill_mod.FK_TOKEN_FRAMING_BYTES
            assert bound >= real_staged, f"{v!r}: bound {bound} < real {real_staged}"

    def test_non_float_key_sizing_unchanged(self) -> None:
        # int keys keep the 28-byte floor; string keys keep width + framing.
        assert spill_mod._staged_key_token_bytes(8.0, "int64") == float(
            spill_mod.MIN_KEY_TOKEN_BYTES
        )
        assert spill_mod._staged_key_token_bytes(8.0, "int32") == float(
            spill_mod.MIN_KEY_TOKEN_BYTES
        )
        assert spill_mod._staged_key_token_bytes(100.0, "object") == 100.0 + float(
            spill_mod.FK_TOKEN_FRAMING_BYTES
        )

    def _one_edge_profile(self, dtype: str, rows: int) -> tuple[Profile, RelationshipGraph, dict]:
        parent = TableProfile(
            name="p",
            row_count=rows,
            columns=(_col("id", dtype, rows=rows),),
        )
        child = TableProfile(
            name="c",
            row_count=rows,
            columns=(_col("pid", dtype, rows=rows, is_fk=True, fk_target=("p", "id")),),
        )
        graph = RelationshipGraph(edges=(_edge("p", "id", "c", "pid"),), ordering=())
        return _profile((parent, child)), graph, _all_mask("p", "c")

    def test_float_fk_spill_exceeds_equal_width_int_fk(self) -> None:
        # End-to-end on the public API: a float64 FK key (same 8-byte source
        # width as int64) must predict a strictly LARGER spill footprint,
        # because its staged token is wider. Before finding #13's fix both
        # priced at the 28-byte floor and these were EQUAL.
        rows = 5_000_000
        fp_f, g_f, kinds = self._one_edge_profile("float64", rows)
        fp_i, g_i, _ = self._one_edge_profile("int64", rows)
        float_pred = predict_ooc_disk_bytes(
            fp_f, graph=g_f, table_kinds=kinds, config={}, include_output=False
        )
        int_pred = predict_ooc_disk_bytes(
            fp_i, graph=g_i, table_kinds=kinds, config={}, include_output=False
        )
        assert float_pred.spill_bytes > int_pred.spill_bytes
