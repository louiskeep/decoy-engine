"""The B6a benchmark driver's resume and merge steps (`scripts/bench-auto-chunk/bench_sink.py`).

The oracle 10M cell is run separately, so the merge of saved cells with a new jsonl cell and
the evaluation of every frozen bar must be right without running the benchmark."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
SAVED = REPO / "docs" / "records" / "b6a-bench-2026-10-02" / "b6a-bench-3cells.json"


@pytest.fixture(scope="module")
def bench() -> Any:
    path = REPO / "scripts" / "bench-auto-chunk" / "bench_sink.py"
    spec = importlib.util.spec_from_file_location("bench_sink", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entries(rounds: int = 20, streamed_scale: float = 1.0) -> list[dict[str, Any]]:
    """An oracle 10M cell built from the saved oracle 2M trials (resident x5)."""
    cell = json.loads(SAVED.read_text())["cells"]["oracle_2000000"]
    out: list[dict[str, Any]] = []
    ref = copy.deepcopy(cell["trials"]["resident"][0])
    ref["rows"] = 10_000_000
    out.append(
        {
            "cell": "oracle_10000000",
            "kind": "reference",
            "mode": "resident",
            "round": None,
            "trial": ref,
        }
    )
    for mode in ("resident", "streamed"):
        for t in cell["trials"][mode][:rounds]:
            t = copy.deepcopy(t)
            t["rows"] = 10_000_000
            scale = 5.0 if mode == "resident" else streamed_scale
            t["increment_bytes"] = int(t["increment_bytes"] * scale)
            out.append(
                {
                    "cell": "oracle_10000000",
                    "kind": "measured",
                    "mode": mode,
                    "round": t["round"],
                    "trial": t,
                }
            )
    return out


def _merge(
    bench: Any, tmp_path: Path, entries: list[dict[str, Any]], *, meta_rounds: int | None = None
) -> tuple[int, dict[str, Any]]:
    (tmp_path / "results.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
    if meta_rounds is not None:
        (tmp_path / "meta.json").write_text(json.dumps({"rounds": meta_rounds}))
    args = argparse.Namespace(
        out_dir=str(tmp_path), saved=[str(SAVED)], merged_out=None, min_rounds=0
    )
    code = bench.merge(args)
    return code, json.loads((tmp_path / "merged.json").read_text())


def test_nearest_rank_percentiles(bench: Any) -> None:
    values = [float(v) for v in range(1, 21)]
    assert bench.nearest_rank(values, 0.5) == 10.0
    assert bench.nearest_rank(values, 0.95) == 19.0
    assert bench.summarize(values)["max"] == 20.0


def test_merge_with_a_passing_oracle_cell_evaluates_all_eight_bars(
    bench: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, merged = _merge(bench, tmp_path, _entries(streamed_scale=0.05))
    capsys.readouterr()
    bars = merged["bars"]
    assert set(merged["cells"]) == {
        "native_2000000",
        "native_10000000",
        "oracle_2000000",
        "oracle_10000000",
    }
    for key in ("W_native_2000000", "W_native_10000000", "W_oracle_2000000", "W_oracle_10000000"):
        assert key in bars
    for key in ("M1_native", "M2_native", "M1_oracle", "M2_oracle"):
        assert bars[key]["pass"] is True, key
    assert bars["all_pass"] is True and code == 0


def test_a_streamed_increment_that_does_not_shrink_fails_m1_and_the_exit_code(
    bench: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, merged = _merge(bench, tmp_path, _entries(streamed_scale=5.0))
    capsys.readouterr()
    assert merged["bars"]["M1_oracle"]["pass"] is False
    assert merged["bars"]["all_pass"] is False and code == 1


def test_an_incomplete_cell_is_not_merged_and_its_bars_stay_pending(
    bench: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, merged = _merge(bench, tmp_path, _entries(rounds=19))
    capsys.readouterr()
    assert "oracle_10000000" not in merged["cells"]
    assert merged["bars"]["M1_oracle"]["status"] == "pending"
    assert merged["bars"]["all_pass"] is False and code == 1


def _with_warmup(entries: list[dict[str, Any]], rounds: int) -> list[dict[str, Any]]:
    """A `--warmups 1 --rounds N` cell: a warmup row per mode, then rounds 0..N-1."""
    out = [e for e in entries if e["kind"] == "reference"]
    for mode in ("resident", "streamed"):
        warm = next(e for e in entries if e["mode"] == mode and e["kind"] == "measured")
        out.append({**copy.deepcopy(warm), "kind": "warmup", "round": 0})
    out += [e for e in entries if e["kind"] == "measured" and e["round"] < rounds]
    return out


def test_a_one_warmup_five_round_cell_is_complete_and_evaluated(
    bench: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    entries = _with_warmup(_entries(streamed_scale=0.05), 5)
    code, merged = _merge(bench, tmp_path, entries, meta_rounds=5)
    capsys.readouterr()
    cell = merged["cells"]["oracle_10000000"]
    assert len(cell["trials"]["resident"]) == len(cell["trials"]["streamed"]) == 5
    for key in ("W_oracle_10000000", "M1_oracle", "M2_oracle"):
        assert merged["bars"][key]["pass"] is True, key
    assert merged["bars"]["all_pass"] is True and code == 0


def test_five_rounds_without_a_recorded_reduced_run_are_still_rejected(
    bench: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    entries = _with_warmup(_entries(streamed_scale=0.05), 5)
    code, merged = _merge(bench, tmp_path, entries)
    capsys.readouterr()
    assert "oracle_10000000" not in merged["cells"] and code == 1
