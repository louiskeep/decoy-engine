"""A10b: scans share no state, so concurrent scans equal their serial results."""

from __future__ import annotations

import threading

from decoy_engine.verification.canary import CanaryArtifactInput, CanarySpanReport
from tests.unit.verification.canary.test_scan_end_to_end import scan, strings

CELLS = ("AAAAAAAA-1", "AAAAAAAA-1", "BBBBBBBB-2")


def test_concurrent_scans_equal_serial() -> None:
    barrier = threading.Barrier(2, timeout=30)

    def run(canary: str, wait: bool) -> CanarySpanReport:
        def open_batches():  # type: ignore[no-untyped-def]
            if wait:  # both scans hold their patterns here, before either reads a cell
                barrier.wait()
            return iter([strings(*CELLS)])

        return scan([canary], [CanaryArtifactInput(0, None, open_batches)])

    serial = {c: run(c, False) for c in ("AAAAAAAA-1", "BBBBBBBB-2")}
    assert serial["AAAAAAAA-1"] != serial["BBBBBBBB-2"]
    results: dict[str, CanarySpanReport] = {}

    def worker(canary: str) -> None:
        results[canary] = run(canary, True)

    threads = [threading.Thread(target=worker, args=(c,)) for c in serial]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert results == serial
