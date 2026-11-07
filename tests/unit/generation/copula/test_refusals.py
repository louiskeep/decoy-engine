"""A8: fit and sample refuse bad input before any work, with value-free messages."""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation.copula import fit_gaussian_copula, sample_gaussian_copula

from ._fixtures import numeric_frame, seed_bytes, snapshot

PLANTED = 987654.125
_FRAME = numeric_frame(5, n=300)
_FRAME.loc[17, "x"] = PLANTED
_SNAP = snapshot(_FRAME)
_MODEL = fit_gaussian_copula(_FRAME, _SNAP, seed=seed_bytes(1))


def _expect(error: type[Exception], call, match: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(error, match=match) as info:
        call()
    message = str(info.value)
    assert "987654" not in message and "zz-label" not in message
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_frame_must_be_a_dataframe() -> None:
    _expect(
        TypeError,
        lambda: fit_gaussian_copula(_FRAME.to_dict(), _SNAP, seed=seed_bytes(1)),
        "DataFrame",
    )


def test_colliding_column_names() -> None:
    frame = pd.DataFrame(np.ones((120, 2)), columns=[1, "1"])
    _expect(ValueError, lambda: fit_gaussian_copula(frame, {}, seed=seed_bytes(1)), "unique")


@pytest.mark.parametrize(
    ("seed", "error"), [("x" * 16, TypeError), (b"short", ValueError), (bytearray(16), TypeError)]
)
def test_bad_seed(seed: object, error: type[Exception]) -> None:
    _expect(error, lambda: fit_gaussian_copula(_FRAME, _SNAP, seed=seed), "seed")  # type: ignore[arg-type]
    _expect(error, lambda: sample_gaussian_copula(_MODEL, 1, seed=seed), "seed")  # type: ignore[arg-type]


def test_infinite_value() -> None:
    frame = _FRAME.copy()
    frame.loc[3, "y"] = np.inf
    _expect(ValueError, lambda: fit_gaussian_copula(frame, _SNAP, seed=seed_bytes(1)), "infinite")


def test_datetime_and_freetext_columns() -> None:
    frame = _FRAME.copy()
    frame["when"] = pd.date_range("2020-01-01", periods=len(frame), freq="D")
    _expect(
        ValueError,
        lambda: fit_gaussian_copula(frame, snapshot(frame), seed=seed_bytes(1)),
        "datetime",
    )
    frame = _FRAME.copy()
    frame["note"] = [f"zz-label {i}" for i in range(len(frame))]
    _expect(
        ValueError,
        lambda: fit_gaussian_copula(frame, snapshot(frame), seed=seed_bytes(1)),
        "freetext",
    )


def _tampered(edit) -> dict:  # type: ignore[no-untyped-def]
    snap = copy.deepcopy(_SNAP)
    edit(snap)
    return snap


@pytest.mark.parametrize(
    ("name", "edit", "match"),
    [
        (
            "wrong version",
            lambda s: s.update(schema_version="dps-marginal/v3"),
            "distribution-snapshot/v1",
        ),
        ("missing column", lambda s: s["columns"].pop("x"), "exactly the frame's columns"),
        ("row count", lambda s: s.update(row_count=301), "row_count"),
        ("null count", lambda s: s["columns"]["x"].update(null_count=1), "null_count"),
        (
            "bin edge",
            lambda s: s["columns"]["y"]["stats"]["bin_edges"].__setitem__(3, 0.5),
            "bin_edges",
        ),
        (
            "bin count",
            lambda s: s["columns"]["y"]["stats"]["bin_counts"].__setitem__(0, 0),
            "bin_counts",
        ),
        ("kind", lambda s: s["columns"]["w"].update(kind="categorical"), "kind"),
    ],
)
def test_snapshot_must_describe_the_frame(name: str, edit, match: str) -> None:  # type: ignore[no-untyped-def]
    _expect(
        ValueError, lambda: fit_gaussian_copula(_FRAME, _tampered(edit), seed=seed_bytes(1)), match
    )


def test_snapshot_of_another_frame_with_equal_counts() -> None:
    other = numeric_frame(6, n=300)
    _expect(
        ValueError,
        lambda: fit_gaussian_copula(_FRAME, snapshot(other), seed=seed_bytes(1)),
        "does not match",
    )


def test_snapshot_with_a_different_bin_count_is_recomputed_with_it() -> None:
    model = fit_gaussian_copula(_FRAME, snapshot(_FRAME, numeric_bins=5), seed=seed_bytes(1))
    assert all(len(c.bin_counts) == 5 for c in model.columns)


@pytest.mark.parametrize(
    ("n", "start", "error"),
    [(-1, 0, ValueError), (1, -1, ValueError), (True, 0, TypeError), (1, 1.5, TypeError)],
)
def test_bad_sample_size_or_start(n: object, start: object, error: type[Exception]) -> None:
    _expect(
        error, lambda: sample_gaussian_copula(_MODEL, n, seed=seed_bytes(1), start=start), "n|start"
    )  # type: ignore[arg-type]


def test_sample_needs_a_model() -> None:
    _expect(
        TypeError,
        lambda: sample_gaussian_copula(_MODEL.to_dict(), 1, seed=seed_bytes(1)),
        "GaussianCopulaModel",
    )  # type: ignore[arg-type]
