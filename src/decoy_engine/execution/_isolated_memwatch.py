"""Driver-side memory evidence for a capped isolated run.

A child that dies from a signal after native code hit its RLIMIT leaves no trace of why.
While the driver waits, this module samples the child's `/proc/<pid>/status` and keeps the
last value, the maximum and a freshness stamp. Evidence is a suspicion recorded beside an
unchanged outcome, never a verdict.

Source pattern: `VmData` (`mm->data_vm`) is what the kernel checks against RLIMIT_DATA
(mm/mmap.c); `VmSize` is what RLIMIT_AS bounds. Sampling is the same procfs read
`_isolated_common.peak_rss_mb` already uses, taken from outside the process.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from decoy_engine.errors import DecoyError

__all__ = [
    "MemoryEvidence",
    "MemorySampler",
    "MemorySnapshot",
    "build_evidence",
    "error_suffix",
    "evidence_of",
    "margin_bytes",
    "start_sampler",
    "suspected_pressure",
]

_logger = logging.getLogger(__name__)

THREAD_NAME = "decoy-memwatch"
SAMPLE_INTERVAL_S = 0.05
MAX_SAMPLE_AGE_MS = 250.0
JOIN_TIMEOUT_S = 0.5

STOP_REASONS = (
    "process_exited",
    "field_missing",
    "read_error",
    "stopped",
    "thread_start_failed",
)
BASIS_NEAR_CAP = "last sample within margin of cap"
BASIS_NO_SAMPLE = "no sample"
BASIS_NOT_NEAR = "last sample below margin of cap"
BASIS_STALE = "last sample too old"
BASIS_NOT_ABNORMAL = "run did not end abnormally"

_MIB = 1024 * 1024
_FIELDS = {"data": ("VmData",), "as": ("VmSize", "VmPeak")}

# Test seam: called with each successful live sample (bytes) as it is taken.
live_observer: Callable[[int], None] | None = None


class FieldMissing(DecoyError):  # noqa: N818 - a value-parse signal, not an error type
    """A requested `Vm*` line is absent or unparseable (a zombie drops them all)."""


def parse_vm_fields(text: str, names: tuple[str, ...]) -> dict[str, int]:
    out: dict[str, int] = {}
    wanted = {f"{n}:": n for n in names}
    for line in text.splitlines():
        key, _, rest = line.partition("\t")
        name = wanted.get(key)
        if name is None:
            continue
        parts = rest.split()
        try:
            out[name] = int(parts[0]) * 1024
        except (IndexError, ValueError):
            raise FieldMissing(name) from None
    for name in names:
        if name not in out:
            raise FieldMissing(name)
    return out


def read_vm_fields(pid: int, names: tuple[str, ...]) -> dict[str, int]:
    with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as fh:
        return parse_vm_fields(fh.read(), names)


@dataclass(frozen=True)
class MemorySnapshot:
    """One immutable view of what the sampler saw; the only thing ever published."""

    samples: int = 0
    first_at: float | None = None
    last_at: float | None = None
    last_bytes: int | None = None
    max_bytes: int | None = None
    peak_bytes: int | None = None
    stop_reason: str | None = None


@dataclass(frozen=True)
class MemoryEvidence:
    cap_mb: float
    kind: str
    last_mb: float | None
    peak_mb: float | None
    samples: int
    window_ms: float | None
    last_sample_age_ms: float | None
    stop_reason: str | None
    suspected_memory_pressure: bool
    basis: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cap_mb": self.cap_mb,
            "kind": self.kind,
            "last_mb": self.last_mb,
            "peak_mb": self.peak_mb,
            "samples": self.samples,
            "window_ms": self.window_ms,
            "last_sample_age_ms": self.last_sample_age_ms,
            "stop_reason": self.stop_reason,
            "suspected_memory_pressure": self.suspected_memory_pressure,
            "basis": self.basis,
        }


def margin_bytes(cap_bytes: int) -> int:
    """Positive, bounded: 10% of the cap, at least 64 MiB, at most half the cap."""
    return int(min(max(64 * _MIB, 0.10 * cap_bytes), 0.5 * cap_bytes))


def suspected_pressure(
    *, last_bytes: int | None, age_ms: float | None, cap_bytes: int, abnormal: bool
) -> bool:
    if not abnormal or last_bytes is None or age_ms is None:
        return False
    if age_ms > MAX_SAMPLE_AGE_MS:
        return False
    return last_bytes >= cap_bytes - margin_bytes(cap_bytes)


def _mb(value: int | None) -> float | None:
    return None if value is None else round(value / _MIB, 1)


def build_evidence(
    snap: MemorySnapshot, *, cap_bytes: int, kind: str, observed_at: float, abnormal: bool
) -> MemoryEvidence:
    """`observed_at` is the driver's clock when `communicate` returned; the flag rule needs
    the age of the last sample against that moment, not against the time of this call."""
    if snap.samples == 0 or snap.last_at is None or snap.first_at is None:
        return MemoryEvidence(
            cap_mb=_mb(cap_bytes) or 0.0,
            kind=kind,
            last_mb=None,
            peak_mb=None,
            samples=0,
            window_ms=None,
            last_sample_age_ms=None,
            stop_reason=snap.stop_reason,
            suspected_memory_pressure=False,
            basis=BASIS_NO_SAMPLE,
        )
    age_ms = round(max(0.0, (observed_at - snap.last_at) * 1000.0), 1)
    flag = suspected_pressure(
        last_bytes=snap.last_bytes, age_ms=age_ms, cap_bytes=cap_bytes, abnormal=abnormal
    )
    near = snap.last_bytes is not None and snap.last_bytes >= cap_bytes - margin_bytes(cap_bytes)
    if flag:
        basis = BASIS_NEAR_CAP
    elif not abnormal:
        basis = BASIS_NOT_ABNORMAL
    elif near:
        basis = BASIS_STALE
    else:
        basis = BASIS_NOT_NEAR
    peak = max(v for v in (snap.max_bytes, snap.peak_bytes) if v is not None)
    return MemoryEvidence(
        cap_mb=_mb(cap_bytes) or 0.0,
        kind=kind,
        last_mb=_mb(snap.last_bytes),
        peak_mb=_mb(peak),
        samples=snap.samples,
        window_ms=round((snap.last_at - snap.first_at) * 1000.0, 1),
        last_sample_age_ms=age_ms,
        stop_reason=snap.stop_reason,
        suspected_memory_pressure=flag,
        basis=basis,
    )


def error_suffix(ev: MemoryEvidence) -> str:
    """Sizes only; never any data value."""
    if ev.samples == 0 or ev.last_mb is None or ev.peak_mb is None:
        return ""
    return (
        f"; memory: last {ev.last_mb} MiB, peak {ev.peak_mb} MiB of {ev.cap_mb:g} MiB ({ev.kind})"
    )


@dataclass(frozen=True)
class Finished:
    """The stopped sampler's snapshot plus the driver's clock at termination."""

    snapshot: MemorySnapshot
    cap_bytes: int
    kind: str
    observed_at: float

    def evidence(self, *, abnormal: bool) -> MemoryEvidence:
        return build_evidence(
            self.snapshot,
            cap_bytes=self.cap_bytes,
            kind=self.kind,
            observed_at=self.observed_at,
            abnormal=abnormal,
        )


def evidence_of(finished: Finished | None, *, abnormal: bool) -> MemoryEvidence | None:
    return None if finished is None else finished.evidence(abnormal=abnormal)


class MemorySampler:
    """Daemon-thread sampler of one child's procfs memory field.

    It owns no pipes and never waits on or reaps the child. The reader runs without the
    publication lock, and `stop()` closes publication, so a read still in flight after the
    bounded join cannot change the snapshot the driver already took.
    """

    def __init__(
        self,
        pid: int,
        cap_bytes: int,
        kind: str,
        *,
        read: Callable[[int, tuple[str, ...]], dict[str, int]] = read_vm_fields,
        clock: Callable[[], float] = time.monotonic,
        interval_s: float = SAMPLE_INTERVAL_S,
        join_timeout_s: float = JOIN_TIMEOUT_S,
        observer: Callable[[int], None] | None = None,
    ) -> None:
        self.pid = pid
        self.cap_bytes = cap_bytes
        self.kind = kind
        self._names = _FIELDS[kind]
        self._read = read
        self._clock = clock
        self._interval_s = interval_s
        self._join_timeout_s = join_timeout_s
        self._observer = observer
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = False
        self._snap = MemorySnapshot()
        self._thread = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)

    def now(self) -> float:
        return self._clock()

    def snapshot(self) -> MemorySnapshot:
        with self._lock:
            return self._snap

    def step(self) -> bool:
        """Take one sample; False means sampling is over (the reason is published)."""
        try:
            fields = self._read(self.pid, self._names)
            value = fields[self._names[0]]
        except (FileNotFoundError, ProcessLookupError):
            return self._end("process_exited")
        except FieldMissing:
            return self._end("field_missing")
        except Exception:
            return self._end("read_error")
        at = self._clock()
        with self._lock:
            if self._closed:
                return False
            prev = self._snap
            vmpeak = fields.get("VmPeak")
            self._snap = MemorySnapshot(
                samples=prev.samples + 1,
                first_at=prev.first_at if prev.first_at is not None else at,
                last_at=at,
                last_bytes=value,
                max_bytes=value if prev.max_bytes is None else max(prev.max_bytes, value),
                peak_bytes=(
                    prev.peak_bytes
                    if vmpeak is None
                    else vmpeak
                    if prev.peak_bytes is None
                    else max(prev.peak_bytes, vmpeak)
                ),
                stop_reason=None,
            )
        observer = self._observer
        if observer is not None:
            try:
                observer(value)
            except Exception:
                _logger.debug("memwatch observer raised", exc_info=True)
        return True

    def _end(self, reason: str) -> bool:
        with self._lock:
            if not self._closed:
                self._snap = _with_reason(self._snap, reason)
        return False

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.step():
                return
            if self._stop.wait(self._interval_s):
                return

    def start(self) -> bool:
        try:
            self._thread.start()
        except RuntimeError:
            _logger.debug("memwatch thread did not start (%s)", STOP_REASONS[-1])
            return False
        return True

    def finish(self) -> Finished:
        # Read the clock before the bounded join so the join cannot age the last sample.
        observed_at = self._clock()
        return Finished(self.stop(), self.cap_bytes, self.kind, observed_at)

    def stop(self) -> MemorySnapshot:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(self._join_timeout_s)
        with self._lock:
            self._closed = True
            if self._snap.stop_reason is None:
                self._snap = _with_reason(self._snap, "stopped")
            return self._snap


def _with_reason(snap: MemorySnapshot, reason: str) -> MemorySnapshot:
    return MemorySnapshot(
        samples=snap.samples,
        first_at=snap.first_at,
        last_at=snap.last_at,
        last_bytes=snap.last_bytes,
        max_bytes=snap.max_bytes,
        peak_bytes=snap.peak_bytes,
        stop_reason=reason,
    )


def start_sampler(pid: int, cap_bytes: int, kind: str) -> MemorySampler | None:
    """A running sampler, or None when its thread could not start (the run goes on)."""
    sampler = MemorySampler(pid, cap_bytes, kind, observer=live_observer)
    return sampler if sampler.start() else None
