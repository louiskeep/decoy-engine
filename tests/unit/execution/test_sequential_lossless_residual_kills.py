"""Kill the 3 residual `run_sequential` mutants on the lossless-int-typing union.

Target: `execution/_sequential.py`'s per-table load column set

    to_pandas_fk_safe(
        src,
        fk_columns_for_table(graph.edges, table)
        | group_anchor_cols.get(table, set())   # <- mut 137: table -> None
        | top_code_cols.get(table, set()),      # <- mut 131 (| -> &) / 141 (table -> None)
    )

This set names every column that MUST ingest losslessly as a nullable Int64
rather than through the bare `to_pandas()` that widens a null-bearing int64
column to float64. float64 has a 53-bit mantissa, so it cannot represent EVERY
integer with magnitude > 2**53 exactly: the odd ones round (2**53 + 1 rounds to
2**53, ties-to-even, 2**53 is even), while even ones like 2**53 + 2 stay exact.
The residual mutants each drop one contribution from that set, so a null-bearing
int64 group-anchor / top-code column widens and a non-representable > 2**53 value
is silently rounded (the fixtures below use exactly such a value). The mechanism only surfaces with such a column, which
is why the string-anchor group_by test and the plain top_code tests (both drive
non-sequential routes or string/small-int columns) leave these three alive.

These tests drive the SEQUENTIAL route directly (`run_sequential`) with a
null-bearing int64 column carrying a value differing from a neighbour only beyond
float64 precision, and assert the exact machine outcome:

- top_code (mut 131 + 141): a huge-magnitude negative in-range value renders to
  its EXACT decimal string; under the mutant it rounds to the neighbouring
  representable double and renders a DIFFERENT integer.
- date_shift group_by (mut 137): two entity ids that are distinct as int64 but
  collapse to one double drive two distinct keyed offsets on the real code; under
  the mutant the anchor widens to float and `_canonicalize_source` hard-errors on
  a float (S5 determinism envelope), so the run fails closed instead of shifting.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution import PandasExecutionAdapter

# 2**53 is the last integer every larger neighbour can no longer be represented
# beside exactly in float64. 2**53 + 1 rounds to 2**53, so the two differ as
# int64 but are indistinguishable once widened to a double.
_TWO53 = 2**53  # 9007199254740992, exactly representable (a power of two)
_TWO53_PLUS_1 = 2**53 + 1  # 9007199254740993, rounds to _TWO53 in float64
_BIG_NEG_IN_RANGE = -(2**53 + 1)  # -9007199254740993, rounds to -(2**53)


def _compile_job(config: dict[str, Any]):
    """Compile a raw config into (plan, graph, ns, registry) -- the same block
    `run_pipeline` runs, so `run_sequential` can be called directly (mirrors
    `test_sequential_run_kills.compile_job`)."""
    from decoy_engine.plan import compile_plan
    from decoy_engine.plan._seed import _normalize_job_seed_int
    from decoy_engine.profile import profile_source
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships import (
        RelationshipGraph,
        build_namespace_registry,
        build_relationship_graph,
        check_orphan_fk_policy_completeness,
    )

    job_seed = _normalize_job_seed_int(config)
    profile = profile_source(config, seed=job_seed)
    plan = compile_plan(config, profile, decoy_engine_version="0.1.0")
    ns = build_namespace_registry(config, profile)
    if profile.relationships:
        lookup = check_orphan_fk_policy_completeness(config, profile.relationships)
        graph = build_relationship_graph(
            profile.relationships, namespace_registry=ns, orphan_policy_lookup=lookup
        )
    else:
        graph = RelationshipGraph(edges=(), ordering=())
    return plan, graph, ns, get_default_registry()


def _loader(sources: dict[str, pa.Table]):
    def load(table: str) -> pa.Table:
        return sources[table]

    return load


def _sources(config: dict[str, Any]) -> dict[str, pa.Table]:
    return {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}


def _write_source(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _run_sequential(config: dict[str, Any]):
    plan, graph, ns, registry = _compile_job(config)
    return PandasExecutionAdapter().run_sequential(
        plan,
        _loader(_sources(config)),
        registry=registry,
        relationship_graph=graph,
        namespace_registry=ns,
    )


# ===========================================================================
# top_code -- kills mut 131 (second `|` -> `&`) and mut 141 (`.get(table)` ->
# `.get(None)`). Both drop the top_code column from the lossless set.
# ===========================================================================


def _top_code_config(tmp_path: Path) -> dict[str, Any]:
    # A single table `t`, column `n` masked by top_code with a SMALL cap (89) and
    # NO floor, so a huge-magnitude NEGATIVE value is in-range (below the cap, no
    # bottom bound) and renders to its exact decimal string. The column is
    # null-bearing int64: the null is what forces the bare `to_pandas()` path to
    # widen the whole column to float64 when top_code is dropped from the lossless
    # set -- rounding the > 2**53-magnitude value.
    src = pa.table({"n": pa.array([_BIG_NEG_IN_RANGE, 1, None, 90], type=pa.int64())})
    return {
        "version": 1,
        "global_settings": {"job_name": "residual-top-code", "seed": 42},
        "sources": {
            "t": {"type": "file", "path": _write_source(tmp_path, src, "t"), "format": "parquet"}
        },
        "targets": {
            "t": {"type": "file", "path": str(tmp_path / "t.out.parquet"), "format": "parquet"}
        },
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": "n",
                        "strategy": "top_code",
                        "provider_config": {"cap": 89, "over_label": "OVER"},
                    }
                ],
            }
        ],
    }


def test_top_code_huge_in_range_value_survives_exact_via_lossless_ingest(tmp_path: Path) -> None:
    res = _run_sequential(_top_code_config(tmp_path))
    out = res.outputs["t"].column("n").to_pylist()
    # Real code: the in-range value ingests as nullable Int64 and top_code renders
    # its exact Python int. Under mut 131/141 the column widens to float64 and
    # -(2**53 + 1) rounds to -(2**53), rendering "-9007199254740992" instead.
    assert out == [str(_BIG_NEG_IN_RANGE), "1", None, "OVER"]
    assert out[0] == "-9007199254740993"


# ===========================================================================
# date_shift group_by -- kills mut 137 (`group_anchor_cols.get(table)` ->
# `.get(None)`), which drops the group-anchor column from the lossless set.
# ===========================================================================


def _group_by_config(tmp_path: Path) -> dict[str, Any]:
    # A single table `t`: `d` is date_shift-anchored to the entity column
    # `entity_id`, which is a null-bearing int64 whose first two rows differ only
    # beyond float64 precision (2**53 vs 2**53 + 1). Same source date on both, so
    # a difference in the shifted output is a difference in the keyed offset --
    # which exists only if the two anchors stay distinct (lossless Int64). The
    # null row forces the widening under the mutant; `entity_id` is a passthrough
    # column so the null-bearing-int ingest guard (truncate/hash/categorical
    # only) does not reject it and DE-03 does not flag it.
    src = pa.table(
        {
            "d": pa.array(["2020-01-01", "2020-01-01", "2020-01-01"], type=pa.string()),
            "entity_id": pa.array([_TWO53, _TWO53_PLUS_1, None], type=pa.int64()),
        }
    )
    return {
        "version": 1,
        "global_settings": {"job_name": "residual-group-by", "seed": 7},
        "sources": {
            "t": {"type": "file", "path": _write_source(tmp_path, src, "t"), "format": "parquet"}
        },
        "targets": {
            "t": {"type": "file", "path": str(tmp_path / "t.out.parquet"), "format": "parquet"}
        },
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": "d",
                        "strategy": "date_shift",
                        "provider_config": {
                            "min_days": -30000,
                            "max_days": 30000,
                            "date_format": "%Y-%m-%d",
                            "group_by": "entity_id",
                        },
                        "namespace": "dates",
                    },
                    {"name": "entity_id", "strategy": "passthrough"},
                ],
            }
        ],
    }


def test_group_by_int_anchor_beyond_float_precision_keeps_entities_distinct(
    tmp_path: Path,
) -> None:
    res = _run_sequential(_group_by_config(tmp_path))
    out = res.outputs["t"]

    # The passthrough anchor keeps both large ids EXACTLY: lossless Int64 ingest,
    # not a rounded float. (Under the mutant the run never reaches output -- the
    # widened float anchor hard-errors in `_canonicalize_source` -- so this is the
    # positive half of the contract.)
    assert out.column("entity_id").to_pylist() == [_TWO53, _TWO53_PLUS_1, None]

    fmt = "%Y-%m-%d"
    out_dates = [datetime.datetime.strptime(v, fmt).date() for v in out.column("d").to_pylist()]
    # Both source dates are identical, so different shifted dates mean different
    # keyed offsets -- which can only happen if the two anchors are still distinct
    # ints. Under mut 137 the anchor widens to float64, 2**53 and 2**53 + 1
    # collapse to one double, and the run fails closed on float canonicalization;
    # either way the equal-shift outcome this asserts against cannot be produced.
    assert out_dates[0] != out_dates[1]
