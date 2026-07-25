"""Shared adapter-conformance harness (engine-v2 S7).

Closes MEDIUM-ADAPTER-CONFORMANCE-1 from the S6 end-of-sprint review: one
parametrized harness that every concrete BackendAdapter family runs through
(FakerAdapter, the 5 S6 DecoyNative identifier adapters, and MimesisAdapter
when installed), asserting the S4 conformance surface: seed-stability,
format-correctness, and no-blocklist-leak. Centralizing it means a future
adapter added without going through the same gate is caught at test time.

Each family reaches reproducibility differently, so `ConformanceCase`
abstracts "produce a seed-stable sample batch from an 8-byte seed":
- Faker / Mimesis: a seeded `generate_batch` (non-deterministic providers; the
  seed makes the batch reproducible across independent adapter instances).
- S6 DecoyNative: deterministic `generate` over a fixed source-value list
  (reproducible across runs for the same seed + sources).

`test_adapter_conformance.py` builds the case list and parametrizes the three
assertions over it. The harness is a plain module (underscore-prefixed) so
pytest does not collect it as tests.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ConformanceCase:
    """One adapter+provider under the shared conformance harness."""

    label: str
    make_samples: Callable[[bytes], Sequence[Any]]
    validate: Callable[[Any], bool] | None = None
    has_blocklist: bool = False
    sample_count: int = 64


def assert_seed_stable(case: ConformanceCase) -> None:
    """Same 8-byte seed -> byte-identical sample batch across two runs."""
    seed = (0x0123456789ABCDEF).to_bytes(8, "big")
    first = list(case.make_samples(seed))
    second = list(case.make_samples(seed))
    assert len(first) == case.sample_count, (
        f"{case.label}: expected {case.sample_count} samples, got {len(first)}"
    )
    assert first == second, f"{case.label}: seeded output not reproducible across runs"


def assert_format_correct(case: ConformanceCase) -> None:
    """Every sampled value satisfies the provider's declared validator."""
    if case.validate is None:
        return
    seed = (42).to_bytes(8, "big")
    bad = [v for v in case.make_samples(seed) if not case.validate(v)]
    assert not bad, f"{case.label}: format-correctness failed for {bad[:3]!r}"


def assert_no_blocklist_leak(case: ConformanceCase) -> None:
    """For blocklist-bearing adapters, no sampled value is blocklist-invalid.

    The S6 validators reject blocklisted values, so a clean validate pass over
    the sample is the no-leak assertion for those families.
    """
    if not case.has_blocklist or case.validate is None:
        return
    seed = (7).to_bytes(8, "big")
    leaks = [v for v in case.make_samples(seed) if not case.validate(v)]
    assert not leaks, f"{case.label}: blocklist leak: {leaks[:3]!r}"
