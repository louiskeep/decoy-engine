"""The 7-check Mimesis/Faker parity suite (engine-v2 S7).

The adoption decision gate. A provider is eligible for Mimesis adoption iff
checks 1-6 pass AND the benchmark ratio (Mimesis time / Faker time over the
sample) is < 0.20 (Mimesis is 5x+ faster). Check 7 (distribution sanity)
failure with otherwise-clean results goes to the PO for a manual per-provider
call (S7 spec §3).

Each check is a module-level pure function of the two sample lists so a test
can force a regression in any one of them; `run_parity_suite` generates the
real samples, times both backends, and orchestrates the seven.

Determinism (check 6) is the hard, non-negotiable gate: a provider whose
seeded `generate_batch` is not byte-identical across runs makes the pool
identity a lie and is rejected regardless of speed (S7 spec §2 / Risks).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from decoy_engine.providers_v2._adapter import ProviderSpec
from decoy_engine.providers_v2._faker_adapter import FakerAdapter
from decoy_engine.providers_v2.mimesis._adapter import (
    _SUPPORTED_LOCALES,
    MimesisAdapter,
)

# Provider -> loose format regex, enforced by check 5 where applicable. Absent
# means "no format constraint"; the check passes with a note.
_FORMAT_REGEX: dict[str, str] = {
    "person_email": r".+@.+\..+",
    "address_zip": r"\d{3,}",
}

_ADOPTION_RATIO_THRESHOLD = 0.20
_LENGTH_TOLERANCE = 0.20
_DISTRIBUTION_TOLERANCE = 0.30

CHECKS: tuple[str, ...] = (
    "dtype",
    "null",
    "locale",
    "length",
    "format",
    "determinism",
    "distribution",
)


@dataclass(frozen=True)
class ParityCheckResult:
    """One parity-check outcome for a provider."""

    provider: str
    check: str
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)
    benchmark_ratio: float | None = None


def _lengths(samples: Sequence[Any]) -> list[int]:
    return [len(str(v)) for v in samples]


def check_dtype(
    mimesis_samples: Sequence[Any], faker_samples: Sequence[Any]
) -> tuple[bool, dict[str, Any]]:
    m_types = {type(v).__name__ for v in mimesis_samples}
    f_types = {type(v).__name__ for v in faker_samples}
    return m_types == f_types, {"mimesis_types": sorted(m_types), "faker_types": sorted(f_types)}


def check_null(
    mimesis_samples: Sequence[Any], faker_samples: Sequence[Any]
) -> tuple[bool, dict[str, Any]]:
    m_nulls = sum(1 for v in mimesis_samples if v is None)
    f_nulls = sum(1 for v in faker_samples if v is None)
    return m_nulls == f_nulls, {"mimesis_nulls": m_nulls, "faker_nulls": f_nulls}


def check_locale(locale: str) -> tuple[bool, dict[str, Any]]:
    supported = locale in _SUPPORTED_LOCALES
    return supported, {"locale": locale, "mimesis_supported": list(_SUPPORTED_LOCALES)}


def check_length(
    mimesis_samples: Sequence[Any], faker_samples: Sequence[Any]
) -> tuple[bool, dict[str, Any]]:
    m, f = _lengths(mimesis_samples), _lengths(faker_samples)
    f_mean = sum(f) / len(f) if f else 0.0
    m_mean = sum(m) / len(m) if m else 0.0
    f_max, m_max = (max(f) if f else 0), (max(m) if m else 0)
    mean_ok = f_mean == 0 or abs(m_mean - f_mean) <= _LENGTH_TOLERANCE * f_mean
    max_ok = f_max == 0 or abs(m_max - f_max) <= _LENGTH_TOLERANCE * f_max
    detail = {
        "mimesis_mean": m_mean,
        "faker_mean": f_mean,
        "mimesis_max": m_max,
        "faker_max": f_max,
    }
    return mean_ok and max_ok, detail


def check_format(provider: str, mimesis_samples: Sequence[Any]) -> tuple[bool, dict[str, Any]]:
    import re

    pattern = _FORMAT_REGEX.get(provider)
    if pattern is None:
        return True, {"format_regex": None, "note": "no format constraint for this provider"}
    rx = re.compile(pattern)
    misses = [v for v in mimesis_samples if not rx.fullmatch(str(v))]
    return len(misses) == 0, {"format_regex": pattern, "miss_count": len(misses)}


def check_determinism(
    provider: str, locale: str, seed: bytes = b"\x00\x00\x00\x00\x00\x00\x00\x2a"
) -> tuple[bool, dict[str, Any]]:
    """Parity check 6: seeded `generate_batch` reproducibility.

    This is the IN-PROCESS leg (two adapters in one interpreter). The
    cross-process leg (the real hard gate) is proven separately in
    test_mimesis_adapter.py::test_seeded_batch_reproducible_cross_process.
    """
    spec = ProviderSpec(locale=locale, deterministic=False, namespace=None, seed=seed)
    run1 = MimesisAdapter(locale=locale).generate_batch(provider, spec=spec, count=64)
    run2 = MimesisAdapter(locale=locale).generate_batch(provider, spec=spec, count=64)
    return list(run1) == list(run2), {"sample_count": 64}


def check_distribution(
    mimesis_samples: Sequence[Any], faker_samples: Sequence[Any]
) -> tuple[bool, dict[str, Any]]:
    m_distinct = len({str(v) for v in mimesis_samples})
    f_distinct = len({str(v) for v in faker_samples})
    ok = f_distinct == 0 or abs(m_distinct - f_distinct) <= _DISTRIBUTION_TOLERANCE * f_distinct
    return ok, {"mimesis_distinct": m_distinct, "faker_distinct": f_distinct}


def run_parity_suite(
    provider: str, locale: str = "en_US", n: int = 10_000
) -> list[ParityCheckResult]:
    """Run the 7-check parity suite for `provider`.

    Returns 7 results. The provider is adoption-eligible iff checks 1-6 are
    `passed=True` AND `benchmark_ratio < 0.20`; check 7 failure triggers a
    manual PO review.
    """
    spec = ProviderSpec(locale=locale, deterministic=False, namespace=None, seed=None)
    faker = FakerAdapter(locale=locale)
    mimesis = MimesisAdapter(locale=locale)

    t0 = time.perf_counter()
    faker_samples = list(faker.generate_batch(provider, spec=spec, count=n))
    faker_time = time.perf_counter() - t0

    t0 = time.perf_counter()
    mimesis_samples = list(mimesis.generate_batch(provider, spec=spec, count=n))
    mimesis_time = time.perf_counter() - t0

    ratio = (mimesis_time / faker_time) if faker_time > 0 else None

    dtype_ok, dtype_d = check_dtype(mimesis_samples, faker_samples)
    null_ok, null_d = check_null(mimesis_samples, faker_samples)
    locale_ok, locale_d = check_locale(locale)
    length_ok, length_d = check_length(mimesis_samples, faker_samples)
    format_ok, format_d = check_format(provider, mimesis_samples)
    det_ok, det_d = check_determinism(provider, locale)
    dist_ok, dist_d = check_distribution(mimesis_samples, faker_samples)

    return [
        ParityCheckResult(provider, "dtype", dtype_ok, dtype_d, ratio),
        ParityCheckResult(provider, "null", null_ok, null_d, ratio),
        ParityCheckResult(provider, "locale", locale_ok, locale_d, ratio),
        ParityCheckResult(provider, "length", length_ok, length_d, ratio),
        ParityCheckResult(provider, "format", format_ok, format_d, ratio),
        ParityCheckResult(provider, "determinism", det_ok, det_d, ratio),
        ParityCheckResult(provider, "distribution", dist_ok, dist_d, ratio),
    ]


def is_adoptable(results: Sequence[ParityCheckResult]) -> bool:
    """True iff checks 1-6 (dtype, null, locale, length, format, determinism)
    pass AND the benchmark ratio is < 0.20. Check 7 (distribution) is advisory
    (manual PO review on failure), so it does not gate this predicate.
    """
    by_check = {r.check: r for r in results}
    gating = ("dtype", "null", "locale", "length", "format", "determinism")
    if not all(by_check[c].passed for c in gating if c in by_check):
        return False
    ratios = [r.benchmark_ratio for r in results if r.benchmark_ratio is not None]
    # max(ratios) so the predicate is order-independent (results all carry the
    # same overall ratio today, but a public predicate should not depend on
    # which result happens to be first).
    return bool(ratios) and max(ratios) < _ADOPTION_RATIO_THRESHOLD
