"""Process-scoped advisory destination lock (POSIX `flock`).

The lock file is stable and never unlinked while contenders may hold it; the kernel
releases it on process death, so there is no PID-file guessing. Where `flock` is not
available or the lock file cannot be created, publication is *unsupported* and must be
refused before any staging write: there is no lock-free fallback (A13).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import TracebackType

from decoy_engine.errors import DecoyError
from decoy_engine.publication._tokens import os_error_label

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]


class LockUnavailableError(DecoyError):
    """No qualified locking on this destination; the caller must refuse, not downgrade."""


class LockTimeoutError(LockUnavailableError):
    """Locking works but a peer holds the lock past the timeout (contention, not support)."""


def lock_path_for(destination: Path) -> Path:
    return destination.parent / f".{destination.name}.decoy-lock"


class DestinationLock:
    def __init__(self, destination: Path, *, timeout_s: float = 60.0) -> None:
        self._path = lock_path_for(destination)
        self._timeout_s = timeout_s
        self._fd: int | None = None

    def __enter__(self) -> DestinationLock:
        if fcntl is None:
            raise LockUnavailableError("advisory locking is not available on this platform")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise LockUnavailableError(f"cannot open lock file ({os_error_label(exc)})") from exc
        deadline = time.monotonic() + self._timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockTimeoutError("timed out waiting for the destination lock") from None
                time.sleep(0.02)
            except OSError as exc:
                os.close(fd)
                raise LockUnavailableError(
                    f"filesystem does not support flock ({os_error_label(exc)})"
                ) from exc
        self._fd = fd
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
