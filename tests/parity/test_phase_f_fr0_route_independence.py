"""Phase F FR0: one generation core, so the resident and streamed routes emit the same bytes.

The contract replaces byte-parity-to-the-old-resident-output with two guarantees over a pinned
input envelope: REPEATABILITY (a fresh process reproduces the bytes) and ROUTE-INDEPENDENCE
(resident == streamed, for any partition of the rows into batches). Hypothesis drives the
partition layouts, including zero-row batches, a cut at row 0 and at the table's last row, and a
cut that straddles the faker pool admission threshold. Values must also equal the legacy
whole-table generator's for every column the legacy path types the same way, which is the
evidence that persistent per-batch state reproduces one whole-column draw.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from decoy_engine.generation import _faker_pool, _plan_entry
from decoy_engine.generation._admission import classify_plan
from decoy_engine.generation._streamed import run_generation
from decoy_engine.generation.synthesize import _generate_tables_from_config
from tests.parity import _f0_fixtures as fx
from tests.parity._f0_canonical import assert_matches_oracle, fingerprints_diff
from tests.parity._f0_expected import EXPECTED
from tests.parity._fr0_support import (
    fingerprints,
    partition_from_cuts,
    plan_of,
    prepared_config,
    run_resident,
    run_streamed,
)

REPO = Path(__file__).resolve().parents[2]
CORE_FIXTURES = (
    "sequence",
    "categorical",
    "pooled_faker",
    "live_faker",
    "live_faker_default_locale",
    "nulls",
    "reference_core",
)


def _eligible(cfg: dict[str, Any]) -> bool:
    return classify_plan(prepared_config(cfg)).eligible


def test_the_core_fixtures_really_take_the_core() -> None:
    for fid in CORE_FIXTURES:
        cfg = fx.FIXTURES_BY_ID[fid].run.builder()  # type: ignore[attr-defined]
        assert _eligible(cfg), fid
    for fid in ("formula", "reference", "derived", "grouped_windowed", "keyed_derive"):
        cfg = fx.FIXTURES_BY_ID[fid].run.builder()  # type: ignore[attr-defined]
        assert not _eligible(cfg), f"{fid} must stay on the legacy path"


# ---- the F0 oracle, through irregular partitions, both routes --------------------------------


def _fixture_cfg(fid: str) -> dict[str, Any]:
    return fx.FIXTURES_BY_ID[fid].run.builder()  # type: ignore[attr-defined]


def _fixture_kwargs(fid: str) -> dict[str, Any]:
    return fx.FIXTURES_BY_ID[fid].run.kwargs  # type: ignore[attr-defined]


@pytest.mark.parametrize("fid", CORE_FIXTURES)
@pytest.mark.parametrize("cuts", [[], [1], [0, 0, 7, 7, 8], [3, 11, 29], [1, 2, 3, 4, 5, 6, 7]])
def test_resident_route_matches_the_oracle_over_irregular_partitions(
    fid: str, cuts: list[int]
) -> None:
    cfg = _fixture_cfg(fid)
    tables = {t["name"] for t in cfg["tables"]}
    part = partition_from_cuts({name: cuts for name in tables})
    out = run_resident(
        cfg, part, instance_default_locale=_fixture_kwargs(fid).get("instance_default_locale")
    )
    assert_matches_oracle(fid, out)


def test_pooled_oracle_is_reproduced_across_the_pool_threshold_in_every_layout() -> None:
    """The pooled column is admitted on the TABLE row count (50_000), not on a batch."""
    cfg = fx.cfg_pooled_faker()
    for cuts in ([25_000], [49_999], [1, 49_999], [10_000, 10_000, 40_000], [0, 50_000]):
        out = run_resident(cfg, partition_from_cuts({"pooled": cuts}))
        assert_matches_oracle("pooled_faker", out)


@pytest.mark.parametrize("fid", ["sequence", "categorical", "live_faker", "nulls"])
def test_streamed_route_equals_the_resident_route(fid: str, tmp_path: Path) -> None:
    cfg = _fixture_cfg(fid)
    names = {t["name"] for t in cfg["tables"]}
    result, committed = run_streamed(
        cfg, tmp_path / "out", partition_from_cuts({n: [2, 9, 9, 17] for n in names})
    )
    assert result.mode == "streamed"
    resident = run_resident(cfg, partition_from_cuts({n: [5] for n in names}))
    assert fingerprints(committed) == fingerprints(resident)
    assert {n: t.num_rows for n, t in committed.items()} == result.row_counts


# ---- Hypothesis: any partition, both routes, a generation with a parent/child reference ------


def _relational_cfg(rows: int, child_rows: int) -> dict[str, Any]:
    parent_cols: list[dict[str, Any]] = [
        {"name": "id", "type": "sequence", "start": 7, "step": 3, "prefix": "P-", "pad_length": 5},
        {
            "name": "kind",
            "type": "categorical",
            "categories": ["a", "b", "c"],
            "weights": [5, 2, 1],
        },
        {"name": "city", "type": "faker", "faker_type": "city"},
        {"name": "pooled_city", "type": "faker", "faker_type": "city", "null_probability": 0.25},
        {"name": "nick", "type": "faker", "faker_type": "user_name", "null_probability": 0.3},
    ]
    child_cols: list[dict[str, Any]] = [
        {"name": "cid", "type": "sequence", "start": 1},
        *[
            {
                "name": f"p_{dist}",
                "type": "reference",
                "reference_table": "parent",
                "reference_column": "id",
                "distribution": dist,
                **({"weights": list(range(1, rows + 1))} if dist == "weighted" else {}),
            }
            for dist in ("random", "sequential", "weighted")
        ],
        {
            "name": "flag",
            "type": "categorical",
            "categories": [True, False],
            "null_probability": 0.2,
        },
        {"name": "score", "type": "categorical", "categories": [1.5, 2.5, 4.0]},
    ]
    return fx.config(
        [fx.table("child", child_rows, child_cols), fx.table("parent", rows, parent_cols)]
    )


def _cuts(rows: int) -> st.SearchStrategy[list[int]]:
    return st.lists(st.integers(min_value=0, max_value=rows), max_size=6)


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    rows=st.integers(0, 120),
    child_rows=st.integers(0, 120),
    data=st.data(),
)
def test_any_partition_gives_the_same_bytes_on_both_routes(
    rows: int, child_rows: int, data: st.DataObject
) -> None:
    cfg = _relational_cfg(rows, child_rows)
    layout_a = {"parent": data.draw(_cuts(rows)), "child": data.draw(_cuts(child_rows))}
    layout_b = {"parent": data.draw(_cuts(rows)), "child": data.draw(_cuts(child_rows))}
    with pytest.MonkeyPatch.context() as mp:
        # Lower the pool threshold so the "pooled_city" column pools at these sizes.
        mp.setattr(_faker_pool, "N_THRESHOLD", 40)
        whole = run_resident(cfg, None)
        resident = run_resident(cfg, partition_from_cuts(layout_a))
        with tempfile.TemporaryDirectory() as tmp:
            result, streamed = run_streamed(cfg, Path(tmp) / "out", partition_from_cuts(layout_b))
        assert result.mode == "streamed"
    assert fingerprints(resident) == fingerprints(whole)
    assert fingerprints(streamed) == fingerprints(whole)


# Columns whose values the FR3 re-baseline left byte-identical to the legacy whole-table generator:
# arithmetic sequences, the `sequential` reference (index arithmetic) and per-row Faker. Every other
# column of `_relational_cfg` is a uniform or weighted selection that now rides the index kernel.
_LEGACY_IDENTICAL = {
    ("parent", "id"),
    ("parent", "nick"),
    ("child", "cid"),
    ("child", "p_sequential"),
}


@settings(max_examples=15, deadline=None)
@given(rows=st.integers(41, 200), cuts=_cuts(200))
def test_core_values_equal_the_legacy_whole_table_generator_outside_kernel_selection(
    rows: int, cuts: list[int]
) -> None:
    """Persistent per-batch state reproduces the historical whole-column draws, value for value,
    for every column that is not a kernel-selected one. Null positions are independent of the
    selection and so equal the legacy path's for EVERY column."""
    cfg = _relational_cfg(rows, rows)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_faker_pool, "N_THRESHOLD", 40)
        core = run_resident(cfg, partition_from_cuts({"parent": cuts, "child": cuts}))
        legacy = _generate_tables_from_config(prepared_config(cfg))
    assert list(core) == list(legacy)
    for name, table in legacy.items():
        for col in table.column_names:
            got, want = core[name].column(col).to_pylist(), table.column(col).to_pylist()
            assert [v is None for v in got] == [v is None for v in want], (name, col)
            if (name, col) in _LEGACY_IDENTICAL:
                assert got == want, (name, col)


@settings(max_examples=15, deadline=None)
@given(rows=st.integers(41, 200), cuts=_cuts(200))
def test_kernel_selected_columns_stay_inside_their_configured_sets(
    rows: int, cuts: list[int]
) -> None:
    """The kernel columns differ from the legacy draw by design; what must hold is membership."""
    cfg = _relational_cfg(rows, rows)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_faker_pool, "N_THRESHOLD", 40)
        core = run_resident(cfg, partition_from_cuts({"parent": cuts, "child": cuts}))
    parent, child = core["parent"], core["child"]
    assert set(parent.column("kind").to_pylist()) <= {"a", "b", "c"}
    assert set(child.column("flag").to_pylist()) <= {True, False, None}
    assert set(child.column("score").to_pylist()) <= {1.5, 2.5, 4.0}
    ids = set(parent.column("id").to_pylist())
    for col in ("p_random", "p_sequential", "p_weighted"):
        assert set(child.column(col).to_pylist()) <= ids, col


# ---- repeatability: fresh processes -----------------------------------------------------------

_CHILD = """
import json, sys
from tests.parity import _f0_fixtures as fx
from tests.parity._f0_canonical import table_fingerprint
out = {}
with fx.frozen_faker_clock():
    for fid in sys.argv[1:]:
        out[fid] = {n: table_fingerprint(t) for n, t in fx.FIXTURES_BY_ID[fid].run().items()}
print(json.dumps(out, default=list))
"""


def _fresh_process(fids: list[str], hashseed: str) -> dict[str, Any]:
    import os

    env = {**os.environ, "PYTHONHASHSEED": hashseed, "PYTHONPATH": f"{REPO / 'src'}:{REPO}"}
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _CHILD, *fids],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
    )
    parsed: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    return parsed


def test_fresh_processes_repeat_the_golden_byte_for_byte() -> None:
    fids = [
        "sequence",
        "categorical",
        "live_faker",
        "live_faker_default_locale",
        "nulls",
        "pooled_faker",
        "reference_core",
    ]
    first = _fresh_process(fids, "0")
    second = _fresh_process(fids, "98765")
    assert first == second
    for fid in fids:
        for table, fp in first[fid].items():
            want = EXPECTED[fid][table]
            assert not fingerprints_diff(
                {**want, "columns": [list(c) for c in want["columns"]]},
                fp,
                label=f"{fid}.{table}: ",
            )


def test_interleaved_generations_share_no_mutable_state() -> None:
    """Two runs advancing together (one thread each, batch by batch) equal their solo runs."""
    cfg = _relational_cfg(90, 90)
    solo = fingerprints(run_resident(cfg, partition_from_cuts({"parent": [10, 40], "child": [3]})))
    barrier = threading.Barrier(2)
    outputs: dict[int, Any] = {}

    def partition(table: str, rows: int) -> list[int]:
        barrier.wait(timeout=60)  # both runs reach each table boundary together
        return [rows // 3, rows - rows // 3]

    def work(slot: int) -> None:
        outputs[slot] = fingerprints(run_resident(cfg, partition))

    threads = [threading.Thread(target=work, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)
    assert outputs[0] == outputs[1] == solo


# ---- the semantic decision does not read the environment --------------------------------------


def test_values_do_not_depend_on_sink_budget_or_batch_size(tmp_path: Path) -> None:
    cfg = _relational_cfg(150, 150)
    config = prepared_config(cfg)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_faker_pool, "N_THRESHOLD", 40)
        runs = {
            "unbounded": run_generation(config).tables,
            "small_batch": run_generation(config, batch_rows=7).tables,
            "bounded_resident": run_generation(config, budget_bytes=512 * 1024 * 1024).tables,
            "callable_sink": run_generation(config, sink=lambda *a: None).tables,
        }
        result, committed = run_streamed(cfg, tmp_path / "out", budget_bytes=512 * 1024 * 1024)
    assert result.mode == "streamed"
    base = fingerprints(runs["unbounded"])  # type: ignore[arg-type]
    for name, tables in runs.items():
        assert fingerprints(tables) == base, name  # type: ignore[arg-type]
    assert fingerprints(committed) == base


def test_generate_tables_is_the_resident_route_of_the_same_core() -> None:
    cfg = fx.cfg_live_faker()
    assert fingerprints(_plan_entry.generate_tables(plan_of(cfg))) == fingerprints(
        run_resident(cfg)
    )
