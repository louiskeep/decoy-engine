"""joint_mask row streams: every group draws under its own derived seed.

The null-key fallback and gen mode used to seed NumPy from `job_seed[:8]`, so every
group of a run (and every table) walked one stream and picked the same reference row
index. Each stream now comes from `derive(job_seed, namespace, source)` where the source
names the stream label, the table, the reference and the target columns.
"""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np
import pandas as pd
import pytest

from decoy_engine.determinism import derive
from decoy_engine.transforms.joint_mask import JointMaskConfig, apply_joint_mask

_KEY32 = b"\xca\xfe" * 16
_REF = "us_zip5_city_state"
_N = 40


def _lp(text: str) -> bytes:
    raw = text.encode("utf-8")
    return len(raw).to_bytes(4, "big") + raw


def _expected_seed_int(
    label: bytes, key: bytes, namespace: str, table: str, reference: str, columns: tuple[str, ...]
) -> int:
    source = label + _lp(table) + _lp(reference) + _lp("\x1f".join(columns))
    return int.from_bytes(derive(key, namespace, source)[:8], "big")


def _cfg(columns: list[str]) -> JointMaskConfig:
    return JointMaskConfig.from_dict({"columns": columns, "reference": _REF, "key_by": "pid"})


def _frame(null_keys: bool, columns: list[str]) -> pd.DataFrame:
    keys: list[Any] = [None] * _N if null_keys else [f"P{i:04d}" for i in range(_N)]
    return pd.DataFrame({"pid": keys, **{c: ["x"] * _N for c in columns}})


def _real_pairs(config: JointMaskConfig, left: str, right: str) -> set[tuple[str, str]]:
    tbl = config.table
    return {(tbl.row(i)[left], tbl.row(i)[right]) for i in range(tbl.row_count)}


def _run(mode: str, columns: list[str], *, table: str = "t", namespace: str | None = None):
    config = _cfg(columns)
    frame = _frame(mode == "mask", columns)
    return apply_joint_mask(
        frame, config, mode=mode, job_seed=_KEY32, namespace=namespace, table=table
    )


@pytest.mark.parametrize("mode", ["mask", "gen"])
def test_two_groups_on_one_table_draw_different_reference_rows(mode: str) -> None:
    """AT-E2-1 (and the gen half of AT-E2-2): group one writes zip, group two writes city.

    On a shared stream both groups pick the same row index, so every (zip, city) pair is a
    real reference row. Under separate streams the pairs almost never are.
    """
    zips = _run(mode, ["zip"])["zip"].tolist()
    cities = _run(mode, ["city"])["city"].tolist()
    real = _real_pairs(_cfg(["zip", "city"]), "zip", "city")
    coupled = sum((z, c) in real for z, c in zip(zips, cities, strict=True))
    assert coupled < _N // 2, f"{coupled} of {_N} rows picked one shared reference row"


@pytest.mark.parametrize("mode", ["mask", "gen"])
def test_same_inputs_draw_the_same_rows_across_runs(mode: str) -> None:
    first = _run(mode, ["zip", "city", "state"])
    second = _run(mode, ["zip", "city", "state"])
    pd.testing.assert_frame_equal(first, second)


@pytest.mark.parametrize("mode", ["mask", "gen"])
def test_the_table_name_separates_the_stream(mode: str) -> None:
    here = _run(mode, ["zip", "city"], table="patients")["zip"].tolist()
    there = _run(mode, ["zip", "city"], table="visits")["zip"].tolist()
    assert here != there


@pytest.mark.parametrize(
    ("mode", "label"),
    [("mask", b"joint_mask/null_fallback/v1"), ("gen", b"joint_mask/gen_row/v1")],
)
def test_the_stream_seed_is_the_derived_source_and_never_job_seed_bytes(
    mode: str, label: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AT-E2-6: patch default_rng and inspect its argument."""
    columns = ["zip", "city", "state"]
    config = _cfg(columns)
    seen: list[Any] = []
    original = np.random.default_rng

    def spy(arg: Any = None) -> Any:
        seen.append(arg)
        return original(arg)

    monkeypatch.setattr(np.random, "default_rng", spy)
    apply_joint_mask(
        _frame(mode == "mask", columns),
        config,
        mode=mode,
        job_seed=_KEY32,
        namespace="ns/col",
        table="patients",
    )
    assert seen == [_expected_seed_int(label, _KEY32, "ns/col", "patients", _REF, tuple(columns))]
    assert int.from_bytes(_KEY32[:8], "big") not in seen


def test_a_missing_namespace_derives_under_the_joint_mask_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []
    original = np.random.default_rng

    def spy(arg: Any = None) -> Any:
        seen.append(arg)
        return original(arg)

    monkeypatch.setattr(np.random, "default_rng", spy)
    apply_joint_mask(_frame(False, ["zip"]), _cfg(["zip"]), mode="gen", job_seed=_KEY32, table="t")
    label = b"joint_mask/gen_row/v1"
    assert seen == [_expected_seed_int(label, _KEY32, "joint_mask", "t", _REF, ("zip",))]


def test_an_empty_table_name_is_refused_not_defaulted() -> None:
    with pytest.raises(ValueError):
        apply_joint_mask(
            _frame(False, ["zip"]), _cfg(["zip"]), mode="gen", job_seed=_KEY32, table=""
        )


def test_keyed_selection_for_non_null_keys_is_byte_identical_to_main() -> None:
    """AT-E2-3 control. The digest was captured from `origin/main` before the change."""
    columns = ["zip", "city", "state"]
    frame = pd.DataFrame({"pid": [f"P{i}" for i in range(20)], **{c: ["x"] * 20 for c in columns}})
    out = apply_joint_mask(
        frame, _cfg(columns), mode="mask", job_seed=bytes(range(32)), namespace="t", table="t"
    )
    digest = hashlib.sha256(repr({c: out[c].tolist() for c in columns}).encode()).hexdigest()
    assert digest == "fbff5167b6ef7cecbf95358a08629e819a2b6909a2604944588e31d16d268fe5"
