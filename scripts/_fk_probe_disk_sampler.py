"""Background on-disk high-water-mark sampler for `fk_memory_probe.py`.

Split out of the probe to keep it under its module-size ceiling; behaviour is unchanged.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

_MIB = 1024 * 1024


class _DiskPeakSampler:
    """Polls total bytes under `root` on a background thread and keeps the max.

    `run_fk_out_of_core` wipes its own relation/join staging subtrees on
    success (`_runner.py`'s `finally` block), so the on-disk high-water mark
    cannot be read after the run, only during it. This is the same
    high-water-mark-by-polling approach as `ru_maxrss` for memory, but with a
    coarser, explicit sampling interval: true peak disk usage could fall
    strictly between two samples and be missed (most exposed by a very short
    burst write/delete inside one polling window). Documented honestly per the
    sprint brief rather than claimed as an exact measurement.
    """

    def __init__(self, root: Path, interval_s: float = 0.1) -> None:
        self._root = root
        self._interval = interval_s
        self._stop = threading.Event()
        self._peak_bytes = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._sample()  # catch a peak that landed after the last loop wakeup

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(self._interval)

    def _sample(self) -> None:
        total = 0
        if self._root.exists():
            for dirpath, _dirnames, filenames in os.walk(self._root):
                for name in filenames:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, name))
                    except OSError:
                        pass  # file evicted between listdir and stat; not the peak
        self._peak_bytes = max(self._peak_bytes, total)

    @property
    def peak_mb(self) -> float:
        return self._peak_bytes / _MIB
