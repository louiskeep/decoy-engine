"""Public `job_seed_for_config`, the CLI vault-inspector's seed helper.

It exposes the same 8-byte job seed the vault and `unmask_pipeline` key on, so an
external caller can `load_vault(path, job_seed_for_config(config))` without
reaching into the private normalizer.
"""

from __future__ import annotations

import pytest

from decoy_engine import job_seed_for_config
from decoy_engine.plan import PlanCompileError
from decoy_engine.plan._seed import _normalize_job_seed


def test_returns_canonical_8_byte_seed():
    cfg = {"global_settings": {"seed": 42}}
    out = job_seed_for_config(cfg)
    assert isinstance(out, bytes) and len(out) == 8
    # Must equal the private normalizer the vault + unmask path key on.
    assert out == _normalize_job_seed(cfg)


def test_is_deterministic():
    cfg = {"global_settings": {"seed": 12345}}
    assert job_seed_for_config(cfg) == job_seed_for_config(cfg)


def test_rejects_non_int_seed_like_the_run_path():
    # F5: a bool/float seed is rejected by the single shared normalizer.
    with pytest.raises(PlanCompileError):
        job_seed_for_config({"global_settings": {"seed": True}})
