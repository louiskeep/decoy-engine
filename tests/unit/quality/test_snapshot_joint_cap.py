"""GEN-COND-CAP E1: the `contingency_top_k` bounds, the kind refusal and the fit-side warning."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pandas as pd
import pytest

from decoy_engine import reasons, snapshot_joint_warnings
from decoy_engine.profiling import distribution_snapshot_document
from decoy_engine.quality.snapshot import (
    DEFAULT_CONTINGENCY_TOP_K,
    MAX_CONTINGENCY_TOP_K,
    DistributionSnapshotError,
    compute_distribution_snapshot,
)

_WEIGHTS = (20, 15, 12, 10, 9, 8, 7, 6, 5, 4, 3, 1)
_PAIR = [("parent", "kid")]
# Digests of both writers at the default cap, recorded at origin/main 1a5b36e5 (AT-1).
_DIGEST_COMPUTE = "796fa21ab05e1f3024eefd6b9523d18e55e76561c9510cb5ead91e4e66d8e3be"
_DIGEST_DOCUMENT = "263822eea1d98cbf3d48c4bf22103b5ad7fa24ce99c613014c1fe6dcb1c77b5e"


def _cond_frame() -> pd.DataFrame:
    """6,000 rows, 36 parent/kid pairs; built without an RNG."""
    parents: list[str] = []
    kids: list[str] = []
    for i, w in enumerate(_WEIGHTS):
        for j, share in enumerate((6, 3, 1)):
            n = w * 6 * share
            parents += [f"SENTP{i + 1:02d}"] * n
            kids += [f"SENTK{(i + 1 + j) % 6 + 1}"] * n
    return pd.DataFrame({"parent": parents, "kid": kids})


def _digest(doc: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def _scan(texts: list[str]) -> None:
    for t in texts:
        assert "SENTP" not in t
        assert "SENTK" not in t


def test_at1_default_bytes_unchanged() -> None:
    df = _cond_frame()
    assert _digest(compute_distribution_snapshot(df, joint_columns=_PAIR)) == _DIGEST_COMPUTE
    assert _digest(distribution_snapshot_document(df, joint_columns=_PAIR)) == _DIGEST_DOCUMENT


def test_fixture_shape() -> None:
    (joint,) = compute_distribution_snapshot(_cond_frame(), joint_columns=_PAIR)["joints"]
    assert (joint["cell_count"], len(joint["cells"]), joint["other_count"]) == (36, 25, 366)


@pytest.mark.parametrize("writer", [compute_distribution_snapshot, distribution_snapshot_document])
def test_at2a_raised_cap_keeps_all_and_warns_nothing(writer: Any) -> None:
    doc = writer(_cond_frame(), joint_columns=_PAIR, contingency_top_k=36)
    (joint,) = doc["joints"]
    assert len(joint["cells"]) == 36
    assert joint["other_count"] == 0
    assert snapshot_joint_warnings(doc, contingency_top_k=36) == []


def test_at3a_default_cap_reports_the_cut() -> None:
    doc = distribution_snapshot_document(_cond_frame(), joint_columns=_PAIR)
    (w,) = snapshot_joint_warnings(doc, contingency_top_k=DEFAULT_CONTINGENCY_TOP_K)
    assert w.code == reasons.warning.FIT_JOINT_CELLS_TRUNCATED
    assert w.detail == {
        "columns": ["kid", "parent"],
        "cell_count": 36,
        "cells_kept": 25,
        "cap_dropped": 11,
        "policy_dropped": 0,
        "other_count": 366,
    }
    _scan([repr(w.detail), str(w.column)])


def test_at3a_policy_dropped_is_apart_from_cap_dropped() -> None:
    doc = compute_distribution_snapshot(_cond_frame(), joint_columns=_PAIR)
    doc["joints"][0]["cells"] = doc["joints"][0]["cells"][:23]  # a value policy removed two
    (w,) = snapshot_joint_warnings(doc, contingency_top_k=25)
    assert (w.detail["cells_kept"], w.detail["cap_dropped"], w.detail["policy_dropped"]) == (
        23,
        11,
        2,
    )


@pytest.mark.parametrize("bad", [0, -1, MAX_CONTINGENCY_TOP_K + 1, True, 2.5, "25", None])
@pytest.mark.parametrize("writer", [compute_distribution_snapshot, distribution_snapshot_document])
def test_at4_bad_caps_are_refused(writer: Any, bad: Any) -> None:
    with pytest.raises(DistributionSnapshotError) as exc:
        writer(_cond_frame(), joint_columns=_PAIR, contingency_top_k=bad)
    assert exc.value.code == "contingency_top_k_invalid"
    _scan([str(exc.value)])


@pytest.mark.parametrize("ok", [1, MAX_CONTINGENCY_TOP_K])
def test_at4_edge_caps_work(ok: int) -> None:
    doc = compute_distribution_snapshot(_cond_frame(), joint_columns=_PAIR, contingency_top_k=ok)
    assert len(doc["joints"][0]["cells"]) == min(ok, 36)


def test_at5_a_withheld_joint_gets_no_truncation_record() -> None:
    emails = [f"user{i % 12}@example.com" for i in range(1200)]
    df = pd.DataFrame({"email": emails, "kid": [f"SENTK{(i // 12) % 3}" for i in range(1200)]})
    for cap in (DEFAULT_CONTINGENCY_TOP_K, MAX_CONTINGENCY_TOP_K):
        doc = distribution_snapshot_document(
            df, joint_columns=[("email", "kid")], contingency_top_k=cap
        )
        assert "email" in doc["redacted_columns"]
        assert doc["joints"][0]["cells"] == []
        assert doc["joints"][0]["cell_count"] == 36
        assert snapshot_joint_warnings(doc, contingency_top_k=cap) == []


def _kind_frame() -> dict[str, pd.DataFrame]:
    n = 120
    kid = [f"SENTK{i % 3}" for i in range(n)]
    base = {"kid": kid}
    return {
        "numeric": pd.DataFrame({"a_p": list(range(n)), **base}),
        "datetime": pd.DataFrame({"a_p": pd.date_range("2020-01-01", periods=n), **base}),
        "highcard": pd.DataFrame({"a_p": [f"label{i}" for i in range(n)], **base}),
        "freetext": pd.DataFrame({"a_p": [f"some text {i}" for i in range(n)], **base}),
        "empty": pd.DataFrame({"a_p": [None] * n, **base}),
    }


@pytest.mark.parametrize("which", ["numeric", "datetime", "highcard", "freetext", "empty"])
def test_at13_non_categorical_pairs_refused_above_default(which: str) -> None:
    df = _kind_frame()[which]
    hc = ["a_p"] if which == "highcard" else []
    joint = [("a_p", "kid")]
    kw: dict[str, Any] = {"joint_columns": joint, "high_cardinality_columns": hc}
    # allowed at the default and with explicit raw opt-in
    distribution_snapshot_document(df, **kw)
    distribution_snapshot_document(df, contingency_top_k=26, include_values=True, **kw)
    with pytest.raises(DistributionSnapshotError) as exc:
        distribution_snapshot_document(df, contingency_top_k=26, **kw)
    assert exc.value.code == "contingency_top_k_kind_unsupported"
    assert "'a_p'" in str(exc.value)
    assert "--" not in str(exc.value)
    _scan([str(exc.value)])


def test_at13_categorical_pair_fits_raised_cap() -> None:
    doc = distribution_snapshot_document(_cond_frame(), joint_columns=_PAIR, contingency_top_k=36)
    assert len(doc["joints"][0]["cells"]) == 36


def test_a_raw_column_sorting_after_the_categorical_one_is_refused_too() -> None:
    df = _kind_frame()["numeric"].rename(columns={"a_p": "z_p"})
    with pytest.raises(DistributionSnapshotError) as exc:
        distribution_snapshot_document(df, joint_columns=[("z_p", "kid")], contingency_top_k=26)
    assert exc.value.code == "contingency_top_k_kind_unsupported"
    assert "'z_p'" in str(exc.value)


def test_a_sensitive_joint_in_a_raw_document_still_warns_at_the_default_cap() -> None:
    emails = [f"user{i % 12}@example.com" for i in range(1200)]
    kids = [f"SENTK{(i // 12) % 3}" for i in range(1200)]
    df = pd.DataFrame({"email": emails, "kid": kids})
    doc = distribution_snapshot_document(df, joint_columns=[("email", "kid")], include_values=True)
    (w,) = snapshot_joint_warnings(doc, contingency_top_k=DEFAULT_CONTINGENCY_TOP_K)
    assert w.detail["cap_dropped"] == 11


@pytest.mark.parametrize("bad", [0, -1, MAX_CONTINGENCY_TOP_K + 1, True, 2.5, None])
def test_the_warning_reader_validates_its_cap_like_the_writer(bad: Any) -> None:
    doc = compute_distribution_snapshot(_cond_frame(), joint_columns=_PAIR)
    with pytest.raises(DistributionSnapshotError) as exc:
        snapshot_joint_warnings(doc, contingency_top_k=bad)
    assert exc.value.code == "contingency_top_k_invalid"


def test_the_warning_reader_refuses_a_cap_smaller_than_the_document_holds() -> None:
    doc = compute_distribution_snapshot(_cond_frame(), joint_columns=_PAIR, contingency_top_k=36)
    with pytest.raises(DistributionSnapshotError) as exc:
        snapshot_joint_warnings(doc, contingency_top_k=25)
    assert exc.value.code == "contingency_top_k_invalid"
    _scan([str(exc.value)])
