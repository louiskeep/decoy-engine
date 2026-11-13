"""GEN-TYPES E3: a lookup file is read once, pinned in the plan, and verified on load.

AT-10 (one open across compile and run), AT-11 (value-free refusals), AT-20 (no fingerprint disk
fallback, edited payload refused), AT-21 (save, delete the file, load, run).
Mutants named in the plan (run by hand, listed in the slice report): run re-reads the path
(AT-10), quote the cell in `lookup_weight_invalid` (AT-11), raise inside the handler scope
(AT-11 fd count), give `file` the disk fallback (AT-20), drop `lookups` from the load rebuild
(AT-21), leave PLAN_VERSION at 3 (version pin), drop the sha256 re-check from the helper (AT-20).
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any

import pytest
import yaml

from decoy_engine import reasons
from decoy_engine.fileaccess import FileRole
from decoy_engine.fileaccess._openat2 import openat2_usable
from decoy_engine.generation import _plan_entry
from decoy_engine.generation.kinds import _lookup as lookup_kind
from decoy_engine.generators.derivation import (
    GenDeriveContext,
    strategy_config_fingerprint,
    with_table,
)
from decoy_engine.plan import PlanCompileError, compile_plan
from decoy_engine.plan._compile import PLAN_VERSION
from decoy_engine.plan._serialize import plan_from_yaml, plan_to_yaml
from decoy_engine.plan.checks import _lookup_tables
from tests.fileaccess.support import RecordingFileAccess, RootRelative
from tests.unit._dps_helpers import empty_profile
from tests.unit.generation.kinds._support import cfg, one, table

SENTINEL = "SENTINEL-4f1c9a"
ORIGINAL = "v,w\nred,5\ngreen,3\nblue,2\n"
CHANGED = "v,w\nzzz,1\n"


def _lookup(**extra: Any) -> dict[str, Any]:
    return {"name": "x", "type": "lookup", **extra}


def _compile(config: dict[str, Any], **kwargs: Any) -> Any:
    return compile_plan(config, empty_profile(), decoy_engine_version="test", **kwargs)


def _column(out: dict[str, Any]) -> list[Any]:
    return out["t"].column("x").to_pylist()


def _opens(policy: RecordingFileAccess) -> int:
    return sum(1 for role, access, _k, _o in policy.requests if role is FileRole.LOOKUP)


def _rooted(tmp_path: Path) -> RootRelative:
    if not openat2_usable():
        pytest.skip("rooted file access needs Linux openat2")
    return RootRelative(tmp_path)


@pytest.fixture
def world(tmp_path: Path):  # type: ignore[no-untyped-def]
    policy = RecordingFileAccess(_rooted(tmp_path))
    yield tmp_path, policy
    policy.inner.close()  # type: ignore[attr-defined]


def _write(tmp_path: Path, text: str | bytes, name: str = "list.csv") -> Path:
    path = tmp_path / name
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return path


def test_the_plan_version_is_four() -> None:
    assert PLAN_VERSION == 4


# AT-10 ---------------------------------------------------------------------------------


def test_a_file_changed_after_compile_does_not_change_the_run_and_is_opened_once(world) -> None:  # type: ignore[no-untyped-def]
    tmp_path, policy = world
    path = _write(tmp_path, ORIGINAL)
    config = one(_lookup(file=str(path), weight_column="w"), rows=50)
    expected = _column(_plan_entry.generate_tables(_compile(config)))
    before = _opens(policy)

    plan = _compile(config, file_access=policy)
    path.write_bytes(CHANGED.encode("utf-8"))
    out = _plan_entry.generate_tables(plan, file_access=policy)

    assert _column(out) == expected
    assert "zzz" not in _column(out)
    assert _opens(policy) - before == 1


def test_two_columns_on_one_file_open_it_once(world) -> None:  # type: ignore[no-untyped-def]
    tmp_path, policy = world
    path = _write(tmp_path, ORIGINAL)
    config = cfg(
        [
            table("t", 5, [_lookup(file=str(path)), {**_lookup(file=str(path)), "name": "y"}]),
            table("u", 5, [_lookup(file=str(path))]),
        ]
    )
    plan = _compile(config, file_access=policy)
    _plan_entry.generate_tables(plan, file_access=policy)
    assert _opens(policy) == 1


# AT-11 ---------------------------------------------------------------------------------


def _hostile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, dict[str, Any], str]]:
    """(file text or bytes name, column extras, expected code) for each refusal."""
    _write(tmp_path, f"v,w\n{SENTINEL},{SENTINEL}\n", "bad_weight.csv")
    _write(tmp_path, f"v\n{SENTINEL}\n", "no_column.csv")
    _write(tmp_path, f"v\n{SENTINEL}\n".encode() + b"\xff\xfe\n", "not_utf8.csv")
    _write(tmp_path, f"v\n{SENTINEL}\n", "list.txt")
    _write(tmp_path, f"v\n{SENTINEL}\n" * 40, "big.csv")
    _write(tmp_path, f"v\n{SENTINEL}\n{SENTINEL}\n{SENTINEL}\n", "many.csv")
    _write(tmp_path, " \n\t\n", "blank.csv")
    return [
        ("bad_weight.csv", {"weight_column": "w"}, "lookup_weight_invalid"),
        ("no_column.csv", {"value_column": "absent"}, "lookup_column_missing"),
        ("not_utf8.csv", {}, "lookup_file_unreadable"),
        ("list.txt", {}, "lookup_file_format_unsupported"),
        ("big.csv", {}, "lookup_file_too_large"),
        ("many.csv", {}, "lookup_too_many_entries"),
        ("blank.csv", {}, "lookup_file_empty"),
    ]


def _texts(exc: BaseException, caplog: pytest.LogCaptureFixture) -> list[str]:
    out = [str(exc), repr(exc), "".join(traceback.format_exception(exc)), repr(exc.args)]
    logging.getLogger("hostile").error("failed", exc_info=exc)
    out.extend(logging.Formatter().format(r) for r in caplog.records)
    return out


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_every_hostile_file_is_refused_with_its_code_and_no_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cases = _hostile(tmp_path, monkeypatch)
    monkeypatch.setattr(_lookup_tables, "EMBEDDED_SNAPSHOT_MAX_BYTES", 100)
    monkeypatch.setattr(lookup_kind, "MAX_LOOKUP_ENTRIES", 2)
    policy = _rooted(tmp_path)
    try:
        for name, extra, code in cases:
            config = one(_lookup(file=name, **extra))
            fds = _open_fds() if sys.platform.startswith("linux") else None
            caplog.clear()
            with pytest.raises(PlanCompileError) as exc:
                _compile(config, file_access=policy, base_dir=tmp_path)
            assert exc.value.code == code, name
            assert exc.value.__cause__ is None and exc.value.__context__ is None, name
            for text in _texts(exc.value, caplog):
                assert SENTINEL not in text, name
                assert str(tmp_path) not in text, name
            if fds is not None:
                assert _open_fds() == fds, name
    finally:
        policy.close()


def test_the_codes_are_the_registered_lookup_codes() -> None:
    for name in (
        "LOOKUP_NOT_PINNED",
        "LOOKUP_PINNED_DIGEST_MISMATCH",
        "LOOKUP_FILE_UNREADABLE",
        "LOOKUP_FILE_FORMAT_UNSUPPORTED",
        "LOOKUP_FILE_EMPTY",
        "LOOKUP_FILE_TOO_LARGE",
        "LOOKUP_COLUMN_MISSING",
        "LOOKUP_TOO_MANY_ENTRIES",
        "LOOKUP_WEIGHT_INVALID",
        "PINNED_LOOKUP_UNSUPPORTED",
    ):
        assert str(getattr(reasons.plan, name)) == name.lower()


# AT-20 ---------------------------------------------------------------------------------


def test_a_lookup_fingerprint_without_a_digest_raises_and_opens_nothing(world) -> None:  # type: ignore[no-untyped-def]
    tmp_path, policy = world
    path = _write(tmp_path, ORIGINAL)
    col = _lookup(file=str(path))
    with pytest.raises(RuntimeError):
        strategy_config_fingerprint(col)
    with pytest.raises(RuntimeError):
        GenDeriveContext.for_column(
            derive_key=None, column_config=with_table(col, "t"), fallback_seed=1
        )
    assert policy.requests == []


def test_a_digest_for_another_type_is_not_inherited_by_a_lookup_file() -> None:
    col = _lookup(file="a.csv")
    with pytest.raises(RuntimeError):
        strategy_config_fingerprint(col, content_digests={("statistical", "snapshot_file"): "d"})
    with pytest.raises(RuntimeError):
        strategy_config_fingerprint(col, content_digests={("reference", "snapshot_file"): "d"})
    assert strategy_config_fingerprint(col, content_digests={("lookup", "file"): "d"})


def _saved(tmp_path: Path, **extra: Any) -> tuple[str, list[Any]]:
    path = _write(tmp_path, ORIGINAL)
    config = one(_lookup(file=str(path), **extra), rows=40)
    plan = _compile(config)
    return plan_to_yaml(plan), _column(_plan_entry.generate_tables(plan))


def _edit(text: str, change: Any) -> str:
    data = yaml.safe_load(text)
    change(data)
    return yaml.safe_dump(data, sort_keys=False)


def test_an_edited_payload_is_refused_on_load(tmp_path: Path) -> None:
    text, _ = _saved(tmp_path)

    def swap(data: dict[str, Any]) -> None:
        entry = data["generation"]["lookups"][0]
        entry["payload_b64"] = base64.b64encode(CHANGED.encode()).decode()

    with pytest.raises(PlanCompileError) as exc:
        plan_from_yaml(_edit(text, swap))
    assert exc.value.code == "lookup_pinned_digest_mismatch"
    assert "zzz" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)


def test_a_snapshot_style_edit_still_hits_the_snapshot_check() -> None:
    from decoy_engine.plan._generation import verified_pinned_bytes

    good = base64.b64encode(b"abc").decode()
    import hashlib

    digest = hashlib.sha256(b"abc").hexdigest()
    assert verified_pinned_bytes({"payload_b64": good, "sha256": digest}) == b"abc"
    assert verified_pinned_bytes({"payload_b64": good, "sha256": "0" * 64}) is None
    assert verified_pinned_bytes({"payload_b64": "!!!", "sha256": digest}) is None


# AT-21 ---------------------------------------------------------------------------------


def test_a_saved_plan_loads_and_runs_with_the_file_deleted(world) -> None:  # type: ignore[no-untyped-def]
    tmp_path, policy = world
    path = _write(tmp_path, ORIGINAL)
    config = one(_lookup(file=str(path), weight_column="w"), rows=40)
    plan = _compile(config, file_access=policy)
    direct = _column(_plan_entry.generate_tables(plan, file_access=policy))
    text = plan_to_yaml(plan)
    assert "lookups" in yaml.safe_load(text)["generation"]
    path.unlink()
    opened = _opens(policy)

    loaded = plan_from_yaml(text, file_access=policy)
    out = _column(_plan_entry.generate_tables(loaded, file_access=policy))

    assert out == direct
    assert _opens(policy) == opened == 1


def test_a_manifest_without_its_lookup_entry_is_refused(tmp_path: Path) -> None:
    text, _ = _saved(tmp_path)

    def drop(data: dict[str, Any]) -> None:
        del data["generation"]["lookups"]

    with pytest.raises(PlanCompileError) as exc:
        plan_from_yaml(_edit(text, drop))
    assert exc.value.code == "lookup_not_pinned"


def test_an_edited_value_column_is_checked_against_the_pinned_bytes(tmp_path: Path) -> None:
    text, _ = _saved(tmp_path, value_column="v")

    def retarget(data: dict[str, Any]) -> None:
        config = json.loads(data["generation"]["config_json"])
        for t in config["tables"]:
            for col in t["generate_columns"]:
                col["value_column"] = "missing"
        data["generation"]["config_json"] = json.dumps(config)

    with pytest.raises(PlanCompileError) as exc:
        plan_from_yaml(_edit(text, retarget))
    assert exc.value.code == "lookup_column_missing"


def test_a_plan_built_without_its_lookups_is_refused_at_the_run(tmp_path: Path) -> None:
    path = _write(tmp_path, ORIGINAL)
    plan = _compile(one(_lookup(file=str(path))))
    stripped = dataclasses.replace(
        plan, generation=dataclasses.replace(plan.generation, lookups=())
    )
    with pytest.raises(PlanCompileError) as exc:
        _plan_entry.generate_tables(stripped)
    assert exc.value.code == "lookup_not_pinned"


def test_a_plan_with_no_lookup_has_no_lookups_key(tmp_path: Path) -> None:
    config = one({"name": "x", "type": "categorical", "categories": ["a", "b"]})
    data = yaml.safe_load(plan_to_yaml(_compile(config)))
    assert "lookups" not in data["generation"]
    assert data["plan_version"] == 4
