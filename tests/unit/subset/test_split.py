"""AT-21: the split leaves a run config that dumps and hashes like one that never had a subset."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.subset import SubsetError, split_subset_config
from tests.unit.execution import _subset_run_support as sup


def _cfg(tmp_path: Path) -> dict[str, Any]:
    return sup.job(tmp_path)[0]


def test_run_config_equals_a_config_with_no_subset(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    plain = copy.deepcopy(cfg)
    plain["subset"] = None
    split = split_subset_config(cfg)
    assert split.run_config == plain
    assert "subset" in split.run_config and split.run_config["subset"] is None
    assert cfg["subset"] is not None, "the caller's dict is not edited"


def test_config_hash_equals_that_of_the_same_config_without_a_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import decoy_engine.plan as plan_pkg
    from decoy_engine.profile import profile_source

    cfg = _cfg(tmp_path)
    plain = copy.deepcopy(cfg)
    plain["subset"] = None
    hashes = []
    for c in (split_subset_config(cfg).run_config, plain):
        plan = plan_pkg.compile_plan(c, profile_source(c), decoy_engine_version="t")
        hashes.append(plan.pipeline_config_hash)
    assert hashes[0] == hashes[1]


def test_no_block_means_no_request_and_the_same_object(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg["subset"] = None
    split = split_subset_config(cfg)
    assert split.request is None and split.fingerprint is None and split.run_config is cfg


def test_fingerprint_follows_the_request(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    other = copy.deepcopy(cfg)
    other["subset"]["seeds"][0]["count"] = 6
    a, b = split_subset_config(cfg), split_subset_config(other)
    assert a.fingerprint == split_subset_config(cfg).fingerprint
    assert a.fingerprint != b.fingerprint


def test_a_bad_seed_raises_a_subset_error(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg["subset"]["seeds"][0]["fraction"] = 3.0
    cfg["subset"]["seeds"][0]["count"] = None
    with pytest.raises(SubsetError):
        split_subset_config(cfg)


def test_validate_plan_returns_findings_for_a_subset_config(tmp_path: Path) -> None:
    from decoy_engine.plan import validate_plan
    from decoy_engine.profile import profile_source

    cfg = _cfg(tmp_path)
    ok = validate_plan(cfg, profile_source(cfg), decoy_engine_version="t")
    assert ok.ok, ok.error
    cfg["subset"]["seeds"][0]["fraction"] = 3.0
    cfg["subset"]["seeds"][0]["count"] = None
    bad = validate_plan(cfg, profile_source(cfg), decoy_engine_version="t")
    assert not bad.ok and bad.error is not None and bad.error.code.startswith("subset")
