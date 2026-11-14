"""The engine-set isolated-worker marker is read through the one flag parser."""

from __future__ import annotations

import pytest

from decoy_engine.errors import EnvFlagError
from decoy_engine.execution import run_pipeline_isolated
from tests.unit.execution.test_isolated_run import (
    _ENGINE_VERSION,
    _mask_config,
    _mask_sources,
)

_ENV = "DECOY_INSIDE_ISOLATED_WORKER"


def _no_spawn(*args: object, **kwargs: object) -> None:
    raise AssertionError("a spawn happened from inside an isolated worker")


@pytest.mark.parametrize("raw", ["1", "yes", "TRUE", "on"])
def test_every_accepted_true_spelling_counts_as_inside_a_worker(
    tmp_path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(_ENV, raw)
    monkeypatch.setattr("decoy_engine.execution._isolated_run.subprocess.Popen", _no_spawn)
    cfg = _mask_config(tmp_path, n_cols=1)
    sources = _mask_sources(tmp_path, n_rows=10, n_cols=1)
    result = run_pipeline_isolated(cfg, sources, engine_version=_ENGINE_VERSION, isolate=True)
    assert result.isolated is False
    assert result.outcome == "completed"


def test_an_unknown_marker_spelling_is_refused_before_any_spawn(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "maybe")
    monkeypatch.setattr("decoy_engine.execution._isolated_run.subprocess.Popen", _no_spawn)
    cfg = _mask_config(tmp_path, n_cols=1)
    sources = _mask_sources(tmp_path, n_rows=10, n_cols=1)
    with pytest.raises(EnvFlagError) as caught:
        run_pipeline_isolated(cfg, sources, engine_version=_ENGINE_VERSION, isolate=True)
    assert caught.value.variable == _ENV


def test_isolate_false_never_reads_the_marker(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV, "maybe")
    cfg = _mask_config(tmp_path, n_cols=1)
    sources = _mask_sources(tmp_path, n_rows=10, n_cols=1)
    result = run_pipeline_isolated(cfg, sources, engine_version=_ENGINE_VERSION, isolate=False)
    assert result.outcome == "completed"
