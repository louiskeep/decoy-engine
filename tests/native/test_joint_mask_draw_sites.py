"""The two joint_mask stream draw sites and the composite build-pool seed rule.

`mask.joint_mask_null_fallback` and `mask.joint_mask_gen_row` are the numpy streams that
`transforms/joint_mask.py` seeds from `derive(job_seed, namespace, source)`; each has one
provider that reproduces the source bytes. `gen.composite_build_pool` now draws from a
resolved 8-byte seed, so its native mirror refuses a missing or short seed.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from decoy_engine.determinism import derive, seed_bytes_to_int
from decoy_engine.execution.native import _determinism_protocol as protocol
from decoy_engine.execution.native._determinism_protocol import (
    DrawSiteProtocolError,
    all_providers,
    draw_site_by_id,
    provider_for,
)
from decoy_engine.transforms.joint_mask import JointMaskConfig, apply_joint_mask

_NEW_IDS = ("mask.joint_mask_null_fallback", "mask.joint_mask_gen_row")
_LABELS = {
    "mask.joint_mask_null_fallback": b"joint_mask/null_fallback/v1",
    "mask.joint_mask_gen_row": b"joint_mask/gen_row/v1",
}
_KEY = bytes(range(32))
_REF = "us_zip5_city_state"


def _lp(text: str) -> bytes:
    raw = text.encode("utf-8")
    return len(raw).to_bytes(4, "big") + raw


@pytest.mark.parametrize("site_id", _NEW_IDS)
def test_each_new_site_is_catalogued_with_exactly_one_provider(site_id: str) -> None:
    ids = [site.draw_site_id for site in protocol.DRAW_SITES]
    assert ids.count(site_id) == 1
    assert site_id in all_providers()
    site = draw_site_by_id(site_id)
    assert (site.family, site.entropy_root, site.partitionable) == (
        "numpy_pcg64",
        "mask_key",
        False,
    )


@pytest.mark.parametrize("site_id", _NEW_IDS)
def test_the_source_is_the_label_then_three_length_prefixed_fields(site_id: str) -> None:
    provider: Any = provider_for(site_id)
    got = provider.source("patients", _REF, ("zip", "city"))
    assert got == _LABELS[site_id] + _lp("patients") + _lp(_REF) + _lp("zip\x1fcity")


@pytest.mark.parametrize("site_id", _NEW_IDS)
def test_length_prefixes_keep_adjacent_names_apart(site_id: str) -> None:
    provider: Any = provider_for(site_id)
    assert provider.source("ab", "c", ("x",)) != provider.source("a", "bc", ("x",))
    assert provider.source("t", _REF, ("ab", "c")) != provider.source("t", _REF, ("a", "bc"))


@pytest.mark.parametrize("site_id", _NEW_IDS)
def test_a_partitioned_draw_is_refused(site_id: str) -> None:
    with pytest.raises(DrawSiteProtocolError) as exc:
        provider_for(site_id).partitioned_draw()
    assert exc.value.code == "site_not_partitionable"


@pytest.mark.parametrize(
    ("site_id", "mode"),
    [("mask.joint_mask_null_fallback", "mask"), ("mask.joint_mask_gen_row", "gen")],
)
def test_the_provider_reproduces_the_seed_the_transform_draws(
    site_id: str, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    columns = ("zip", "city", "state")
    config = JointMaskConfig.from_dict(
        {"columns": list(columns), "reference": _REF, "key_by": "pid"}
    )
    keys: list[Any] = [None] * 6 if mode == "mask" else [f"P{i}" for i in range(6)]
    frame = pd.DataFrame({"pid": keys, **{c: ["x"] * 6 for c in columns}})
    seen: list[Any] = []
    real = np.random.default_rng

    def spy(arg: Any = None) -> Any:
        seen.append(arg)
        return real(arg)

    monkeypatch.setattr(np.random, "default_rng", spy)
    apply_joint_mask(frame, config, mode=mode, job_seed=_KEY, namespace="ns/a", table="patients")
    provider: Any = provider_for(site_id)
    derived = derive(_KEY, "ns/a", provider.source("patients", _REF, columns))
    assert seen == [seed_bytes_to_int(derived[:8])]


def test_the_composite_build_pool_mirror_refuses_a_missing_or_empty_seed() -> None:
    """AT-E2-4: the native mirror needs a resolved seed of 8 or 32 bytes, as the Python build."""
    provider: Any = provider_for("gen.composite_build_pool")
    for bad in (None, b"", b"\x01" * 7, b"\x01" * 16):
        with pytest.raises(ValueError):
            provider.generator(bad)
    assert provider.generator(b"\x00" * 6 + b"\x09\x08").random() == pytest.approx(
        np.random.default_rng(0x0908).random()
    )
    wide = bytes(range(1, 33))
    assert provider.generator(wide).random() == pytest.approx(
        np.random.default_rng(int.from_bytes(wide, "big")).random()
    )


def test_the_composite_build_pool_entry_names_resolve_seed() -> None:
    """AT-E2-7: the seed text no longer says the zero fallback."""
    text = draw_site_by_id("gen.composite_build_pool").seed_derivation
    assert "resolve_seed(spec.seed)" in text and "one per spec" in text
    assert "x00" not in text
