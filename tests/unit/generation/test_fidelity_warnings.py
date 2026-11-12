"""GEN-CLI E1: the fidelity gate emits structured warnings on every generation route.

AT-8 (the sink on the four generate entries and every execution route, bytes unchanged) and
AT-30 (one broken column cannot hide in a high overall score).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import reasons, run_pipeline
from decoy_engine.generation._fidelity_gate import score_generated_fidelity
from decoy_engine.generation._plan_entry import generate_tables_streamed
from decoy_engine.generation.statistical import load_spec
from decoy_engine.generation.synthesize import (
    generate_tables,
    generate_tables_from_generation_plan,
    generate_tables_streamed_from_generation_plan,
)
from decoy_engine.plan import compile_plan
from decoy_engine.quality.snapshot import compute_distribution_snapshot
from tests.parity._f2_mixed_support import CHILD, FK, child_table, mixed_config
from tests.parity._f2_mixed_support import run as run_mixed
from tests.unit._dps_helpers import empty_profile
from tests.unit._gen_cli_golden import FIXTURES, digests, stat_config

CODE = str(reasons.warning.GENERATION_FIDELITY_BELOW_THRESHOLD)
GOLDEN = json.loads((FIXTURES / "golden.json").read_text(encoding="utf-8"))


@pytest.fixture
def snapshot_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copy(FIXTURES / "snap.json", tmp_path / "snap.json")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _raised(records: list[Any]) -> list[Any]:
    return [r for r in records if r.code == CODE]


def _plan(threshold: float | None = None) -> Any:
    config = stat_config()
    if threshold is not None:
        config["global_settings"]["fidelity_warn_threshold"] = threshold
    return compile_plan(config, empty_profile(), decoy_engine_version="test")


def _entries(plan: Any, sink: Any) -> list[Any]:
    """Each of the four public generate entries, all handed the same sink."""
    return [
        lambda: generate_tables(plan, on_warning=sink),
        lambda: generate_tables_streamed(plan, sink=None, on_warning=sink).tables,
        lambda: generate_tables_from_generation_plan(plan.generation, on_warning=sink),
        lambda: (
            generate_tables_streamed_from_generation_plan(
                plan.generation, sink=None, on_warning=sink
            ).tables
        ),
    ]


def test_each_generate_entry_hands_the_warning_to_the_sink_and_keeps_the_bytes(
    snapshot_dir: Path,
) -> None:
    plan = _plan()
    for index in range(4):
        raised: list[Any] = []
        out = _entries(plan, raised.append)[index]()
        assert len(_raised(raised)) == 1, index
        assert digests(out) == GOLDEN["statistical"]["tables"], index
    record = _raised(raised)[0]
    assert record.provider == "statistical"
    assert record.column is None
    detail = record.detail
    assert detail["table"] == "synthetic"
    assert detail["snapshot_index"] == 0
    assert detail["threshold"] == 0.99
    assert detail["trigger"] in {"overall", "column"}
    assert detail["worst_columns"] and detail["worst_columns"][0][0] == "amount"
    assert set(detail) == {
        "table",
        "snapshot_index",
        "overall_score",
        "threshold",
        "trigger",
        "worst_columns",
        "below_columns",
    }
    json.dumps(detail)


def test_threshold_zero_raises_none_and_two_runs_raise_one_each(snapshot_dir: Path) -> None:
    quiet: list[Any] = []
    generate_tables(_plan(0.0), on_warning=quiet.append)
    assert quiet == []
    plan = _plan()
    first: list[Any] = []
    second: list[Any] = []
    generate_tables(plan, on_warning=first.append)
    generate_tables(plan, on_warning=second.append)
    assert len(first) == len(second) == 1


def test_no_sink_means_no_change_for_existing_callers(snapshot_dir: Path) -> None:
    assert digests(generate_tables(_plan())) == GOLDEN["statistical"]["tables"]


# Execution routes


def _generate_only_config(snapshot_dir: Path, threshold: float = 0.99) -> dict[str, Any]:
    config = stat_config()
    config["tables"][0]["generate_columns"][0]["snapshot_file"] = str(snapshot_dir / "snap.json")
    config["global_settings"]["fidelity_warn_threshold"] = threshold
    return config


def _mixed(snapshot_dir: Path, parent_extra: list[dict[str, Any]], threshold: float = 0.99) -> Any:
    child = child_table(pa.array([str(i % 5) for i in range(40)], pa.string()))
    amount = {
        "name": "amount",
        "type": "statistical",
        "snapshot_file": str(snapshot_dir / "snap.json"),
    }
    parent = [{"name": "id", "type": "sequence", "start": 0, "step": 1}, amount, *parent_extra]
    config = mixed_config(snapshot_dir, child, parent_columns=parent)
    config["global_settings"]["fidelity_warn_threshold"] = threshold
    return config, {CHILD: child}


def test_pure_generate_full_frame_carries_the_warning_into_the_result(
    snapshot_dir: Path,
) -> None:
    result = run_pipeline(_generate_only_config(snapshot_dir), None, engine_version="test")
    assert len(_raised(list(result.warnings))) == 1
    quiet = run_pipeline(_generate_only_config(snapshot_dir, 0.0), None, engine_version="test")
    assert _raised(list(quiet.warnings)) == []
    assert result.outputs["synthetic"].equals(quiet.outputs["synthetic"])


ROUTES = {
    "forced-full-frame": ({}, "full_frame", "override_full_frame"),
    "admitted-mixed": ({}, "auto", "mixed"),
    "mixed-runtime-decline": (
        {
            "extra": [
                {
                    "name": "tier",
                    "type": "categorical",
                    "categories": ["A", None],
                    "weights": [0, 1],
                }
            ]
        },
        "auto",
        "mixed_runtime_declined:",
    ),
}


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_every_mixed_route_reports_the_warning_once_and_keeps_the_bytes(
    snapshot_dir: Path, route: str
) -> None:
    spec, mode, expect = ROUTES[route]
    config, sources = _mixed(snapshot_dir, spec.get("extra", []))
    result = run_mixed(config, sources, mode=mode)
    execution = result.quality_metrics["execution"]
    taken = execution["route_reason"] if mode == "full_frame" else execution["execution_mode"]
    if route == "mixed-runtime-decline":
        taken = execution["route_reason"]
        assert taken.startswith(expect), taken
    else:
        assert expect in taken or taken == expect, taken
    assert len(_raised(list(result.warnings))) == 1, route
    quiet_config, quiet_sources = _mixed(snapshot_dir, spec.get("extra", []), threshold=0.0)
    quiet = run_mixed(quiet_config, quiet_sources, mode=mode)
    assert _raised(list(quiet.warnings)) == []
    assert result.outputs["people"].equals(quiet.outputs["people"])
    assert FK in result.outputs[CHILD].column_names


# AT-30


def _wide_frame(rows: int = 3000) -> pd.DataFrame:
    rng = np.random.default_rng(5)
    frame = {f"c{i}": rng.normal(50.0 + i, 5.0, size=rows).round(2) for i in range(9)}
    broken = rng.normal(40.0, 4.0, size=rows).round(2)
    broken[: int(rows * 0.9)] = np.nan
    frame["null90"] = broken
    return pd.DataFrame(frame)


def _score(frame: pd.DataFrame, generated: dict[str, list[Any]], threshold: float) -> list[Any]:
    snapshot = compute_distribution_snapshot(frame)
    cols = [{"name": c, "type": "statistical", "snapshot_file": "s.json"} for c in frame.columns]
    specs = {("t", c["name"]): load_spec(c, snapshot=snapshot) for c in cols}
    return score_generated_fidelity(
        cols,
        generated,
        table_name="t",
        threshold=threshold,
        statistical_specs=specs,
        snapshot_index_for_column={("t", c): 0 for c in frame.columns},
        snapshot_artifacts=[snapshot],
    )


def _faithful(frame: pd.DataFrame, *, break_column: bool) -> dict[str, list[Any]]:
    rng = np.random.default_rng(99)
    out: dict[str, list[Any]] = {}
    for name in frame.columns:
        values = frame[name].dropna().to_numpy()
        sample = rng.choice(values, size=len(frame))
        if name == "null90" and not break_column:
            sample = sample.astype(object)
            sample[: int(len(frame) * 0.9)] = None
        out[name] = sample.tolist()
    return out


def test_one_broken_column_warns_even_when_the_overall_score_passes() -> None:
    frame = _wide_frame()
    records = _score(frame, _faithful(frame, break_column=True), 0.8)
    assert len(records) == 1
    detail = records[0].detail
    assert detail["overall_score"] >= 0.8
    assert detail["trigger"] == "column"
    assert [name for name, _ in detail["below_columns"]] == ["null90"]
    assert detail["worst_columns"][0][0] == "null90"


def test_a_low_overall_score_triggers_on_overall_and_a_faithful_table_is_quiet() -> None:
    frame = _wide_frame()
    broken = _faithful(frame, break_column=True)
    assert _score(frame, broken, 0.99)[0].detail["trigger"] == "overall"
    assert _score(frame, _faithful(frame, break_column=False), 0.0) == []
    assert _score(frame, broken, 0.0) == []


def test_generation_bytes_do_not_depend_on_the_column_trigger(snapshot_dir: Path) -> None:
    plan = _plan(0.5)
    with_gate = generate_tables(plan)
    off = generate_tables(_plan(0.0))
    assert with_gate["synthetic"].equals(off["synthetic"])
    assert os.getcwd() == str(snapshot_dir)
