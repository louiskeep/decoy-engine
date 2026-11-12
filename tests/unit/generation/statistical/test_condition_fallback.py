"""GEN-COND-CAP E1b: a conditional column warns when parents have no fitted combination.

AT-2b, AT-3b, AT-3s, AT-3n, AT-6, AT-7, AT-8, AT-12 and AT-14. The digests are of bytes captured
at origin/main 4694c91f, before the sampler's table choice moved into `_select_table`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import reasons
from decoy_engine.generation.statistical import load_spec, sample_column
from decoy_engine.generation.synthesize import generate_tables
from decoy_engine.plan import compile_plan
from decoy_engine.quality.snapshot import compute_distribution_snapshot
from tests.parity._f2_mixed_support import CHILD, child_table, mixed_config
from tests.parity._f2_mixed_support import run as run_mixed
from tests.unit._dps_helpers import empty_profile

CODE = "generation_condition_parent_unseen"
_WEIGHTS = (20, 15, 12, 10, 9, 8, 7, 6, 5, 4, 3, 1)
# sha256 of the sorted-key JSON rows of the generated tables, captured at origin/main 4694c91f.
_DIGEST_AT2B = "9921ef5338b259f0f8605da7cc21a555ae3b058d047f45de88aca14104317a1a"
_DIGEST_AT3B = "95e13472de99dc5ee3808da2fecc93e0e9ce98bcb4c4849687714c9c1abaf4e7"
_DIGEST_PINNED = "dd918ed3a1b7cee134a6ea03c97e8a7d8b8c1d0cb63c9b0f1986e59c03c65325"
_DETAIL_KEYS = {
    "table",
    "column",
    "condition_on",
    "rows",
    "rows_null_parent",
    "rows_total",
    "parent_values",
    "parent_values_capped",
}


def _cond_frame(int_parents: bool = False) -> pd.DataFrame:
    """6,000 rows, 36 parent/kid pairs; built without an RNG (the GEN-COND-CAP fixture)."""
    parents: list[Any] = []
    kids: list[str] = []
    for i, w in enumerate(_WEIGHTS):
        for j, share in enumerate((6, 3, 1)):
            n = w * 6 * share
            parents += [i + 1 if int_parents else f"SENTP{i + 1:02d}"] * n
            kids += [f"SENTK{(i + 1 + j) % 6 + 1}"] * n
    return pd.DataFrame({"parent": parents, "kid": kids})


def _write(tmp_path: Path, frame: pd.DataFrame, **kw: Any) -> str:
    snap = compute_distribution_snapshot(frame, joint_columns=[("parent", "kid")], **kw)
    path = tmp_path / f"snap{len(list(tmp_path.glob('snap*.json')))}.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    return str(path)


def _columns(snap: str) -> list[dict[str, Any]]:
    stat = {"type": "statistical", "snapshot_file": snap, "allow_real_categories": True}
    return [
        {"name": "parent", **stat},
        {"name": "kid", **stat, "condition_on": "parent"},
    ]


def _config(snap: str, rows: int = 6000) -> dict[str, Any]:
    return {
        "version": 2,
        "global_settings": {"seed": 42},
        "tables": [{"name": "t", "row_count": rows, "generate_columns": _columns(snap)}],
    }


def _digest(tables: dict[str, Any]) -> str:
    rows = {name: tables[name].to_pylist() for name in sorted(tables)}
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def _generate(config: dict[str, Any], sink: Any = None) -> dict[str, Any]:
    plan = compile_plan(config, empty_profile(), decoy_engine_version="test")
    return generate_tables(plan, on_warning=sink)


def _scan(texts: list[str]) -> None:
    for text in texts:
        assert "SENTP" not in text
        assert "SENTK" not in text


def _records(raised: list[Any]) -> list[Any]:
    return [r for r in raised if str(r.code) == CODE]


@pytest.fixture
def full(tmp_path: Path) -> str:
    return _write(tmp_path, _cond_frame(), contingency_top_k=36)


@pytest.fixture
def default(tmp_path: Path) -> str:
    return _write(tmp_path, _cond_frame())


def test_code_is_registered_and_matches_the_emitted_string() -> None:
    assert str(reasons.warning.GENERATION_CONDITION_PARENT_UNSEEN) == CODE


def test_at2b_full_snapshot_has_no_absent_pair_and_no_warning(tmp_path: Path, full: str) -> None:
    source = _cond_frame()
    allowed = set(zip(source["parent"], source["kid"], strict=True))
    raised: list[Any] = []
    out = _generate(_config(full), raised.append)
    pairs = set(zip(out["t"]["parent"].to_pylist(), out["t"]["kid"].to_pylist(), strict=True))
    assert pairs <= allowed
    assert _records(raised) == []
    assert _digest(out) == _DIGEST_AT2B  # AT-14


def test_at3b_default_snapshot_warns_once_with_the_independent_count(default: str) -> None:
    raised: list[Any] = []
    out = _generate(_config(default), raised.append)
    (record,) = _records(raised)
    parents = out["t"]["parent"].to_pylist()
    unseen = parents.count("SENTP12")
    assert unseen > 0
    assert record.detail == {
        "table": "t",
        "column": "kid",
        "condition_on": "parent",
        "rows": unseen,
        "rows_null_parent": 0,
        "rows_total": 6000,
        "parent_values": 1,
        "parent_values_capped": False,
    }
    assert (record.provider, record.column) == ("statistical", "kid")
    assert set(record.detail) == _DETAIL_KEYS
    assert _digest(out) == _DIGEST_AT3B  # AT-14


def test_at3s_integer_parent_labels_count_the_same(tmp_path: Path) -> None:
    snap = _write(tmp_path, _cond_frame(int_parents=True))
    raised: list[Any] = []
    out = _generate(_config(snap), raised.append)
    (record,) = _records(raised)
    parents = out["t"]["parent"].to_pylist()
    assert record.detail["rows"] == sum(1 for p in parents if str(p) == "12") > 0
    # The sampler also sees raw ints in a caller's parent list; the key is still str(parent).
    spec = _load(snap)
    from decoy_engine.generation.statistical._sample import conditional_fallback_counts

    ints = [(i % 12) + 1 for i in range(1200)]
    counts = conditional_fallback_counts(spec, ints, len(ints))
    assert tuple(counts)[:3] == (100, 0, 1)


def _load(snap: str) -> Any:
    return load_spec(_columns(snap)[1], snapshot=json.loads(Path(snap).read_text("utf-8")))


def _spec_for(tmp_path: Path, frame: pd.DataFrame, **kw: Any) -> Any:
    return _load(_write(tmp_path, frame, **kw))


def _parent_keys(spec: Any) -> set[str]:
    index = 0 if spec.parent_first else 1
    return {str(c["key"][index]) for c in spec.joint["cells"]}


def _replay(spec: Any, parents: list[Any], n: int) -> tuple[int, int, int]:
    """Per-row replay of the sampler's choice, written without `_select_table`."""
    keyed = _parent_keys(spec)
    rows = nulls = 0
    keys: set[str] = set()
    for i in range(n):
        parent = parents[i] if i < len(parents) else None
        if str(parent) not in keyed:
            rows += 1
            nulls += parent is None
            keys.add(str(parent))
    return rows, nulls, len(keys)


def _small_frame(extra: tuple[str, str] | None = None) -> pd.DataFrame:
    parents = ["SENTPA"] * 40 + ["SENTPB"] * 20
    kids = ["SENTK1"] * 40 + ["SENTK2"] * 20
    if extra:
        parents.append(extra[0])
        kids.append(extra[1])
    return pd.DataFrame({"parent": parents, "kid": kids})


def test_at3n_null_parents_count_as_fallbacks(tmp_path: Path) -> None:
    from decoy_engine.generation.statistical._sample import conditional_fallback_counts

    spec = _spec_for(tmp_path, _small_frame(), contingency_top_k=1)  # SENTPB has no cell
    n = 300
    parents = [None if i % 10 == 0 else ("SENTPA", "SENTPB", "SENTPZ")[i % 3] for i in range(n)]
    got = conditional_fallback_counts(spec, parents, n)
    assert tuple(got)[:3] == _replay(spec, parents, n)
    assert got.rows_null_parent == 30
    assert got.parent_values == 3  # SENTPB, SENTPZ and the None key
    assert len(sample_column(spec, n, col_seed=3, parent_values=parents)) == n


def test_at3n_short_parent_list_counts_missing_rows_as_null(tmp_path: Path) -> None:
    from decoy_engine.generation.statistical._sample import conditional_fallback_counts

    spec = _spec_for(tmp_path, _small_frame(), contingency_top_k=1)
    parents = ["SENTPA"] * 20
    got = conditional_fallback_counts(spec, parents, 30)
    assert tuple(got)[:3] == _replay(spec, parents, 30) == (10, 10, 1)
    assert len(sample_column(spec, 30, col_seed=3, parent_values=parents)) == 30


def test_at3n_the_label_none_shares_the_key_with_a_real_none(tmp_path: Path) -> None:
    from decoy_engine.generation.statistical._sample import conditional_fallback_counts

    spec = _spec_for(tmp_path, _small_frame(("None", "SENTK3")))
    assert "None" in _parent_keys(spec)
    parents = ["None", None, "SENTPA", "SENTPZ"]
    got = conditional_fallback_counts(spec, parents, 4)
    assert tuple(got)[:3] == _replay(spec, parents, 4) == (1, 0, 1)
    drawn = sample_column(spec, 4, col_seed=5, parent_values=parents)
    assert drawn[0] == drawn[1] == "SENTK3"


def test_distinct_parents_are_capped_and_the_row_count_stays_exact(tmp_path: Path) -> None:
    from decoy_engine.generation.statistical import conditional_fallback_counts
    from decoy_engine.generation.statistical._sample import DISTINCT_PARENT_CAP

    spec = _spec_for(tmp_path, _small_frame(), contingency_top_k=1)
    n = DISTINCT_PARENT_CAP * 3 + 7
    parents = [f"SENTPX{i}" for i in range(n)]
    got = conditional_fallback_counts(spec, parents, n)
    assert (got.rows, got.rows_null_parent) == (n, 0)
    assert got.parent_values == DISTINCT_PARENT_CAP
    assert got.parent_values_capped is True
    few = conditional_fallback_counts(spec, ["SENTPB"] * 5, 5)
    assert (few.parent_values, few.parent_values_capped) == (1, False)


def test_at3n_a_counter_never_draws(tmp_path: Path) -> None:
    from decoy_engine.generation.statistical._sample import conditional_fallback_counts

    spec = _spec_for(tmp_path, _small_frame(), contingency_top_k=1)
    parents = ["SENTPB"] * 5
    before = sample_column(spec, 5, col_seed=9, parent_values=parents)
    conditional_fallback_counts(spec, parents, 5)
    assert sample_column(spec, 5, col_seed=9, parent_values=parents) == before


def test_zero_rows_and_unconditional_columns_emit_nothing(default: str) -> None:
    raised: list[Any] = []
    _generate(_config(default, rows=0), raised.append)
    assert raised == []
    config = _config(default)
    config["tables"][0]["generate_columns"] = config["tables"][0]["generate_columns"][:1]
    _generate(config, raised.append)
    assert raised == []


def test_at6_no_value_in_a_warning_or_a_log_line(
    default: str, caplog: pytest.LogCaptureFixture
) -> None:
    raised: list[Any] = []
    with caplog.at_level(logging.DEBUG):
        _generate(_config(default), raised.append)
    (record,) = _records(raised)
    _scan([repr(record.detail), str(record.code), str(record.column), str(record.provider)])
    assert all(isinstance(v, (str, int)) for v in record.detail.values())
    lines = [r.getMessage() for r in caplog.records]
    assert any("generation_condition_parent_unseen" in line for line in lines)
    _scan(lines)


def test_at7_the_sink_does_not_change_the_bytes(default: str) -> None:
    with_sink = _generate(_config(default), [].append)
    without = _generate(_config(default))
    assert _digest(with_sink) == _digest(without) == _DIGEST_AT3B


def _mixed(tmp_path: Path, extra: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    snap = _write(tmp_path, _small_frame(), contingency_top_k=1)
    child = child_table(pa.array([str(i % 5) for i in range(40)], pa.string()))
    columns = [{"name": "id", "type": "sequence", "start": 0, "step": 1}, *_columns(snap), *extra]
    config = mixed_config(tmp_path, child, parent_columns=columns, parent_rows=40)
    return config, {CHILD: child}


_DECLINE = [{"name": "tier", "type": "categorical", "categories": ["A", None], "weights": [0, 1]}]
ROUTES = {
    "forced-full-frame": ([], "full_frame", "override_full_frame"),
    "admitted-mixed": ([], "auto", "mixed"),
    "mixed-runtime-decline": (_DECLINE, "auto", "mixed_runtime_declined:"),
}


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_at8_every_route_carries_exactly_one_record(tmp_path: Path, route: str) -> None:
    extra, mode, expect = ROUTES[route]
    config, sources = _mixed(tmp_path, extra)
    result = run_mixed(config, sources, mode=mode)
    execution = result.quality_metrics["execution"]
    taken = execution["route_reason"] if mode == "full_frame" else execution["execution_mode"]
    if route == "mixed-runtime-decline":
        taken = execution["route_reason"]
        assert taken.startswith(expect), taken
    else:
        assert expect in taken or taken == expect, taken
    mine = _records(list(result.warnings))
    assert len(mine) == 1, route
    assert mine[0].detail["rows"] > 0
    assert mine[0].detail["rows_total"] == 40
    _scan([repr(mine[0].detail)])


def test_at8_pure_generate_run_pipeline_carries_one_record(default: str) -> None:
    from decoy_engine import run_pipeline

    result = run_pipeline(_config(default), None, engine_version="test")
    assert len(_records(list(result.warnings))) == 1


def test_at12_pinned_execution_neither_crashes_nor_changes_bytes(default: str) -> None:
    from decoy_engine.generation.pinned import compile_pinned_plan_with_generation, execute_pinned

    ref = datetime(2026, 10, 22, 9, 30, 0, tzinfo=timezone.utc)
    result = compile_pinned_plan_with_generation(_config(default), ref)
    assert _digest(execute_pinned(result.pinned, 17)) == _DIGEST_PINNED
