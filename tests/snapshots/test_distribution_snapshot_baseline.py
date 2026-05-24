"""Snapshot harness for distribution snapshots (V2 Phase 3 D1a).

Mirror of test_validator_baseline.py for the distribution snapshot
foundation. Lands as Sprint D1a so any later D1/D2 change that
accidentally shifts a bin edge, quantile rounding, or kind assignment
fails CI with a clear "snapshot drift" message instead of silently
re-baselining downstream fidelity scores.

Per engineering-best-practices §1.1: snapshot before extraction.
Distribution snapshots are the input to every D1c fidelity score
landed later, so drift here propagates everywhere -- pin the digests
now while the surface is small.

What's hashed:
  - The full snapshot dict, with sort_keys=True so dict key ordering
    is canonical regardless of insertion order.

The hash is a single SHA-256 over the canonical-form JSON. Fixtures
exercise each kind branch (numeric / categorical / datetime / freetext
/ empty / bool) plus a joint pair, so a regression in any single code
path lights up exactly one fixture.

Adding a fixture:
  1. Define the frame builder in FIXTURES below.
  2. Run: UPDATE_SNAPSHOTS=1 pytest \
        tests/snapshots/test_distribution_snapshot_baseline.py
  3. Inspect the generated golden file and commit it.

Updating an existing fixture's expected output:
  Only do this when you have intentionally changed snapshot behavior.
  Re-run with UPDATE_SNAPSHOTS=1 and check in the new goldens. The
  commit MUST explain why the snapshot shape legitimately changed.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from decoy_engine.quality.snapshot import compute_distribution_snapshot

GOLDEN = Path(__file__).parent / "golden" / "distribution_snapshot"


def _numeric_only() -> pd.DataFrame:
    # Deterministic series via fixed seed; equal-width binning means the
    # exact distribution shape is captured in the digest.
    rng = np.random.default_rng(seed=1)
    return pd.DataFrame({"x": rng.normal(loc=10.0, scale=2.0, size=100)})


def _categorical_only() -> pd.DataFrame:
    return pd.DataFrame({"state": ["CA", "NY", "TX", "CA", "NY", "CA", "WA", "OR", "CA", "TX"]})


def _datetime_only() -> pd.DataFrame:
    # Year range spans three years so year_bins has multiple entries.
    return pd.DataFrame(
        {
            "joined": pd.to_datetime(
                [
                    "2022-01-15",
                    "2022-06-30",
                    "2023-03-12",
                    "2023-09-05",
                    "2024-02-28",
                    "2024-11-11",
                ]
            )
        }
    )


def _freetext_only() -> pd.DataFrame:
    # >30 distinct values forces freetext kind via the cardinality cap.
    return pd.DataFrame({"notes": [f"note number {i} with text" for i in range(50)]})


def _empty_column_only() -> pd.DataFrame:
    return pd.DataFrame({"x": [None, None, None, None]}, dtype="object")


def _bool_only() -> pd.DataFrame:
    return pd.DataFrame({"active": [True, False, True, True, False, True]})


def _mixed_with_joint() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "state": ["CA", "CA", "NY", "NY", "TX", "TX"] * 5,
            "active": [True, False, True, False, True, False] * 5,
            "salary": [50_000, 60_000, 70_000, 55_000, 65_000, 75_000] * 5,
        }
    )


FIXTURES: dict[str, tuple[Callable[[], pd.DataFrame], dict[str, object]]] = {
    "numeric_only": (_numeric_only, {}),
    "categorical_only": (_categorical_only, {}),
    "datetime_only": (_datetime_only, {}),
    "freetext_only": (_freetext_only, {}),
    "empty_column_only": (_empty_column_only, {}),
    "bool_only": (_bool_only, {}),
    "mixed_with_joint": (
        _mixed_with_joint,
        {"joint_columns": [("state", "active")]},
    ),
}


def _digest(snapshot: dict[str, object]) -> str:
    blob = json.dumps(snapshot, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _golden_path(name: str) -> Path:
    return GOLDEN / f"{name}.sha256"


@pytest.mark.parametrize("name", sorted(FIXTURES.keys()))
def test_distribution_snapshot_baseline(name: str) -> None:
    builder, kwargs = FIXTURES[name]
    df = builder()
    # mypy: kwargs entries are typed object; cast to the signature shape.
    joint_columns = kwargs.get("joint_columns")
    if joint_columns is not None and not isinstance(joint_columns, Sequence):
        pytest.fail(f"Fixture {name} has malformed joint_columns kwarg")
    snapshot = compute_distribution_snapshot(
        df,
        joint_columns=joint_columns,  # type: ignore[arg-type]
    )
    digest = _digest(snapshot)

    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        GOLDEN.mkdir(parents=True, exist_ok=True)
        _golden_path(name).write_text(digest + "\n", encoding="utf-8")
        return

    path = _golden_path(name)
    if not path.exists():
        pytest.fail(
            f"Missing golden for fixture {name!r}. "
            f"Run with UPDATE_SNAPSHOTS=1 to create it, then inspect "
            f"{path} before committing."
        )
    expected = path.read_text(encoding="utf-8").strip()
    if expected != digest:
        pytest.fail(
            f"Distribution snapshot drift on fixture {name!r}\n"
            f"  expected digest: {expected}\n"
            f"  actual digest:   {digest}\n"
            f"  actual payload:  {json.dumps(snapshot, indent=2, sort_keys=True)[:2000]}"
        )
