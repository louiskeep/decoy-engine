"""Task 4.5 D9: FLAG-OFF INERTNESS.

Poisons every new unified-slice call site (the cheap admission check, the
dominating resident-contract check, and the full execution path) and proves
NONE fire when `unified_slice_enabled` defaults False, then proves -- in a
fresh subprocess, so a hidden dynamic import cannot hide from a source-text
sweep -- that `decoy_engine.execution.physical` is never imported on that
path either.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.execution import _unified_slice, _unified_slice_admission, run_pipeline
from tests.physical._shadow_helpers import build_config, write_read_only_fixture

ENGINE_VERSION = "unified-slice-inertness-test"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _admissible_config_and_source(tmp_path: Path) -> tuple[dict, pa.Table]:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "fixture")
    config = build_config(tmp_path, "t", path, [{"name": "c", "strategy": "passthrough"}])
    return config, source


def _run_flag_off(config: dict, source: pa.Table):
    return run_pipeline(
        config, {"t": source}, engine_version=ENGINE_VERSION, unified_slice_enabled=False
    )


def test_flag_off_never_calls_cheap_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, source = _admissible_config_and_source(tmp_path)

    def _poisoned(*args: object, **kwargs: object) -> object:
        raise AssertionError("cheap_admission must not run when the flag is off")

    monkeypatch.setattr(_unified_slice_admission, "cheap_admission", _poisoned)
    result = _run_flag_off(config, source)
    assert result.outputs["t"].column("c").to_pylist() == ["a", "b", "c"]


def test_flag_off_never_calls_execute_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, source = _admissible_config_and_source(tmp_path)

    def _poisoned(*args: object, **kwargs: object) -> object:
        raise AssertionError("_execute_admitted must not run when the flag is off")

    monkeypatch.setattr(_unified_slice, "_execute_admitted", _poisoned)
    _run_flag_off(config, source)


def test_flag_off_never_calls_resident_contract_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, source = _admissible_config_and_source(tmp_path)

    def _poisoned(*args: object, **kwargs: object) -> object:
        raise AssertionError("resident_contract_admission must not run when the flag is off")

    monkeypatch.setattr(_unified_slice_admission, "resident_contract_admission", _poisoned)
    _run_flag_off(config, source)


def test_flag_off_native_companion_probe_never_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one runtime host probe the admitted path calls
    (`native_companion_status`); a flag-off run must not touch it either."""
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    path = write_read_only_fixture(tmp_path, source, "fixture")
    config = build_config(
        tmp_path, "t", path, [{"name": "c", "strategy": "hash", "namespace": "n"}]
    )

    def _poisoned() -> object:
        raise AssertionError("native_companion_status must not be probed when the flag is off")

    monkeypatch.setattr(_unified_slice_admission, "native_companion_status", _poisoned)
    run_pipeline(config, {"t": source}, engine_version=ENGINE_VERSION, unified_slice_enabled=False)


def test_default_omitted_flag_is_also_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan's default-OFF contract: a caller that never passes
    `unified_slice_enabled` at all (the overwhelming majority of today's
    callers) gets the identical inertness proof as an explicit `False`."""
    config, source = _admissible_config_and_source(tmp_path)

    def _poisoned(*args: object, **kwargs: object) -> object:
        raise AssertionError("cheap_admission must not run when the kwarg is omitted")

    monkeypatch.setattr(_unified_slice_admission, "cheap_admission", _poisoned)
    run_pipeline(config, {"t": source}, engine_version=ENGINE_VERSION)


_FRESH_IMPORT_PROBE = """
import sys
import pyarrow as pa
import pyarrow.parquet as pq
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline

path = {path!r}
out_path = {out_path!r}
source = pa.table({{"c": pa.array(["a", "b", "c"], type=pa.string())}})
pq.write_table(source, path)
raw = {{
    "version": 1,
    "global_settings": {{"seed": 1}},
    "sources": {{"t": {{"type": "file", "format": "parquet", "path": path}}}},
    "targets": {{"t": {{"type": "file", "format": "parquet", "path": out_path}}}},
    "tables": [{{"name": "t", "columns": [{{"name": "c", "strategy": "passthrough"}}]}}],
}}
config = PipelineConfig.model_validate(raw).model_dump()
run_pipeline(config, {{"t": source}}, engine_version="probe", unified_slice_enabled=False)
hits = [name for name in sys.modules if name.startswith("decoy_engine.execution.physical")]
print(",".join(sorted(hits)))
"""


def test_flag_off_never_imports_execution_physical_in_a_fresh_process(tmp_path: Path) -> None:
    """The dynamic complement to the poison tests above: proves, by
    observing a fresh interpreter's `sys.modules` after a real flag-off
    `run_pipeline` call (on a shape the unified slice WOULD admit if the
    flag were on), that `execution.physical` was never reached at all --
    not merely that the functions this test file happens to know about
    were not called."""
    path = str(tmp_path / "src.parquet")
    out_path = str(tmp_path / "out.parquet")
    probe = _FRESH_IMPORT_PROBE.format(path=path, out_path=out_path)
    result = subprocess.run(  # noqa: S603 fixed test-local probe script, no untrusted input
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    hits = [name for name in result.stdout.strip().split(",") if name]
    assert not hits, (
        f"A flag-off run_pipeline call pulled execution.physical module(s) into sys.modules: {hits}"
    )


def test_run_from_pipeline_locals_flag_off_ignores_every_other_local() -> None:
    """`run_from_pipeline_locals` reads ONLY the stable `unified_slice_enabled`
    run_pipeline parameter on the flag-off path and returns before indexing any
    of the other forwarded locals. Passing a mapping that contains ONLY the
    flag proves a future rename of one of those locals cannot break a default
    (flag-off) customer run: if the helper indexed them, this would KeyError."""
    from decoy_engine.execution._unified_slice import run_from_pipeline_locals

    assert run_from_pipeline_locals({"unified_slice_enabled": False}) is None
    # A wholly empty mapping (`.get()` -> None -> falsy) is likewise inert.
    assert run_from_pipeline_locals({}) is None
