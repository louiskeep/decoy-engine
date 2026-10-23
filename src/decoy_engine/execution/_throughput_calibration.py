"""Throughput calibration for the job-time estimator (job-time-estimator Slice 1).

A deliberately COARSE, host-caveated runtime model: `runtime ≈ work / throughput`,
returned as a wide confidence BAND, never a false-precise point (per the roadmap
"confidence band not a false-precise number" and the §CAP advisory lock). The
estimator composes this with the existing `_mem_estimate` byte model and the route
decision; nothing here gates a job.

Two regimes, because the bottleneck differs:

- **Mask routes** are byte-bound: `runtime ≈ raw_data_bytes / bytes_per_s(route)`.
  Reuses the same byte basis the memory estimator already computes
  (`_mem_estimate.raw_data_bytes`).
- **Generation** is row/provider-bound (per-row provider + HKDF, not byte volume):
  `runtime ≈ rows / rows_per_s`.

Calibration anchors (reference-host measurements; cited to their bench records):

- native mask route ~45 MB/s: b6a `native_2000000` = 160,000,000 source bytes at
  resident wall_s p50 3.539 s (docs/records/b6a-bench-2026-10-02/b6a-bench-merged.json)
  -> 45.2 MB/s. b6b corroborates (~33 MB/s on a cold warmup trial,
  docs/records/b6b-bench-2026-10-03/results.jsonl).
- pure generation ~14k rows/s: the windowed_date generator floor
  (docs/records/2026-10-18-f4a-gen-capacity.md: "Pure generation throughput is
  therefore about 14k rows/s").

Everything else (full_frame / sequential / out_of_core mask routes) has no clean
reference-host byte-throughput anchor yet, so it is priced CONSERVATIVELY off the
native anchor with a `low` confidence tag and a wide band, rather than a guess.
These numbers are host-dependent (CPU/cores): the band is wide and the result
carries a `host-calibrated, advisory` caveat; a dedicated per-route throughput
bench (plan Slice 1 follow-up) would tighten them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Confidence = Literal["low", "medium"]

_MIB = 1024 * 1024

# Reference-host bytes/s per mask route. Only `native`/`streaming` is directly
# measured; the oracle-ish routes are priced conservatively (slower) off it until
# their own bench exists. Keep this the single source of truth for mask speed.
_MASK_BYTES_PER_S: dict[str, float] = {
    "native": 45.0 * _MIB,  # b6a native_2000000 (measured)
    "streaming": 45.0 * _MIB,  # same compiled kernel path, streamed sink
    "out_of_core": 20.0 * _MIB,  # conservative: spill/join overhead, not yet benched
    "full_frame": 15.0 * _MIB,  # conservative: pandas oracle, per-cell heavy
    "sequential": 15.0 * _MIB,  # conservative: bounded pandas orchestration
}
# Routes we have a direct measurement for get a tighter band; the rest are `low`.
_MEASURED_MASK_ROUTES = frozenset({"native", "streaming"})

# Pure-generation rows/s (provider-bound). f4a windowed_date floor.
_GENERATION_ROWS_PER_S = 14_000.0

# Band half-widths (multiplicative). A measured anchor still varies with host and
# shape, so even `medium` is deliberately wide; `low` is wider still.
_BAND_FACTOR: dict[Confidence, float] = {"medium": 1.6, "low": 2.5}


@dataclass(frozen=True)
class RuntimeBand:
    """An advisory runtime estimate as a band, never a point.

    `low_seconds`/`high_seconds` bound the estimate; `confidence` is `medium` only
    for a directly-measured route, else `low`; `basis` names what drove it. Callers
    render "~X-Y" and the host/advisory caveat, and NEVER gate on it.
    """

    low_seconds: float
    high_seconds: float
    confidence: Confidence
    basis: str


def _band(point_seconds: float, confidence: Confidence, basis: str) -> RuntimeBand:
    factor = _BAND_FACTOR[confidence]
    # Floor the low end at a small constant: fixed per-job overhead (process start,
    # profile, compile) dominates tiny jobs and the model does not capture it.
    low = max(point_seconds / factor, 0.5)
    high = max(point_seconds * factor, low)
    return RuntimeBand(low_seconds=low, high_seconds=high, confidence=confidence, basis=basis)


def estimate_mask_runtime_band(raw_data_bytes: int, route: str) -> RuntimeBand:
    """Band for a mask job of `raw_data_bytes` on `route`. Unknown route -> the
    slowest calibrated rate at `low` confidence (never raises)."""
    bytes_per_s = _MASK_BYTES_PER_S.get(route)
    if bytes_per_s is None:
        bytes_per_s = min(_MASK_BYTES_PER_S.values())
        confidence: Confidence = "low"
        basis = f"uncalibrated route {route!r}: priced at the slowest known rate"
    else:
        confidence = "medium" if route in _MEASURED_MASK_ROUTES else "low"
        basis = f"mask route {route!r} at {bytes_per_s / _MIB:.0f} MB/s (reference host)"
    return _band(raw_data_bytes / bytes_per_s, confidence, basis)


def estimate_generation_runtime_band(rows: int) -> RuntimeBand:
    """Band for generating `rows` rows (provider-bound). `low` confidence: the
    per-row cost swings widely by provider/strategy (pooled vs live vs windowed)."""
    return _band(
        rows / _GENERATION_ROWS_PER_S,
        "low",
        f"generation at ~{_GENERATION_ROWS_PER_S / 1000:.0f}k rows/s (windowed_date floor)",
    )


__all__ = [
    "RuntimeBand",
    "estimate_generation_runtime_band",
    "estimate_mask_runtime_band",
]
