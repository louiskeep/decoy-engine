"""AT-X3: classification survives every translating, typed, stored-error and fallback handler.

Each failure shape is injected at the adapter (or at the file call the handler wraps) under a
strict policy, at every inventoried handler. Expected:

| shape                                              | class and errno kept | memory failure | isolated outcome |
| MemoryError, ArrowMemoryError, DuckDB OOM, ENOMEM  | yes                  | True           | oom_killed       |
| single-token Arrow `Wrapping`, OpenSSL markers     | yes                  | True           | oom_killed       |
| ENOSPC, EDQUOT                                     | yes, errno kept      | False          | crashed          |
| whitespace-bearing Arrow `Wrapping` (negative)     | not required         | False          | crashed          |
"""

from __future__ import annotations

import errno
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import fileaccess
from decoy_engine.execution._isolated_common import is_memory_failure
from decoy_engine.fileaccess import FileRequest, FileRole, _rooted
from tests.fileaccess.x_support import XWorld

SHAPES: dict[str, tuple[Callable[[], Exception], bool, bool]] = {
    # name: (factory, class+errno must be kept, is_memory_failure)
    "memory": (lambda: MemoryError("out"), True, True),
    "arrow_memory": (lambda: pa.ArrowMemoryError("malloc of size 9 failed"), True, True),
    "duckdb_oom": (lambda: duckdb.OutOfMemoryException("Out of Memory Error: block"), True, True),
    "enomem": (lambda: OSError(errno.ENOMEM, "Cannot allocate memory"), True, True),
    "wrapping_token": (lambda: pa.ArrowInvalid("Unknown error: Wrapping cell1 failed"), True, True),
    "openssl_ctx": (lambda: ValueError("not able to copy ctx"), True, True),
    "openssl_env": (lambda: ValueError("digital envelope routines"), True, True),
    "enospc": (lambda: OSError(errno.ENOSPC, "No space left on device"), True, False),
    "edquot": (lambda: OSError(errno.EDQUOT, "Disk quota exceeded"), True, False),
    "wrapping_spaces": (
        lambda: pa.ArrowInvalid("Unknown error: Wrapping two words failed"),
        False,
        False,
    ),
}


def _boom(factory: Callable[[], Exception]) -> Callable[..., Any]:
    def raiser(*args: Any, **kwargs: Any) -> Any:
        raise factory()

    return raiser


def _parquet(world: XWorld, name: str, columns: dict[str, list[Any]]) -> Path:
    path = world.sub / name
    pq.write_table(pa.table(columns), path)
    return path


Site = Callable[[XWorld, pytest.MonkeyPatch, Callable[[], Exception]], Callable[[], Any]]


def _site_reference_table(world: XWorld, mp: pytest.MonkeyPatch, factory: Any) -> Callable[[], Any]:
    from decoy_engine.reference_tables import _loader

    path = _parquet(world, "ref.parquet", {"id": [1]})
    mp.setattr(_loader.arrow_adapter, "read_table", _boom(factory))
    return lambda: _loader.load_table("ref", path)


def _site_codeset_read(world: XWorld, mp: pytest.MonkeyPatch, factory: Any) -> Callable[[], Any]:
    from decoy_engine.transforms import _codeset_loader

    path = _parquet(world, "corpus.parquet", {"code": ["a"]})
    mp.setattr(_codeset_loader.arrow_adapter, "read_table", _boom(factory))
    return lambda: _codeset_loader.load_corpus("mine", path)


def _site_codeset_stat(world: XWorld, mp: pytest.MonkeyPatch, factory: Any) -> Callable[[], Any]:
    from decoy_engine.transforms import _codeset_loader

    path = _parquet(world, "corpus2.parquet", {"code": ["a"]})
    real = _rooted.FdHandle.stat
    calls = {"n": 0}

    def stat(self: _rooted.FdHandle) -> Any:
        calls["n"] += 1
        if calls["n"] >= 2:  # the existence check passes; the cache-key stat fails
            raise factory()
        return real(self)

    mp.setattr(_rooted.FdHandle, "stat", stat)
    return lambda: _codeset_loader.load_corpus("mine", path)


def _site_snapshot_spec(world: XWorld, mp: pytest.MonkeyPatch, factory: Any) -> Callable[[], Any]:
    from decoy_engine.generation.statistical._spec import _load_snapshot

    path = world.sub / "s.json"
    path.write_text("{}")
    mp.setattr(fileaccess, "read_all", _boom(factory))
    return lambda: _load_snapshot(str(path))


def _site_snapshot_pin(world: XWorld, mp: pytest.MonkeyPatch, factory: Any) -> Callable[[], Any]:
    from decoy_engine.plan._generation import read_and_pin_snapshots

    path = world.sub / "s.json"
    path.write_text("{}")
    config = {
        "tables": [
            {
                "name": "t",
                "generate_columns": [
                    {"name": "c", "type": "statistical", "snapshot_file": str(path)}
                ],
            }
        ]
    }
    mp.setattr(fileaccess, "read_all", _boom(factory))
    return lambda: read_and_pin_snapshots(config)


def _site_digest(world: XWorld, mp: pytest.MonkeyPatch, factory: Any) -> Callable[[], Any]:
    from decoy_engine.generators.derivation import _snapshot_content_digest

    path = world.sub / "s.json"
    path.write_text("{}")
    mp.setattr(fileaccess, "read_all", _boom(factory))
    return lambda: _snapshot_content_digest(str(path))


def _site_pack_fallback(world: XWorld, mp: pytest.MonkeyPatch, factory: Any) -> Callable[[], Any]:
    from decoy_engine.storm.model_pack.loader import ModelPackLoader

    pack = world.sub / "pack"
    pack.mkdir()
    mp.setattr(ModelPackLoader, "_read_member", _boom(factory))
    loader = ModelPackLoader(pack, file_access=world.policy)
    return loader.load_with_fallback


def _site_pack_weights(world: XWorld, mp: pytest.MonkeyPatch, factory: Any) -> Callable[[], Any]:
    from decoy_engine.storm.model_pack.loader import ModelPackLoader

    pack = world.sub / "pack2"
    pack.mkdir()
    mp.setattr(ModelPackLoader, "_read_member", _boom(factory))
    return ModelPackLoader(pack, file_access=world.policy)._read_weights


def _site_sign_pack(world: XWorld, mp: pytest.MonkeyPatch, factory: Any) -> Callable[[], Any]:
    from decoy_engine.storm.model_pack import _packio, provenance

    pack = world.sub / "pack"
    pack.mkdir()
    mp.setattr(_packio, "read_member", _boom(factory))
    return lambda: provenance.sign_pack(pack, b"k" * 32)


def _site_disguises(world: XWorld, mp: pytest.MonkeyPatch, factory: Any) -> Callable[[], Any]:
    from decoy_engine.disguises import loader

    folder = world.sub / "disguises"
    folder.mkdir()
    (folder / "a.yaml").write_text("name: x\n")
    mp.setattr(loader.serial, "yaml_safe_load", _boom(factory))
    return lambda: loader.load_disguises(folder, file_access=world.policy)


def _site_custom_providers(
    world: XWorld, mp: pytest.MonkeyPatch, factory: Any
) -> Callable[[], Any]:
    from decoy_engine.internal import faker_setup

    folder = world.sub / "providers"
    folder.mkdir()
    (folder / "a.txt").write_text("x\n")
    (folder / "b.json").write_text('["x"]')
    mp.setattr(faker_setup, "_read_child", _boom(factory))
    return lambda: faker_setup.load_custom_providers(folder)


def _site_lockfile(world: XWorld, mp: pytest.MonkeyPatch, factory: Any) -> Callable[[], Any]:
    from decoy_engine.quality.dp_provenance import assert_lock_matches_installed

    lock = world.sub / "uv.lock"
    lock.write_text("version = 1\n")
    mp.setattr(_rooted.FdHandle, "open_binary", _boom(factory))
    return lambda: assert_lock_matches_installed(lock)


def _site_vault(world: XWorld, mp: pytest.MonkeyPatch, factory: Any) -> Callable[[], Any]:
    from decoy_engine.vault import load_vault

    path = world.sub / "v.dcyvault"
    path.write_bytes(b"x")
    handle = world.policy.open_read(FileRequest(FileRole.VAULT, "read", str(path), "vault"))
    mp.setattr(_rooted.FdHandle, "read_all", _boom(factory))
    return lambda: load_vault(None, b"12345678", vault=handle)


def _site_capacity(world: XWorld, mp: pytest.MonkeyPatch, factory: Any) -> Callable[[], Any]:
    import decoy_engine.profile as profile
    from decoy_engine.execution.capacity import estimate_job_capacity

    pq.write_table(pa.table({"id": [1, 2], "note": ["a", "b"]}), world.sub / "p.parquet")
    config = {
        "version": 1,
        "global_settings": {"job_name": "x", "seed": 7},
        "sources": {
            "p": {"type": "file", "path": str(world.sub / "p.parquet"), "format": "parquet"}
        },
        "targets": {
            "p": {"type": "file", "path": str(world.sub / "o.parquet"), "format": "parquet"}
        },
        "tables": [
            {
                "name": "p",
                "columns": [
                    {"name": "id", "strategy": "hash", "provider_config": {"namespace": "ns"}},
                    {"name": "note", "strategy": "redact"},
                ],
            }
        ],
        "relationships": [],
    }
    mp.setattr(profile, "profile_source", _boom(factory))
    return lambda: estimate_job_capacity(config, world.sub)


def _site_spill_stat(world: XWorld, mp: pytest.MonkeyPatch, factory: Any) -> Callable[[], Any]:
    from decoy_engine.execution.out_of_core import _spill_estimate

    class Refuses:
        def stat(self, req: Any) -> Any:
            raise factory()

    def call() -> Any:
        with fileaccess.scope(world.policy):
            # swap in a policy whose stat fails with the injected shape
            from decoy_engine.fileaccess import _entry

            token = _entry._CURRENT.set(Refuses())  # type: ignore[arg-type]
            try:
                return _spill_estimate._target_device(world.sub, "targets.p.path")
            finally:
                _entry._CURRENT.reset(token)

    return call


SITES: dict[str, Site] = {
    "reference_table": _site_reference_table,
    "codeset_read": _site_codeset_read,
    "codeset_stat": _site_codeset_stat,
    "snapshot_spec": _site_snapshot_spec,
    "snapshot_pin": _site_snapshot_pin,
    "snapshot_digest": _site_digest,
    "pack_fallback": _site_pack_fallback,
    "pack_weights": _site_pack_weights,
    "sign_pack": _site_sign_pack,
    "disguises": _site_disguises,
    "custom_providers": _site_custom_providers,
    "lockfile": _site_lockfile,
    "vault": _site_vault,
    "capacity": _site_capacity,
    "spill_stat": _site_spill_stat,
}

# Handlers that swallow by design when the shape is not a classified one.
SWALLOWS = {"disguises", "custom_providers", "spill_stat", "pack_fallback", "snapshot_pin"}


@pytest.mark.parametrize("site", sorted(SITES))
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_every_handler_keeps_the_class_of_a_classified_failure(
    xworld: XWorld,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    shape: str,
) -> None:
    factory, keep, memory = SHAPES[shape]
    call = SITES[site](xworld, monkeypatch, factory)
    sample = factory()
    if not keep:
        # Negative control: not a classified failure, so a handler may translate or swallow
        # it; the shape itself must stay unrecognized.
        assert not is_memory_failure(sample)
        with fileaccess.scope(xworld.policy):
            try:
                call()
            except BaseException as exc:
                assert not is_memory_failure(exc)
        return
    with fileaccess.scope(xworld.policy), pytest.raises(type(sample)) as info:
        call()
    assert is_memory_failure(info.value) is memory
    if isinstance(sample, OSError):
        assert isinstance(info.value, OSError) and info.value.errno == sample.errno


@pytest.mark.parametrize("site", ["reference_table", "codeset_read", "snapshot_spec"])
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_strict_in_process_isolated_outcome_follows_the_classification(
    xworld: XWorld,
    monkeypatch: pytest.MonkeyPatch,
    site: str,
    shape: str,
) -> None:
    from decoy_engine.execution import _isolated_run

    factory, keep, memory = SHAPES[shape]
    call = SITES[site](xworld, monkeypatch, factory)
    monkeypatch.setattr(_isolated_run, "run_pipeline", lambda *a, **k: call())
    result = _isolated_run.run_pipeline_isolated(
        {}, {}, isolate=False, engine_version="e", file_access=xworld.policy
    )
    if keep:
        assert result.outcome == ("oom_killed" if memory else "crashed")
    else:
        assert result.outcome == "crashed"


# -- compatibility controls: the ambient policy keeps today's translation --------------------


def test_ambient_handlers_still_translate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.generation.statistical._spec import StatisticalSpecError, _load_snapshot

    path = tmp_path / "s.json"
    path.write_text("{}")
    monkeypatch.setattr(fileaccess, "read_all", _boom(SHAPES["enomem"][0]))
    with pytest.raises(StatisticalSpecError):
        _load_snapshot(str(path))


def test_ambient_disguise_loader_still_skips_an_unreadable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.disguises import loader

    folder = tmp_path / "d"
    folder.mkdir()
    (folder / "a.yaml").write_text("name: x\n")
    monkeypatch.setattr(loader.serial, "yaml_safe_load", _boom(SHAPES["enomem"][0]))
    assert loader.load_disguises(folder) == []


def test_the_classifier_itself_is_unchanged() -> None:
    """ENOSPC and EDQUOT keep their class but are not memory failures; the single-token
    `Wrapping` shape stays distinct from the whitespace-bearing one."""
    assert not is_memory_failure(OSError(errno.ENOSPC, "x"))
    assert not is_memory_failure(OSError(errno.EDQUOT, "x"))
    assert is_memory_failure(pa.ArrowInvalid("Unknown error: Wrapping a failed"))
    assert not is_memory_failure(pa.ArrowInvalid("Unknown error: Wrapping a b failed"))
    json.dumps({})
