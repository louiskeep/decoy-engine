"""The optional per-column `fanout` block of the distribution snapshot."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from decoy_engine import reasons
from decoy_engine.generation.statistical import load_spec
from decoy_engine.generation.statistical._fanout import fanout_spec_from_snapshot
from decoy_engine.generation.statistical._sample import sample_column
from decoy_engine.quality.fidelity import compute_fidelity
from decoy_engine.quality.snapshot import (
    DistributionSnapshotError,
    compute_distribution_snapshot,
)

IDS = ["p1"] * 8 + ["p2"] * 5 + ["p3"] * 2 + ["p4", "p5", "p6", "p7", "p8"]


def _visits() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "patient_id": IDS,
            "amount": [float(i) for i in range(20)],
            "note": [f"n{i % 3}" for i in range(20)],
        }
    )


# Digest of the snapshot of `_visits()` (with one joint) minted by the engine
# before the fan-out keyword existed. Omitting the keyword must reproduce it.
_BASELINE_SHA256 = "c6a4a1302e1b37bcd617ea53b8aaa5e817dff857bd0088be11f08cc20b4b31ad"


def _digest(snap: dict) -> str:
    raw = json.dumps(snap, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def test_no_keyword_is_byte_identical_to_prior_engine() -> None:
    joints = [("patient_id", "note")]
    plain = compute_distribution_snapshot(_visits(), joint_columns=joints)
    empty = compute_distribution_snapshot(_visits(), joint_columns=joints, fanout_columns=())
    assert _digest(plain) == _BASELINE_SHA256
    assert _digest(empty) == _BASELINE_SHA256
    assert all("fanout" not in c for c in plain["columns"].values())


def test_fanout_is_purely_additive() -> None:
    plain = compute_distribution_snapshot(_visits())
    with_fan = compute_distribution_snapshot(_visits(), fanout_columns=("patient_id",))
    block = with_fan["columns"]["patient_id"].pop("fanout")
    assert with_fan == plain
    assert block == {"rows": 20, "keys": 8, "degrees": [[1, 5], [2, 1], [5, 1], [8, 1]]}


def test_block_holds_counts_only() -> None:
    snap = compute_distribution_snapshot(_visits(), fanout_columns=["patient_id"])
    text = json.dumps(snap["columns"]["patient_id"]["fanout"])
    assert not any(pid in text for pid in set(IDS))


def test_numeric_key_gets_a_block() -> None:
    df = pd.DataFrame({"k": [1, 1, 2, 3, 3, 3]})
    snap = compute_distribution_snapshot(df, fanout_columns=["k"])
    assert snap["columns"]["k"]["fanout"] == {
        "rows": 6,
        "keys": 3,
        "degrees": [[1, 1], [2, 1], [3, 1]],
    }


@pytest.mark.parametrize("bad", ["patient_id", b"patient_id", ["nope"], ("patient_id", "nope")])
def test_bare_string_and_unknown_name_raise(bad: object) -> None:
    with pytest.raises(DistributionSnapshotError) as exc:
        compute_distribution_snapshot(_visits(), fanout_columns=bad)  # type: ignore[arg-type]
    assert exc.value.code == reasons.quality.FANOUT_COLUMN_UNKNOWN


def test_bare_string_of_real_column_names_still_raises() -> None:
    df = pd.DataFrame({"a": [1, 1, 2], "b": [3, 4, 4], "ab": [5, 5, 5]})
    # Every character of "ab" is a real column, so only the str guard can refuse it.
    with pytest.raises(DistributionSnapshotError) as exc:
        compute_distribution_snapshot(df, fanout_columns="ab")  # type: ignore[arg-type]
    assert exc.value.code == reasons.quality.FANOUT_COLUMN_UNKNOWN
    assert "bare string" in str(exc.value)


def test_existing_readers_accept_a_snapshot_with_fanout() -> None:
    src = compute_distribution_snapshot(_visits(), fanout_columns=["patient_id", "amount"])
    spec = load_spec(
        {"name": "amount", "type": "statistical", "source_column": "amount"}, snapshot=src
    )
    assert sample_column(spec, 5, col_seed=3)
    out = compute_distribution_snapshot(_visits())
    report = compute_fidelity(src, out)
    assert report["overall_score"] is not None


def test_compat_corpus_snapshot_loads_with_fanout_added() -> None:
    path = (
        Path(__file__).parents[2]
        / "integration"
        / "compat_corpus"
        / "distribution-snapshot-v1__synthetic.json"
    )
    snap = json.loads(path.read_text(encoding="utf-8"))
    for col in snap["columns"].values():
        col["fanout"] = {"rows": 2, "keys": 1, "degrees": [[2, 1]]}
    for name in snap["columns"]:
        spec = fanout_spec_from_snapshot(snap, name, digest="00" * 32)
        assert spec.rows == 2
    # The older readers must still take the very same snapshot, fan-out included.
    for name, col in snap["columns"].items():
        cfg = {"name": name, "type": "statistical", "source_column": name}
        if col["kind"] == "categorical":
            cfg["allow_real_categories"] = True
        loaded = load_spec(cfg, snapshot=snap)
        assert len(sample_column(loaded, 4, col_seed=1)) == 4


def test_writer_then_decoder_roundtrip() -> None:
    snap = compute_distribution_snapshot(_visits(), fanout_columns=["patient_id"])
    spec = fanout_spec_from_snapshot(snap, "patient_id", digest="cd" * 32)
    assert (spec.rows, spec.keys) == (20, 8)
    assert spec.degrees == ((1, 5), (2, 1), (5, 1), (8, 1))


def test_snapshot_module_never_imports_the_private_module() -> None:
    src = Path(__file__).parents[3] / "src" / "decoy_engine" / "quality" / "snapshot.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "statistical._fanout" not in node.module
            if node.module.endswith("statistical"):
                assert all(a.name != "_fanout" for a in node.names)
        if isinstance(node, ast.Import):
            assert all("_fanout" not in a.name for a in node.names)
