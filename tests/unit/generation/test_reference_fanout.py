"""Fitted reference fan-out: generation executes the pinned degree histogram.

Acceptance tests AT-1, AT-2, AT-5 and AT-7 of the FK fan-out plan, plus the four
failures found reviewing the compile-side slice: seed derivation reopened
`snapshot_file`, `fitted` fell through to the random fallback, a probe got
degrees [1, 2, 5] instead of [1, 1, 1, 2, 3], and deleting the snapshot after
compile raised.
"""

from __future__ import annotations

import builtins
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from decoy_engine import reasons
from decoy_engine.context import make_key_resolver
from decoy_engine.generation import synthesize
from decoy_engine.generation._admission import classify_plan
from decoy_engine.generation._plan_entry import generate_tables
from decoy_engine.generation._synthesize_derived import _reference
from decoy_engine.generation.statistical import StatisticalSpecError
from decoy_engine.generators.derivation import with_table
from decoy_engine.plan import compile_plan
from decoy_engine.plan._serialize import plan_from_yaml, plan_to_yaml
from decoy_engine.quality.snapshot import compute_distribution_snapshot
from tests.unit._dps_helpers import empty_profile

_MASTER = bytes(range(32))


def _source_frame(degrees: dict[int, int]) -> pd.DataFrame:
    """A child frame where `parents` keys each own `degree` rows."""
    keys: list[str] = []
    n = 0
    for degree, parents in sorted(degrees.items()):
        for _ in range(parents):
            keys.extend([f"key-{n:05d}"] * degree)
            n += 1
    return pd.DataFrame({"pid": keys})


def _write_snapshot(tmp_path: Path, degrees: dict[int, int], name: str = "snap.json") -> str:
    snap = compute_distribution_snapshot(_source_frame(degrees), fanout_columns=("pid",))
    path = tmp_path / name
    path.write_text(json.dumps(snap, sort_keys=True), encoding="utf-8")
    return str(path)


def _config(
    snapshot: str, *, parents: int, children: int, null_probability: float = 0.0, seed: int = 7
) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "name": "pid",
        "type": "reference",
        "reference_table": "par",
        "reference_column": "k",
        "distribution": "fitted",
        "snapshot_file": snapshot,
    }
    if null_probability:
        ref["null_probability"] = null_probability
    return {
        "global_settings": {"seed": seed},
        "tables": [
            {
                "name": "par",
                "row_count": parents,
                "generate_columns": [{"name": "k", "type": "sequence"}],
            },
            {"name": "child", "row_count": children, "generate_columns": [ref]},
        ],
    }


def _plan(cfg: dict[str, Any]) -> Any:
    return compile_plan(cfg, empty_profile(), decoy_engine_version="test")


def _run(plan: Any, label: str | None = None) -> tuple[list[Any], list[Any]]:
    key = make_key_resolver(_MASTER, label) if label else None
    out = generate_tables(plan, derive_key=key)
    return out["par"].column("k").to_pylist(), out["child"].column("pid").to_pylist()


def _degrees(parent_keys: list[Any], child: list[Any]) -> list[int]:
    counts = Counter(v for v in child if v is not None)
    return sorted((counts.get(k, 0) for k in parent_keys), reverse=True)


QA = {1: 5, 2: 1, 5: 1, 8: 1}


# ── AT-2: the QA repro ───────────────────────────────────────────────────────


def test_qa_repro_same_ratio_reproduces_source_degrees(tmp_path: Path) -> None:
    plan = _plan(_config(_write_snapshot(tmp_path, QA), parents=10, children=20))
    parents, child = _run(plan)
    assert _degrees(parents, child) == [8, 5, 2, 1, 1, 1, 1, 1, 0, 0]
    assert len(child) == 20
    assert set(child) <= set(parents)


def test_probe_degrees_are_the_fitted_histogram_not_the_random_fallback(tmp_path: Path) -> None:
    # Codex probe: expected [1, 1, 1, 2, 3], the random fallback gave [1, 2, 5].
    plan = _plan(_config(_write_snapshot(tmp_path, {1: 3, 2: 1, 3: 1}), parents=5, children=8))
    parents, child = _run(plan)
    assert _degrees(parents, child) == [3, 2, 1, 1, 1]


def test_qa_repro_scaled_up_keeps_max_and_active_parents(tmp_path: Path) -> None:
    plan = _plan(_config(_write_snapshot(tmp_path, QA), parents=30, children=60))
    parents, child = _run(plan)
    degrees = _degrees(parents, child)
    assert max(degrees) == 8
    assert sum(1 for d in degrees if d) == 24
    assert len(child) == 60
    assert set(child) <= set(parents)


def test_empty_parent_pool_with_child_rows_refuses(tmp_path: Path) -> None:
    spec_col = with_table(
        {
            "name": "pid",
            "type": "reference",
            "reference_table": "par",
            "reference_column": "k",
            "distribution": "fitted",
        },
        "child",
    )
    import pyarrow as pa

    from decoy_engine.generation.statistical import fanout_spec_from_snapshot

    snap = compute_distribution_snapshot(_source_frame(QA), fanout_columns=("pid",))
    spec = fanout_spec_from_snapshot(snap, "pid", digest="ab" * 32)
    pools = {"par": pa.table({"k": pa.array([], type=pa.string())})}
    with pytest.raises(StatisticalSpecError) as exc:
        _reference(spec_col, 5, 1, None, pools, fanout=spec)
    assert exc.value.code == reasons.plan.REFERENCE_PARENT_POOL_EMPTY
    assert _reference(spec_col, 0, 1, None, pools, fanout=spec) == []
    for dist in ("random", "sequential", "weighted"):
        assert _reference({**spec_col, "distribution": dist}, 3, 1, None, pools) == [None] * 3


def test_missing_spec_raises_instead_of_falling_back_to_random(tmp_path: Path) -> None:
    cfg = _config(_write_snapshot(tmp_path, QA), parents=10, children=20)
    with pytest.raises(StatisticalSpecError) as exc:
        synthesize._generate_tables_from_config(cfg)
    assert exc.value.code == reasons.plan.REFERENCE_FANOUT_MISSING


# ── AT-5: pinned bytes, read once ────────────────────────────────────────────


def test_deleting_the_snapshot_after_compile_changes_nothing(tmp_path: Path) -> None:
    path = _write_snapshot(tmp_path, QA)
    plan = _plan(_config(path, parents=10, children=20, null_probability=0.1))
    with_file = _run(plan, "label")
    Path(path).unlink()
    without_file = _run(plan, "label")
    assert without_file == with_file
    assert None in with_file[1]


def test_run_never_opens_the_snapshot_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_snapshot(tmp_path, QA)
    plan = _plan(_config(path, parents=10, children=20, null_probability=0.1))
    opened: list[str] = []
    real_open = builtins.open

    def spy(file: Any, *args: Any, **kwargs: Any) -> Any:
        if str(file) == path:
            opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(Path, "read_bytes", lambda self, *a, **k: opened.append(str(self)) or b"")
    _run(plan)
    assert opened == []


def test_changing_the_file_after_compile_does_not_change_output(tmp_path: Path) -> None:
    path = _write_snapshot(tmp_path, QA)
    plan = _plan(_config(path, parents=10, children=20, null_probability=0.1))
    before = _run(plan)
    _write_snapshot(tmp_path, {1: 2, 9: 4})
    assert _run(plan) == before


# ── AT-7: routes and determinism ─────────────────────────────────────────────


def test_streamed_admission_declines_fitted(tmp_path: Path) -> None:
    plan = _plan(_config(_write_snapshot(tmp_path, QA), parents=10, children=20))
    admission = classify_plan(json.loads(plan.generation.config_json))
    declined = {(a.table, a.column): a for a in admission.columns}[("child", "pid")]
    assert not declined.eligible
    assert declined.reason == "reference_fitted_fanout"
    assert not admission.eligible


def test_same_key_and_label_repeat_and_a_new_label_differs(tmp_path: Path) -> None:
    plan = _plan(_config(_write_snapshot(tmp_path, QA), parents=10, children=20))
    assert _run(plan, "a") == _run(plan, "a")
    assert _run(plan, "a")[1] != _run(plan, "b")[1]


def test_plan_yaml_round_trip_generates_the_same_bytes(tmp_path: Path) -> None:
    plan = _plan(_config(_write_snapshot(tmp_path, QA), parents=10, children=20))
    again = plan_from_yaml(plan_to_yaml(plan))
    assert _run(again, "a") == _run(plan, "a")


def test_pinned_compile_refuses_a_fitted_snapshot_column(tmp_path: Path) -> None:
    from decoy_engine.generation._pinned_compile import PinnedModeError, compile_pinned_plan

    cfg = _config(_write_snapshot(tmp_path, QA), parents=10, children=20)
    with pytest.raises(PinnedModeError) as exc:
        compile_pinned_plan(cfg, datetime(2026, 10, 22, 9, 30, tzinfo=timezone.utc))
    assert exc.value.code == reasons.plan.PINNED_SNAPSHOT_FILE_NOT_STATISTICAL


# ── AT-1: shape ──────────────────────────────────────────────────────────────


def _heavy_tail() -> dict[int, int]:
    # 1,000 parents: 200 with no children are invisible to the child file, so the
    # source here holds the 800 active ones; 10 of them are above 40.
    body = {1: 300, 2: 200, 3: 120, 4: 80, 6: 50, 9: 30, 14: 10}
    return {**body, 40: 6, 55: 4}


def _histogram(degrees: list[int]) -> Counter[int]:
    return Counter(degrees)


def test_shape_equals_source_at_the_source_ratio(tmp_path: Path) -> None:
    src = _heavy_tail()
    rows = sum(d * c for d, c in src.items())
    keys = sum(src.values())
    parents = keys + 200
    plan = _plan(_config(_write_snapshot(tmp_path, src), parents=parents, children=rows))
    pk, child = _run(plan)
    hist = _histogram(_degrees(pk, child))
    assert {d: c for d, c in hist.items() if d} == src
    assert hist[0] == 200


def _shape_score(src: dict[int, int], degrees: list[int]) -> float:
    """One minus the KS distance between the generated and source degree CDFs over active parents."""
    keys = sum(src.values())
    gen = sorted(d for d in degrees if d)
    ks = 0.0
    for d in sorted(set(src) | set(gen)):
        f_src = sum(c for k, c in src.items() if k <= d) / keys
        f_gen = sum(1 for g in gen if g <= d) / len(gen)
        ks = max(ks, abs(f_src - f_gen))
    return 1 - ks


def test_shape_scales_with_tolerance(tmp_path: Path) -> None:
    src = _heavy_tail()
    rows = sum(d * c for d, c in src.items())
    keys = sum(src.values())
    parents, children = int((keys + 200) * 1.5), int(rows * 1.5)
    cfg = _config(_write_snapshot(tmp_path, src), parents=parents, children=children)
    pk, child = _run(_plan(cfg))
    degrees = _degrees(pk, child)
    assert len(child) == children
    assert max(degrees) <= max(src)
    assert _shape_score(src, degrees) >= 0.95

    # Precondition: the same sizes with `random` score clearly lower (0.84 measured, a
    # long way under the 0.95 bar), so the score above is the fitted histogram's doing.
    ref = cfg["tables"][1]["generate_columns"][0]
    ref["distribution"] = "random"
    del ref["snapshot_file"]
    pk_r, child_r = _run(_plan(cfg))
    assert _shape_score(src, _degrees(pk_r, child_r)) < 0.9
