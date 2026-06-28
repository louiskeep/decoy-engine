"""Seeded fixture-generator helpers for the test-flight suite.

Each job supplies a fixture.py (under jobs/<job>/fixture.py) with job-specific
builders. This module provides shared utilities those builders call:

- `make_rng(seed)`: a seeded numpy Generator for reproducible data.
- `make_faker(seed)`: a seeded Faker instance for reproducible fake PII.
- `compute_fingerprint(df)`: SHA-256 of a canonicalized pandas DataFrame.
- `verify_fingerprint(df, expected)`: assert fingerprint matches committed baseline.

Design principles:
- Generators are seeded via manifest.seed so output is byte-identical under
  pinned faker / numpy versions (already pinned in uv.lock).
- Edge cases (restricted ZIP3 rows, invalid-Luhn rows, orphan rows, sentinel
  PII strings) are injected with EXACT planted counts so the invariant
  assertions compare against known integers, not heuristics.
- Correlated columns (claim_amount correlated with ICD chapter, plan_tier
  correlated with age) are generated deliberately so the correlation-
  preservation invariant is non-trivial.
"""

from __future__ import annotations

import hashlib
from typing import Any


def make_rng(seed: int) -> Any:
    """Return a numpy Generator seeded with `seed`.

    Uses numpy.random.default_rng so output is reproducible under the pinned
    numpy version in uv.lock. All fixture generators must call this rather than
    using a module-level global so each call is independently seeded.

    Args:
        seed: Integer seed, typically manifest.seed.

    Returns:
        numpy.random.Generator instance.
    """
    import numpy as np

    return np.random.default_rng(seed)


def make_faker(seed: int) -> Any:
    """Return a Faker instance seeded with `seed`.

    Uses faker.Faker(seed=seed) with the pinned faker version from uv.lock.
    Faker's name / data pools are semver-unstable, so the pinned version is
    part of the determinism guarantee.

    Args:
        seed: Integer seed, typically manifest.seed.

    Returns:
        faker.Faker instance.
    """
    import faker as faker_lib

    fkr = faker_lib.Faker()
    faker_lib.Faker.seed(seed)
    return fkr


def compute_fingerprint(df: Any) -> str:
    """Compute a SHA-256 fingerprint of a canonicalized pandas DataFrame.

    Serialises the DataFrame to a canonical byte representation (sorted columns,
    deterministic dtypes, UTF-8 CSV) and returns the hex digest. Used to detect
    accidental fixture drift after a faker / numpy version bump.

    Args:
        df: pandas DataFrame of source fixture data.

    Returns:
        Hex-encoded SHA-256 digest string.
    """
    import io

    # Canonical form: sort columns alphabetically, reset index, convert to
    # CSV with explicit encoding and line terminator. This is deterministic
    # regardless of column insertion order and index state.
    sorted_df = df[sorted(df.columns)].reset_index(drop=True)
    buf = io.StringIO()
    sorted_df.to_csv(buf, index=False, lineterminator="\n")
    csv_bytes = buf.getvalue().encode("utf-8")
    return hashlib.sha256(csv_bytes).hexdigest()


def verify_fingerprint(df: Any, expected_fingerprint: str, label: str = "") -> None:
    """Assert that df's fingerprint matches the committed baseline.

    Calls compute_fingerprint(df) and compares against expected_fingerprint.
    On mismatch raises RuntimeError with a clear "fixtures changed, re-baseline
    deliberately" message so accidental drift is noisy and intentional
    re-baselining is an explicit reviewer-visible step.

    Args:
        df: pandas DataFrame of source fixture data.
        expected_fingerprint: Committed baseline hex digest.
        label: Descriptive label (e.g. table name) for the error message.

    Raises:
        RuntimeError: If the fingerprint does not match the committed baseline.
    """
    actual = compute_fingerprint(df)
    if actual != expected_fingerprint:
        tag = f"[{label}] " if label else ""
        raise RuntimeError(
            f"{tag}Source fixture fingerprint mismatch.\n"
            f"  Expected : {expected_fingerprint}\n"
            f"  Actual   : {actual}\n"
            "The fixture generator output has changed (likely a faker/numpy "
            "version bump or a deliberate fixture edit). Re-baseline deliberately "
            "by re-running the generator with --rebaseline and committing the "
            "new fingerprint in manifest.yaml."
        )
