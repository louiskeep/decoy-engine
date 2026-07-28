"""Mutation-kill tests for `_probe_scale.py` (TQ isolated-substrate grade,
branch `tq/isolated-substrate-grade`). Companion to `test_probe_scale.py`:
each test here pins the exact machine field a surviving mutant flips -- a
row-count validation boundary, a per-table loop's skip-vs-abort semantics, a
dropped `floor_rows` forwarding, or the generate-table gate that decides which
tables reach the probe's `row_counts` ground truth. See the mutation ledger
`docs/quality/mutation-ledgers/execution_probe_scale.md` for the survivor->kill
map.
"""

from __future__ import annotations

import pyarrow as pa

from decoy_engine.execution._probe_scale import (
    downscale_config,
    downscale_job,
    scale_row_count,
)


def _generate_table(name: str, row_count: int) -> dict:
    return {
        "name": name,
        "row_count": row_count,
        "generate_columns": [{"name": "id", "type": "sequence", "start": 0}],
    }


def _mask_table(name: str) -> dict:
    return {"name": name, "columns": [{"name": "email", "strategy": "faker"}]}


class TestScaleRowCountZeroBoundary:
    def test_zero_rows_is_valid_and_scales_to_zero(self) -> None:
        # scale_row_count 1 (`< 0` -> `<= 0`) and 2 (`< 0` -> `< 1`): an
        # empty table is a legal input (0 rows scale to 0 rows), not a
        # validation error. Both mutants move the guard to reject 0.
        assert scale_row_count(0, 0.5, floor_rows=0) == 0


class TestDownscaleConfigSkipSemantics:
    def test_non_dict_entry_is_skipped_not_a_loop_abort(self) -> None:
        # downscale_config 9 (`continue` -> `break` on the non-dict guard):
        # a junk entry must be stepped over, leaving every LATER generate
        # table still scaled -- `break` would abandon the trailing table.
        config = {
            "version": 1,
            "tables": ["not-a-dict", _generate_table("customers", 1_000_000)],
        }
        scaled = downscale_config(config, 0.01, floor_rows=0)
        assert scaled["tables"][1]["row_count"] == 10_000

    def test_mask_table_is_skipped_not_a_loop_abort(self) -> None:
        # downscale_config 14 (`continue` -> `break` on the no-generate_columns
        # guard): a mask table interleaved before a generate table must not
        # stop the loop -- the generate table after it still gets scaled.
        config = {
            "version": 1,
            "tables": [_mask_table("people"), _generate_table("customers", 1_000_000)],
        }
        scaled = downscale_config(config, 0.01, floor_rows=0)
        assert scaled["tables"][1]["row_count"] == 10_000

    def test_floor_rows_is_forwarded_to_scale_row_count(self) -> None:
        # downscale_config 27 (drops `floor_rows=floor_rows`, taking the callee
        # default 2_000): with floor_rows=0 a sub-floor scale must land on its
        # raw scaled value, not be lifted to the default floor.
        config = {"version": 1, "tables": [_generate_table("customers", 1_000_000)]}
        scaled = downscale_config(config, 0.0001, floor_rows=0)
        assert scaled["tables"][0]["row_count"] == 100


class TestDownscaleJobFloorForwarding:
    def test_config_floor_rows_is_forwarded(self) -> None:
        # downscale_job 7 (config call drops `floor_rows`): with floor_rows=0 a
        # sub-floor generate table lands on 100, not the callee default 2_000.
        config = {"version": 1, "tables": [_generate_table("gen", 1_000_000)]}
        job = downscale_job(config, None, 0.0001, floor_rows=0)
        assert job.config["tables"][0]["row_count"] == 100
        assert job.row_counts == {"gen": 100}

    def test_sources_floor_rows_is_forwarded(self) -> None:
        # downscale_job 14 (sources call drops `floor_rows`): with floor_rows=0
        # a sub-floor resident table is sliced to 100 rows, not lifted to 2_000.
        sources = {"src": pa.table({"id": list(range(1_000_000))})}
        job = downscale_job({"version": 1, "tables": []}, sources, 0.0001, floor_rows=0)
        assert job.sources["src"].num_rows == 100
        assert job.row_counts == {"src": 100}


class TestDownscaleJobRowCountsGate:
    def test_only_generate_tables_contribute_to_row_counts(self) -> None:
        # downscale_job 21 (`or` -> `and` in the skip guard): a mask table
        # (dict, no generate_columns) must be excluded from the config-derived
        # row_counts even when it carries a stray row_count/name. `and` only
        # skips the not-a-dict-AND-no-columns case, so the mutant would admit
        # this mask table's 777.
        mask_with_rowcount = {
            "name": "m",
            "row_count": 777,
            "columns": [{"name": "email", "strategy": "faker"}],
        }
        job = downscale_job({"version": 1, "tables": [mask_with_rowcount]}, None, 0.01, floor_rows=0)
        assert job.row_counts == {}

    def test_mask_table_does_not_abort_the_row_counts_loop(self) -> None:
        # downscale_job 27 (`continue` -> `break` in the row_counts loop): a
        # mask table before a generate table must not stop row_counts
        # collection -- the generate table after it still records its scaled
        # count.
        config = {
            "version": 1,
            "tables": [_mask_table("people"), _generate_table("gen", 1_000_000)],
        }
        job = downscale_job(config, None, 0.01, floor_rows=0)
        assert job.row_counts == {"gen": 10_000}

    def test_row_counts_requires_both_a_count_and_a_name(self) -> None:
        # downscale_job 36 (`and` -> `or` in the isinstance guard): a generate
        # table missing its row_count must NOT be recorded -- recording needs
        # BOTH an int row_count and a str name. `or` would write {name: None}.
        gen_no_count = {
            "name": "gen_norows",
            "generate_columns": [{"name": "id", "type": "sequence", "start": 0}],
        }
        job = downscale_job({"version": 1, "tables": [gen_no_count]}, None, 0.01, floor_rows=0)
        assert job.row_counts == {}
