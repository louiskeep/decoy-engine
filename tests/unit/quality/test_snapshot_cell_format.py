"""The optional per-column `cell_format` block of the distribution snapshot."""

from __future__ import annotations

import hashlib
import json

import pandas as pd

from decoy_engine.quality.snapshot import compute_distribution_snapshot

IDS = ["p1"] * 8 + ["p2"] * 5 + ["p3"] * 2 + ["p4", "p5", "p6", "p7", "p8"]

# Digest of the snapshot of `_visits()` (with one joint) minted before the keyword existed.
_BASELINE_SHA256 = "c6a4a1302e1b37bcd617ea53b8aaa5e817dff857bd0088be11f08cc20b4b31ad"


def _visits() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "patient_id": IDS,
            "amount": [float(i) for i in range(20)],
            "note": [f"n{i % 3}" for i in range(20)],
        }
    )


def _digest(snap: dict) -> str:
    raw = json.dumps(snap, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _all_kinds() -> pd.DataFrame:
    n = 40
    return pd.DataFrame(
        {
            "price": [round(1.5 + i * 0.25, 2) for i in range(n)],
            "visited": pd.date_range("2021-04-01", periods=n, freq="D"),
            "note": ["", "", *[f"free text number {i}" for i in range(n - 2)]],
            "region": ["north", "south"] * (n // 2),
            "unused": [None] * n,
        }
    )


# AT-7 snapshot cell


def test_no_keyword_is_byte_identical_to_prior_engine() -> None:
    joints = [("patient_id", "note")]
    plain = compute_distribution_snapshot(_visits(), joint_columns=joints)
    off = compute_distribution_snapshot(_visits(), joint_columns=joints, cell_format=False)
    assert _digest(plain) == _BASELINE_SHA256
    assert _digest(off) == _BASELINE_SHA256
    assert all("cell_format" not in c for c in plain["columns"].values())


def test_cell_format_is_purely_additive() -> None:
    plain = compute_distribution_snapshot(_all_kinds())
    fitted = compute_distribution_snapshot(_all_kinds(), cell_format=True)
    for col in fitted["columns"].values():
        assert "cell_format" in col
        del col["cell_format"]
    assert fitted == plain


def test_each_kind_gets_its_block() -> None:
    cols = compute_distribution_snapshot(_all_kinds(), cell_format=True)["columns"]
    assert {n: (c["kind"], c["cell_format"]) for n, c in cols.items()} == {
        "price": ("numeric", {"decimal_places": 2}),
        "visited": ("datetime", {"resolution": "day"}),
        "note": ("freetext", {"blank_count": 1 + 1}),
        "region": ("categorical", {}),
        "unused": ("empty", {}),
    }


def test_the_keyword_does_not_mutate_the_frame() -> None:
    df = _all_kinds()
    before = df.copy(deep=True)
    compute_distribution_snapshot(df, cell_format=True)
    pd.testing.assert_frame_equal(df, before)
