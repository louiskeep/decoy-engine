"""GEN-CLI E1: a relative path in a pipeline YAML resolves from the config's folder.

AT-1 (engine half), AT-2, AT-3, AT-4, AT-6, AT-7 (engine half), AT-24, AT-27, AT-31, AT-32.
The checked-in golden holds the config hash captured on engine 86a28222 (`_gen_cli_golden`).
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import fileaccess
from decoy_engine.execution import run_pipeline_isolated
from decoy_engine.execution._pipeline_context import with_quarantine_path
from decoy_engine.execution.capacity import _resolved_config_copy
from decoy_engine.execution.physical import capture_physical_plan_inputs
from decoy_engine.fileaccess import FileRole, Root, RootedFileAccess
from decoy_engine.fileaccess._openat2 import openat2_usable
from decoy_engine.generation.pinned import (
    compile_pinned_plan,
    compile_pinned_plan_with_generation,
    fingerprint,
)
from decoy_engine.generation.statistical import _spec
from decoy_engine.plan import PlanCompileError, compile_plan, run_config_only_checks
from decoy_engine.plan._compile import _hash_config
from decoy_engine.plan.validate import validate_plan
from tests.unit._dps_helpers import empty_profile
from tests.unit._gen_cli_golden import FIXTURES, graph_config, stat_config

REF = datetime(2026, 10, 22, 9, 30, 0, tzinfo=timezone.utc)
GOLDEN = json.loads((FIXTURES / "golden.json").read_text(encoding="utf-8"))
UNREADABLE = "statistical_snapshot_unreadable"


@pytest.fixture
def folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Config folder A holding `snap.json`, and a working directory B that does not."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    shutil.copy(FIXTURES / "snap.json", a / "snap.json")
    monkeypatch.chdir(b)
    return a, b


def _compile(config: dict[str, Any], **kwargs: Any) -> Any:
    return compile_plan(config, empty_profile(), decoy_engine_version="test", **kwargs)


def _error(config: dict[str, Any], **kwargs: Any) -> tuple[str, str]:
    with pytest.raises(Exception) as exc:
        _compile(config, **kwargs)
    return exc.value.code, str(exc.value)


# AT-3


def test_config_relative_joins_lexically_and_keeps_absolute_and_none(tmp_path: Path) -> None:
    assert fileaccess.config_relative("snap.json", tmp_path) == str(tmp_path / "snap.json")
    assert fileaccess.config_relative("snap.json", str(tmp_path)) == str(tmp_path / "snap.json")
    assert fileaccess.config_relative("../x/snap.json", tmp_path / "a") == str(
        tmp_path / "x" / "snap.json"
    )
    absolute = str(tmp_path / "elsewhere" / "snap.json")
    assert fileaccess.config_relative(absolute, tmp_path / "a") == absolute
    relative = "sub/snap.json"
    assert fileaccess.config_relative(relative, None) is relative


def test_a_relative_base_dir_means_the_working_directory(
    folders: tuple[Path, Path],
) -> None:
    _, b = folders
    assert fileaccess.config_relative("snap.json", ".") == str(b / "snap.json")


# AT-1 (engine half), AT-2


def test_without_base_dir_a_relative_snapshot_reads_from_the_working_directory(
    folders: tuple[Path, Path],
) -> None:
    code, _ = _error(stat_config())
    assert code == UNREADABLE


def test_base_dir_reads_the_snapshot_from_the_config_folder_on_every_compile_route(
    folders: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = folders
    config = stat_config()
    reads: list[str] = []
    real = fileaccess.read_all

    def spy(role: Any, locator: str, *args: Any, **kwargs: Any) -> bytes:
        if role == FileRole.SNAPSHOT:
            reads.append(locator)
        return real(role, locator, *args, **kwargs)

    monkeypatch.setattr(fileaccess, "read_all", spy)
    expected = str(a / "snap.json")
    for run in (
        lambda: _compile(config, base_dir=a),
        lambda: _compile(config, base_dir=str(a)),
        lambda: run_config_only_checks(config, base_dir=a),
        lambda: (
            validate_plan(config, empty_profile(), decoy_engine_version="test", base_dir=a).ok
            or pytest.fail("validate_plan refused")
        ),
    ):
        reads.clear()
        run()
        assert reads == [expected]


def test_the_config_hash_and_plan_bytes_do_not_see_the_folder(
    folders: tuple[Path, Path],
) -> None:
    a, b = folders
    config = stat_config()
    before = copy.deepcopy(config)
    from_other_folder = _compile(config, base_dir=a)
    os.chdir(a)
    from_own_folder = _compile(config)
    assert config == before
    assert from_other_folder.pipeline_config_hash == from_own_folder.pipeline_config_hash
    assert _hash_config(config) == GOLDEN["statistical"]["config_hash"]
    assert from_other_folder.generation == from_own_folder.generation
    assert [s.sha256 for s in from_other_folder.generation.snapshots] == [
        s.sha256 for s in from_own_folder.generation.snapshots
    ]
    os.chdir(b)


# AT-4


def test_no_base_dir_hands_the_declared_locator_to_the_policy_unchanged(
    folders: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real = fileaccess.read_all

    def spy(role: Any, locator: str, *args: Any, **kwargs: Any) -> bytes:
        seen.append(locator)
        return real(role, locator, *args, **kwargs)

    monkeypatch.setattr(fileaccess, "read_all", spy)
    _error(stat_config())
    assert seen == ["snap.json"]


def test_a_rooted_policy_still_reads_a_root_relative_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not openat2_usable():
        pytest.skip("rooted file access needs Linux openat2")
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "snap.json", root / "snap.json")
    monkeypatch.chdir(tmp_path)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        roots = [Root("main", fd, frozenset({FileRole.SNAPSHOT}), frozenset({"read", "stat"}))]
        plan = _compile(stat_config(), file_access=RootedFileAccess(roots))
    finally:
        os.close(fd)
    assert plan.generation is not None


# AT-6


def test_an_unreadable_relative_snapshot_names_the_path_tried_and_the_rule(
    folders: tuple[Path, Path],
) -> None:
    a, _ = folders
    (a / "snap.json").unlink()
    code, message = _error(stat_config(), base_dir=a)
    assert code == UNREADABLE
    assert "'snap.json'" in message
    assert str(a / "snap.json") in message
    assert "config file's folder" in message


def test_an_absolute_snapshot_keeps_the_plain_message(folders: tuple[Path, Path]) -> None:
    a, _ = folders
    config = stat_config()
    absolute = str(a / "missing.json")
    config["tables"][0]["generate_columns"][0]["snapshot_file"] = absolute
    _, message = _error(config, base_dir=a)
    assert "config file's folder" not in message


# AT-7 (engine half)


@dataclasses.dataclass
class _Ctx:
    config: dict[str, Any]


def test_quarantine_output_path_resolves_from_base_dir_and_an_argument_wins(
    tmp_path: Path,
) -> None:
    ctx = _Ctx({"quarantine": {"enabled": True, "output_path": "q/rows.jsonl"}})
    resolved = with_quarantine_path(ctx, None, tmp_path)  # type: ignore[arg-type]
    assert resolved.config["quarantine"]["output_path"] == str(tmp_path / "q" / "rows.jsonl")
    assert ctx.config["quarantine"]["output_path"] == "q/rows.jsonl"
    given = with_quarantine_path(ctx, "mine.jsonl", tmp_path)  # type: ignore[arg-type]
    assert given.config["quarantine"]["output_path"] == "mine.jsonl"
    assert with_quarantine_path(ctx, None, None) is ctx  # type: ignore[arg-type]
    absolute = _Ctx({"quarantine": {"output_path": str(tmp_path / "abs.jsonl")}})
    assert with_quarantine_path(absolute, None, tmp_path) is absolute  # type: ignore[arg-type]
    assert with_quarantine_path(_Ctx({}), None, tmp_path).config == {}  # type: ignore[arg-type]


def test_the_capacity_copy_resolves_every_file_source_path_field(tmp_path: Path) -> None:
    config = {
        "sources": {
            "s": {
                "type": "file",
                "format": "csv",
                "path": "in/data.csv",
                "column_names_file": "in/names.csv",
            }
        }
    }
    copy_ = _resolved_config_copy(config, tmp_path)
    assert copy_["sources"]["s"]["path"] == str(tmp_path / "in" / "data.csv")
    assert copy_["sources"]["s"]["column_names_file"] == str(tmp_path / "in" / "names.csv")
    assert config["sources"]["s"]["path"] == "in/data.csv"


# AT-31


def test_both_pinned_compiles_take_base_dir_and_the_fingerprint_ignores_it(
    folders: tuple[Path, Path],
) -> None:
    a, _ = folders
    config = stat_config()
    config["global_settings"]["reference_date"] = "2026-10-22T09:30:00Z"
    wrapper = compile_pinned_plan(config, REF, base_dir=a)
    with_generation = compile_pinned_plan_with_generation(config, REF, base_dir=a)
    assert fingerprint(wrapper) == fingerprint(with_generation.pinned)
    os.chdir(a)
    assert fingerprint(compile_pinned_plan(config, REF)) == fingerprint(wrapper)
    os.chdir(folders[1])
    with pytest.raises(Exception) as exc:
        compile_pinned_plan(config, REF)
    assert exc.value.code == UNREADABLE


# AT-27


def test_physical_capture_reads_the_snapshot_from_base_dir(folders: tuple[Path, Path]) -> None:
    a, _ = folders
    config = stat_config()
    inputs = capture_physical_plan_inputs(config, None, engine_version="test", base_dir=a)
    assert inputs is not None
    with pytest.raises(Exception) as exc:
        capture_physical_plan_inputs(config, None, engine_version="test")
    assert exc.value.code == UNREADABLE


# AT-24


@pytest.mark.serial
def test_an_isolated_run_carries_base_dir_to_the_child(folders: tuple[Path, Path]) -> None:
    a, _ = folders
    config = stat_config()
    result = run_pipeline_isolated(config, None, engine_version="test", base_dir=str(a))
    assert result.outcome == "completed", result.error
    assert result.outputs is not None
    assert result.outputs["synthetic"].num_rows == 300
    os.chdir(a)
    in_process = run_pipeline_isolated(config, None, isolate=False, engine_version="test")
    os.chdir(folders[1])
    assert in_process.outputs is not None
    assert result.outputs["synthetic"].equals(in_process.outputs["synthetic"])
    with pytest.raises(ValueError, match="JSON"):
        run_pipeline_isolated(config, None, engine_version="test", base_dir=a)


# AT-32


def _bad_snapshot(folder: Path, kind: str) -> None:
    doc = json.loads((FIXTURES / "snap.json").read_text(encoding="utf-8"))
    if kind == "schema":
        doc["schema_version"] = "distribution-snapshot/v0"
    if kind == "missing":
        (folder / "snap.json").unlink()
        return
    (folder / "snap.json").write_text(json.dumps(doc), encoding="utf-8")


def _fitted_config() -> dict[str, Any]:
    config = graph_config()
    child = config["tables"][2]
    child["generate_columns"][1].update(distribution="fitted", snapshot_file="snap.json")
    return config


@pytest.mark.parametrize("kind", ["missing", "schema", "unknown-column", "fitted-no-fanout"])
def test_the_folded_pass_keeps_both_validations_on_both_compile_routes(
    folders: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    a, _ = folders
    config = stat_config()
    if kind == "unknown-column":
        config["tables"][0]["generate_columns"][0]["source_column"] = "nope"
    elif kind == "fitted-no-fanout":
        config = _fitted_config()
    else:
        _bad_snapshot(a, kind)
    calls: list[str] = []
    monkeypatch.setattr(
        _spec, "_load_snapshot", lambda *args, **kw: calls.append("load") or pytest.fail("reopened")
    )
    via_compile = _error(config, base_dir=a)
    with pytest.raises(PlanCompileError) as exc:
        run_config_only_checks(config, base_dir=a)
    via_config_only = (exc.value.code, str(exc.value))
    assert via_compile[0] == via_config_only[0]
    assert via_compile[0] != "", kind
    assert calls == []


def test_a_passing_config_yields_pins_specs_and_the_dp_receipt(
    folders: tuple[Path, Path],
) -> None:
    a, _ = folders
    plan = _compile(stat_config(), base_dir=a)
    assert plan.generation is not None
    assert len(plan.generation.snapshots) == 1
    assert [s.column_name for s in plan.generation.statistical_specs] == ["amount"]
    assert plan.generation.dp_verification is None


# AT-5 (engine path-choke guard)


def test_every_engine_path_rewrite_goes_through_the_one_helper() -> None:
    import inspect

    from decoy_engine.execution import _pipeline_context as context
    from decoy_engine.execution import capacity
    from decoy_engine.plan import _generation

    assert "config_relative" in inspect.getsource(_generation.read_and_pin_snapshots)
    assert "config_relative" in inspect.getsource(context.with_quarantine_path)
    assert "config_relative" in inspect.getsource(capacity._resolved_config_copy)
    assert not hasattr(capacity, "_resolve_source_path")


# DUP-DEFECTS E1 (A5): a snapshot with a byte that is not UTF-8 is the same refusal as a truncated one


def _bad_snapshot_config(tmp_path: Path, content: bytes) -> dict[str, Any]:
    (tmp_path / "snap.json").write_bytes(content)
    config = stat_config()
    config["tables"][0]["generate_columns"][0]["snapshot_file"] = str(tmp_path / "snap.json")
    return config


def test_at_e1_2_compile_wraps_a_non_utf8_snapshot_like_a_truncated_one(tmp_path: Path) -> None:
    config = _bad_snapshot_config(tmp_path, b'{"a": "\xff"}')
    with pytest.raises(PlanCompileError) as caught:
        _compile(config)
    code, text = caught.value.code, str(caught.value)
    assert code == UNREADABLE
    assert "snap.json" in text
    assert not [leak for leak in ("\xff", "0xff", "\u00ff", "position", "codec") if leak in text]


def test_at_e1_5_compile_truncated_snapshot_unchanged(tmp_path: Path) -> None:
    code, text = _error(_bad_snapshot_config(tmp_path, b'{"a": '))
    assert code == UNREADABLE
    assert "could not be read: Expecting value" in text


def test_at_e1_6_load_snapshot_second_site_raises_the_same_code(tmp_path: Path) -> None:
    config = _bad_snapshot_config(tmp_path, b'{"a": "\xff"}')
    with pytest.raises(_spec.StatisticalSpecError) as caught:
        _spec._load_snapshot(config["tables"][0]["generate_columns"][0]["snapshot_file"])
    assert caught.value.code == UNREADABLE
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_at_e1_9_plan_entry_decode_of_a_pinned_payload_is_closed(tmp_path: Path) -> None:
    import base64

    from decoy_engine.generation._plan_entry import generate_tables

    plan = _compile(_bad_snapshot_config(tmp_path, (FIXTURES / "snap.json").read_bytes()))
    bad = base64.b64encode(b'{"a": "\xff"}').decode("ascii")
    snaps = [dataclasses.replace(s, payload_b64=bad) for s in plan.generation.snapshots]
    plan = dataclasses.replace(
        plan, generation=dataclasses.replace(plan.generation, snapshots=snaps)
    )
    with pytest.raises(_spec.StatisticalSpecError) as caught:
        generate_tables(plan)
    assert caught.value.code == UNREADABLE
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "\xff" not in str(caught.value)
