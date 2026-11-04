"""`write_private`: one choke point for files that hold secrets or engine copies of real data.

Pattern: the create-0600, fsync, atomic-replace recipe (`tempfile.mkstemp` creates 0600 with
`O_EXCL`; `os.fsync`; `os.replace`), run through the policy's output transaction with the
private `"isolated"` stage label so both the ambient and the strict policies honor it.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os

from decoy_engine.fileaccess._entry import current
from decoy_engine.fileaccess._types import FileRequest

_logger = logging.getLogger(__name__)


def write_private(request: FileRequest, data: bytes) -> None:
    """Write `data` to the request's locator as a 0600 file, replacing any existing one.

    Why: a file written at the umask and narrowed afterwards is readable by others in between.
    The private stage is 0600 at creation (and restored to 0600 under a umask that narrows
    it), is never written through a symlink or special file at the target, and is published
    with one rename, so the target is either the old file or the complete new one.

    Every `OSError` comes back as `OSError(errno, strerror)` with no filename and no chained
    original: the transaction's errors carry the pathname. File-access denials pass through.

    Named limit: the mode is guaranteed for the file this creates. A caller directory that is
    group- or world-searchable still exposes the file's name, not its content. A strict
    policy reaches this only where it allows `request.role` writes.
    """
    code = 0
    try:
        _write(request, data)
    except OSError as exc:
        code = exc.errno or errno.EIO
    if code:  # raised outside the handler, so the pathname-bearing original never rides
        raise OSError(code, os.strerror(code)) from None


def _write(request: FileRequest, data: bytes) -> None:
    txn = current().output_dir(request).begin("file", "replace", label="isolated")
    fd = -1
    try:
        fd = txn.create_file("private")
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:  # a zero-progress write would spin forever
                raise OSError(errno.EIO, os.strerror(errno.EIO))
            view = view[written:]
        os.fsync(fd)
        closing, fd = fd, -1
        os.close(closing)
        txn.publish()
    except BaseException:
        if fd >= 0:
            with contextlib.suppress(OSError):  # the fd is gone either way; keep the first error
                os.close(fd)
        try:
            txn.abort()
        except Exception as cleanup:  # keep the first error; the cleanup error is only logged
            _logger.warning(
                "write_private: aborting the private stage failed (%s)", type(cleanup).__name__
            )
        raise


__all__ = ["write_private"]
