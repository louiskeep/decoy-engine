"""Per-column snapshot harness for the graph runner.

Lands before V2.0-A.1 starts so the runner-decomposition sub-milestones
have a regression net: each extraction PR runs this suite and must
produce identical per-column digests on the canonical fixtures. Drift
in any column digest blocks the merge.

Per Dennis's resolved question #7: per-column SHA-256 digest is the
right granularity. Per-row is too noisy at 10M rows; full-table
digest hides where the regression is. Per-column points at the
column whose values changed.

Adding a fixture:
  1. Define the graph config in FIXTURES below (a Python dict; the
     harness converts to YAML at runtime).
  2. Run: UPDATE_SNAPSHOTS=1 pytest tests/snapshots/test_runner_baseline.py
  3. Inspect the generated golden files under tests/snapshots/golden/
     and commit them alongside the fixture.

Removing a fixture:
  1. Delete the FIXTURES entry.
  2. Delete the matching directory under tests/snapshots/golden/.

Updating an existing fixture's expected output:
  Only do this when you have intentionally changed the engine's
  output. Re-run with UPDATE_SNAPSHOTS=1 and check in the new
  goldens. The commit that does this MUST explain why the engine's
  output legitimately changed.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from decoy_engine import preview_graph

REPO_ROOT = Path(__file__).parents[2]
GOLDEN = Path(__file__).parent / "golden"
# Committed fixture under tests/fixtures/ (not tests/data/, which is gitignored).
SAMPLE_CSV = REPO_ROOT / "tests" / "fixtures" / "sample.csv"


def _fixture(target_node: str, columns: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Helper: produce a single-source / single-mask / no-sink graph
    targeting the mask op. preview_graph reads from the target node
    without writing to disk, so the harness needs no cleanup.

    Sample CSV path is injected at runtime via SAMPLE_CSV (absolute,
    POSIX-formatted so it survives the YAML round-trip on Windows).
    """
    return {
        "mode": "graph",
        "nodes": [
            {
                "id": "src",
                "kind": "source.file",
                "config": {"path": SAMPLE_CSV.as_posix(), "format": "csv"},
            },
            {
                "id": target_node,
                "kind": "mask",
                "config": {"columns": columns},
            },
        ],
        "edges": [{"from": "src", "to": target_node}],
    }


# Each fixture exercises a different mask-strategy combination so a
# subtle regression in any strategy surfaces here. Add fixtures as
# V2.0-A surfaces additional risks worth pinning.
FIXTURES: dict[str, dict[str, Any]] = {
    # Passthrough + hash on multiple columns. Catches: hash inner-loop
    # determinism, column ordering preservation, dtype handling.
    "mask_hash_only": _fixture(
        "mask",
        {
            "customer_id": {"strategy": "passthrough"},
            "first_name": {"strategy": "hash"},
            "last_name": {"strategy": "hash"},
            "ssn": {"strategy": "hash"},
        },
    ),
    # Redact strategy. Catches: fixed-string replacement, null
    # handling.
    "mask_redact": _fixture(
        "mask",
        {
            "customer_id": {"strategy": "passthrough"},
            "address": {"strategy": "redact", "redact_with": "CONFIDENTIAL"},
            "phone": {"strategy": "redact", "redact_with": "REDACTED"},
        },
    ),
    # Faker (seeded determinism). Catches: provider dispatch, seed
    # threading, locale fallback.
    "mask_faker_seeded": _fixture(
        "mask",
        {
            "customer_id": {"strategy": "passthrough"},
            "first_name": {"strategy": "faker", "faker_type": "first_name"},
            "last_name": {"strategy": "faker", "faker_type": "last_name"},
            "email": {"strategy": "faker", "faker_type": "email"},
        },
    ),
}


def _per_column_digest(columns: list[str], rows: list[list[Any]]) -> dict[str, str]:
    """Compute SHA-256 digest of each column's cell values in row order.

    Format: each cell is `repr(value)` followed by a 0x1f unit separator,
    so ``[1, 23]`` and ``[12, 3]`` never collide on the same column.
    repr() preserves the type distinction (str vs int vs None) without
    needing a custom encoder. Stable across Python 3.10+.
    """
    digests: dict[str, str] = {}
    for i, col in enumerate(columns):
        h = hashlib.sha256()
        for row in rows:
            h.update(repr(row[i]).encode("utf-8"))
            h.update(b"\x1f")
        digests[col] = h.hexdigest()
    return digests


@pytest.fixture(scope="module")
def _check_sample_csv_present() -> None:
    """Skip the snapshot suite entirely if the input CSV is missing.

    Without this guard the failure surfaces as a confusing parse
    error inside preview_graph rather than a clear "fixture data not
    in this checkout" message.
    """
    if not SAMPLE_CSV.exists():
        pytest.skip(f"sample CSV not present at {SAMPLE_CSV}")


@pytest.mark.parametrize("fixture_id", sorted(FIXTURES))
def test_per_column_digest(
    fixture_id: str,
    _check_sample_csv_present: None,
) -> None:
    """Run the fixture through preview_graph and assert per-column
    digests match the stored goldens.

    Set ``UPDATE_SNAPSHOTS=1`` to (re)generate the golden files for
    the fixture. The test skips with a clear message in update mode
    so the run reports as updated, not silently green.
    """
    config = FIXTURES[fixture_id]
    yaml_text = yaml.safe_dump(config, sort_keys=False)
    result = preview_graph(yaml_text, node_id="mask", row_limit=1000)
    cols = result.get("columns") or []
    rows = result.get("rows") or []
    assert cols, f"{fixture_id}: preview returned no columns"
    assert rows, f"{fixture_id}: preview returned no rows"

    digests = _per_column_digest(cols, rows)
    fixture_dir = GOLDEN / fixture_id

    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        fixture_dir.mkdir(parents=True, exist_ok=True)
        for col, digest in digests.items():
            (fixture_dir / f"{col}.sha256").write_text(digest)
        # Also write a manifest listing every column captured, so a
        # reviewer can verify the golden set is complete.
        (fixture_dir / "MANIFEST.txt").write_text("\n".join(sorted(digests.keys())) + "\n")
        pytest.skip(
            f"goldens updated at {fixture_dir.relative_to(REPO_ROOT)}; "
            "review the diff before committing"
        )

    if not fixture_dir.exists():
        pytest.skip(
            f"no golden directory yet for {fixture_id}; run with UPDATE_SNAPSHOTS=1 to generate"
        )

    missing: list[str] = []
    drifted: list[str] = []
    for col, digest in digests.items():
        golden = fixture_dir / f"{col}.sha256"
        if not golden.exists():
            missing.append(col)
            continue
        if golden.read_text().strip() != digest:
            drifted.append(col)
    assert not missing, (
        f"{fixture_id}: missing golden for columns {missing}. "
        f"Either the engine grew a new output column (run with "
        f"UPDATE_SNAPSHOTS=1 to regenerate) or the schema regressed."
    )
    assert not drifted, (
        f"{fixture_id}: per-column digest drift on {drifted}. "
        f"The engine's output for these columns changed. If this is "
        f"intentional (mask-strategy fix, dtype change), regenerate "
        f"with UPDATE_SNAPSHOTS=1 and explain the change in the PR "
        f"description. If unintentional, this is a regression."
    )


def test_fixtures_cover_strategy_set() -> None:
    """Meta-test: every strategy referenced in FIXTURES should appear
    in at least one fixture's column config. Catches a fixture-set
    drift where a strategy is added to the engine but no snapshot
    exercises it.
    """
    used_strategies: set[str] = set()
    for fixture in FIXTURES.values():
        for node in fixture["nodes"]:
            if node["kind"] == "mask":
                for col_cfg in node["config"]["columns"].values():
                    used_strategies.add(col_cfg.get("strategy", ""))
    used_strategies.discard("")
    # If you add a strategy to the engine and want it covered here,
    # extend FIXTURES above (recommended), or remove the expected
    # strategy from this set (only if intentionally not snapshotted).
    expected_minimum = {"hash", "redact", "faker", "passthrough"}
    missing = expected_minimum - used_strategies
    assert not missing, (
        f"snapshot fixtures do not exercise {missing}. Add a fixture "
        f"to tests/snapshots/test_runner_baseline.py covering each "
        f"missing strategy."
    )
