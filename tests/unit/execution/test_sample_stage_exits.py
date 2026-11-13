"""SAMPLE SE2: the kept-row ceiling (AT-14) and staging cleanup on every exit (AT-15)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import decoy_engine.subset as subset_pkg
from decoy_engine import run_pipeline
from decoy_engine.execution.pipeline import _subset_stage as stage_mod
from decoy_engine.subset import SubsetError, SubsetInternalError, sample_request
from tests.unit.execution import _sample_run_support as sup


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sup.VAR, "ab" * 32)


class _RootSpy:
    """Records the scratch root; a plain class so nothing is cleaned up on garbage collection."""

    def __init__(self, real: Any, seen: list[Path], held: list[Any]) -> None:
        self._real = real
        self._seen = seen
        held.append(real)

    def __enter__(self) -> Path:
        root: Path = self._real.__enter__()
        self._seen.append(root)
        return root

    def __exit__(self, *exc: Any) -> Any:
        return self._real.__exit__(*exc)


@pytest.fixture
def roots(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    seen: list[Path] = []
    held: list[Any] = []
    real = stage_mod.engine_scratch_root
    monkeypatch.setattr(stage_mod, "engine_scratch_root", lambda p: _RootSpy(real(p), seen, held))
    return seen


def _run(cfg: dict[str, Any], sources: Any) -> Any:
    request = sample_request(cfg, rows=5)
    return run_pipeline(cfg, sources, engine_version="t", subset_request=request)


def test_at14_a_closure_over_the_kept_row_ceiling_refuses(tmp_path: Path) -> None:
    ceiling = 1_000_000
    data = {
        "hub": pa.table({"hid": ["H0"]}),
        "spoke": pa.table(
            {
                "sid": pa.array(range(ceiling + 1)).cast(pa.string()),
                "hid": pa.array(["H0"] * (ceiling + 1)),
            }
        ),
    }
    cfg, _ = sup.build(tmp_path, data, [("hub", ("hid",), "spoke", ("hid",))])
    request = sample_request(cfg, rows=1, root="hub")
    with pytest.raises(SubsetError) as info:
        run_pipeline(cfg, data, engine_version="t", subset_request=request)
    assert info.value.code == "subset_budget_exceeded"


def test_at15_staging_is_removed_after_success(tmp_path: Path, roots: list[Path]) -> None:
    cfg, _, lazy = sup.chain_job(tmp_path)
    res = _run(cfg, lazy)
    assert res.outputs["notes"].num_rows == 5
    assert len(roots) == 1 and not roots[0].exists()


def test_at15_staging_is_removed_when_a_source_changes_between_passes(
    tmp_path: Path, roots: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, data, lazy = sup.chain_job(tmp_path)
    real = subset_pkg.select_subset

    def changed(*a: Any, **k: Any) -> Any:
        sel = real(*a, **k)
        cids = data["customers"].column("cid").to_pylist()
        cids[min(sel.survivors["customers"])] = "ZZ"
        new = pa.table({"cid": cids, "name": data["customers"].column("name")})
        pq.write_table(new, Path(cfg["sources"]["customers"]["path"]))
        return sel

    monkeypatch.setattr(subset_pkg, "select_subset", changed)
    with pytest.raises(Exception) as info:
        _run(cfg, lazy)
    assert getattr(info.value, "code", None) == "lazy_source_changed"
    assert len(roots) == 1 and not roots[0].exists()


def test_at15_staging_is_removed_when_the_staged_count_differs(
    tmp_path: Path, roots: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _, lazy = sup.chain_job(tmp_path)
    real = subset_pkg.select_subset

    def off_by_one(*a: Any, **k: Any) -> Any:
        sel = real(*a, **k)
        counts = {n: (s, kept + (n == "items")) for n, (s, kept) in sel.counts.items()}
        return type(sel)(
            survivors=sel.survivors,
            counts=counts,
            scope=sel.scope,
            excluded_edges=sel.excluded_edges,
            estimate=sel.estimate,
            materialization_check=sel.materialization_check,
        )

    monkeypatch.setattr(subset_pkg, "select_subset", off_by_one)
    with pytest.raises(SubsetInternalError):
        _run(cfg, lazy)
    assert len(roots) == 1 and not roots[0].exists()
