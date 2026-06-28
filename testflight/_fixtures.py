"""Seeded fixture-generator helpers for the test-flight suite (Phase 0 stubs).

Each job supplies a fixture.py (under jobs/<job>/fixture.py) with job-specific
builders. This module provides shared utilities those builders call:

- `make_rng(seed)`: a seeded numpy Generator for reproducible data.
- `make_faker(seed)`: a seeded Faker instance for reproducible fake PII.
- `compute_fingerprint(df)`: SHA-256 of a canonicalized pandas DataFrame.
- `verify_fingerprint(df, expected)`: assert fingerprint matches committed baseline.

Phase 0: stubs with signatures and docstrings. Phase 2 fills in real bodies
alongside the Job A / B / C fixture generators.

Design principles:
- Generators are seeded via manifest.seed so output is byte-identical under
  pinned faker / numpy versions (already pinned in uv.lock).
- Edge cases (restricted ZIP3 rows, invalid-Luhn SSNs, orphan rows, sentinel
  PII strings) are injected with EXACT planted counts so the invariant
  assertions compare against known integers, not heuristics.
- Correlated columns (claim_amount correlated with ICD chapter, salary with
  department) are generated deliberately so the correlation-preservation
  invariant is non-trivial.
"""

from __future__ import annotations

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

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: make_rng")


def make_faker(seed: int) -> Any:
    """Return a Faker instance seeded with `seed`.

    Uses faker.Faker(seed=seed) with the pinned faker==40.23.0 version from
    uv.lock. Faker's name / data pools are semver-unstable, so the pinned
    version is part of the determinism guarantee.

    Args:
        seed: Integer seed, typically manifest.seed.

    Returns:
        faker.Faker instance.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: make_faker")


def compute_fingerprint(df: Any) -> str:
    """Compute a SHA-256 fingerprint of a canonicalized pandas DataFrame.

    Serialises the DataFrame to a canonical byte representation (sorted columns,
    deterministic dtypes, UTF-8 CSV) and returns the hex digest. Used to detect
    accidental fixture drift after a faker / numpy version bump.

    Args:
        df: pandas DataFrame of source fixture data.

    Returns:
        Hex-encoded SHA-256 digest string.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: compute_fingerprint")


def verify_fingerprint(df: Any, expected_fingerprint: str, label: str = "") -> None:
    """Assert that df's fingerprint matches the committed baseline.

    Calls compute_fingerprint(df) and compares against expected_fingerprint.
    On mismatch raises RuntimeError with a clear "fixtures drifted, re-baseline
    deliberately" message so accidental drift is noisy and intentional re-baselining
    is an explicit reviewer-visible step.

    Args:
        df: pandas DataFrame of source fixture data.
        expected_fingerprint: Committed baseline hex digest.
        label: Descriptive label (e.g. table name) for the error message.

    Raises:
        RuntimeError: If the fingerprint does not match the committed baseline.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: verify_fingerprint")
