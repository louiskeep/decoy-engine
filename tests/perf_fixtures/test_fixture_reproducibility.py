"""PERF.BASE.2: reproducibility tests for the fixture suite.

The contract: same engine version + same TierSpec.seed => byte-identical
Parquet. We verify by hashing the committed file against the recorded
sha256 in the companion ``fixture.yaml``. Re-generating from the seed
inside the test would be slow (medium is ~1 minute) and brittle
(temp-file write quirks), so we instead check that:

1. ``fixture.yaml`` records a sha256.
2. The committed Parquet hashes to that sha256.

A drift here means someone regenerated the fixture but didn't commit
the new ``fixture.yaml``, or someone hand-edited the Parquet. Either
way, the per-strategy benchmark deltas would be untrustworthy.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from .loaders import available_tiers, fixture_path

pytestmark = pytest.mark.perf


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.mark.parametrize("tier_name", available_tiers() or ["small"])
def test_parquet_matches_recorded_sha256(tier_name: str) -> None:
    parquet = fixture_path(tier_name)
    if not parquet.exists():
        pytest.skip(f"{tier_name} fixture not on disk")

    manifest_path = parquet.parent / "fixture.yaml"
    assert manifest_path.exists(), (
        f"{tier_name}: fixture.yaml missing next to {parquet.name}"
    )
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    recorded = manifest.get("parquet_sha256")
    assert recorded, f"{tier_name}: fixture.yaml has no parquet_sha256"

    actual = _sha256_of(parquet)
    assert actual == recorded, (
        f"{tier_name}: Parquet sha256 drift -- regenerate via "
        f"`python scripts/gen_perf_fixtures.py {tier_name} --force` "
        f"and commit both the Parquet and fixture.yaml together."
    )
