"""Sprint B2: the down-scaling helper (`_probe_scale.py`) that shrinks a job
to a small row count for the two-point micro-probe. OOM-avoidance routing
redesign, docs/plans/2026-07-10-oom-avoidance-routing-redesign.md §3.3
(corrected per §11): scale EVERY table by the SAME fraction to preserve FK
fan-out, with a floor so a probe never runs at an unrealistically tiny scale.

No subprocess/probe logic here -- purely the config/sources scaling math.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.execution._probe_scale import (
    DEFAULT_PROBE_FLOOR_ROWS,
    downscale_config,
    downscale_job,
    downscale_sources,
    scale_row_count,
)


class TestScaleRowCount:
    def test_scales_by_fraction(self) -> None:
        assert scale_row_count(1_000_000, 0.01, floor_rows=0) == 10_000

    def test_floors_a_tiny_fraction(self) -> None:
        # 1_000_000 * 0.0001 = 100, below the 2_000 floor -> floored up.
        assert scale_row_count(1_000_000, 0.0001, floor_rows=2_000) == 2_000

    def test_floor_never_exceeds_the_real_row_count(self) -> None:
        # A table with only 50 rows can never be "floored up" past 50 --
        # there is nothing to slice beyond what actually exists.
        assert scale_row_count(50, 0.01, floor_rows=2_000) == 50

    def test_default_floor_is_the_module_constant(self) -> None:
        assert scale_row_count(10_000_000, 0.0001) == DEFAULT_PROBE_FLOOR_ROWS

    def test_never_exceeds_row_count_even_without_a_floor_conflict(self) -> None:
        assert scale_row_count(100, 1.0, floor_rows=0) == 100

    def test_rejects_out_of_range_fraction(self) -> None:
        with pytest.raises(ValueError, match="fraction"):
            scale_row_count(100, 0.0, floor_rows=0)
        with pytest.raises(ValueError, match="fraction"):
            scale_row_count(100, 1.5, floor_rows=0)

    def test_rejects_negative_row_count(self) -> None:
        with pytest.raises(ValueError, match="row_count"):
            scale_row_count(-1, 0.5, floor_rows=0)

    def test_rejects_negative_floor(self) -> None:
        with pytest.raises(ValueError, match="floor_rows"):
            scale_row_count(100, 0.5, floor_rows=-1)


class TestDownscaleConfig:
    def _generate_table(self, name: str, row_count: int) -> dict:
        return {
            "name": name,
            "row_count": row_count,
            "generate_columns": [{"name": "id", "type": "sequence", "start": 0}],
        }

    def _mask_table(self, name: str) -> dict:
        return {"name": name, "columns": [{"name": "email", "strategy": "faker"}]}

    def test_scales_every_generate_table_row_count(self) -> None:
        config = {
            "version": 1,
            "tables": [self._generate_table("customers", 1_000_000)],
        }
        scaled = downscale_config(config, 0.01, floor_rows=0)
        assert scaled["tables"][0]["row_count"] == 10_000

    def test_scales_multiple_generate_tables_by_the_same_fraction(self) -> None:
        """FK fan-out preservation: a parent (1M) and a 5x-fanout child
        (5M) scaled by the SAME fraction keep their 1:5 ratio."""
        config = {
            "version": 1,
            "tables": [
                self._generate_table("parent", 1_000_000),
                self._generate_table("child", 5_000_000),
            ],
        }
        scaled = downscale_config(config, 0.01, floor_rows=0)
        parent_rows = scaled["tables"][0]["row_count"]
        child_rows = scaled["tables"][1]["row_count"]
        assert parent_rows == 10_000
        assert child_rows == 50_000
        assert child_rows / parent_rows == pytest.approx(5.0)

    def test_leaves_mask_tables_untouched(self) -> None:
        config = {"version": 1, "tables": [self._mask_table("people")]}
        scaled = downscale_config(config, 0.01, floor_rows=0)
        assert scaled["tables"][0] == self._mask_table("people")

    def test_does_not_mutate_the_original_config(self) -> None:
        config = {"version": 1, "tables": [self._generate_table("customers", 1_000_000)]}
        downscale_config(config, 0.01, floor_rows=0)
        assert config["tables"][0]["row_count"] == 1_000_000


class TestDownscaleSources:
    def test_slices_each_table_to_the_scaled_row_count(self) -> None:
        table = pa.table({"id": list(range(1_000))})
        scaled = downscale_sources({"t": table}, 0.1, floor_rows=0)
        assert scaled["t"].num_rows == 100

    def test_head_slice_is_deterministic(self) -> None:
        """A head slice (not a random sample): the same fraction always
        yields the same rows, so two probe runs of the same job differ only
        in real measurement noise, never sampling noise."""
        table = pa.table({"id": list(range(1_000))})
        scaled = downscale_sources({"t": table}, 0.01, floor_rows=0)
        assert scaled["t"].column("id").to_pylist() == list(range(10))

    def test_preserves_fan_out_ratio_across_tables(self) -> None:
        parent = pa.table({"id": list(range(1_000))})
        child = pa.table({"id": list(range(5_000))})
        scaled = downscale_sources({"parent": parent, "child": child}, 0.02, floor_rows=0)
        assert scaled["child"].num_rows / scaled["parent"].num_rows == pytest.approx(5.0)

    def test_never_exceeds_the_real_table_size(self) -> None:
        table = pa.table({"id": list(range(10))})
        scaled = downscale_sources({"t": table}, 0.5, floor_rows=2_000)
        assert scaled["t"].num_rows == 10


class TestDownscaleJob:
    def test_row_counts_cover_both_resident_and_generate_tables(self) -> None:
        config = {
            "version": 1,
            "tables": [
                {
                    "name": "gen",
                    "row_count": 1_000_000,
                    "generate_columns": [{"name": "id", "type": "sequence", "start": 0}],
                },
            ],
        }
        sources = {"mask_t": pa.table({"id": list(range(2_000_000))})}
        job = downscale_job(config, sources, 0.01, floor_rows=0)
        assert job.row_counts == {"gen": 10_000, "mask_t": 20_000}
        assert job.config["tables"][0]["row_count"] == 10_000
        assert job.sources["mask_t"].num_rows == 20_000

    def test_none_sources_produces_an_empty_sources_dict(self) -> None:
        config = {"version": 1, "tables": []}
        job = downscale_job(config, None, 0.5, floor_rows=0)
        assert job.sources == {}
        assert job.row_counts == {}
