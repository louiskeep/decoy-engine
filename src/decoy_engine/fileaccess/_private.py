"""`write_private`: one choke point for files that hold secrets or engine copies of real data."""

from __future__ import annotations

import os

from decoy_engine.fileaccess._entry import current
from decoy_engine.fileaccess._types import FileRequest


def write_private(request: FileRequest, data: bytes) -> None:
    out = current().output_dir(request)
    txn = out.begin("file", "replace", label="isolated")
    fd = -1
    try:
        fd = txn.create_file("private")
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
        closing, fd = fd, -1
        os.close(closing)
        txn.publish()
    except BaseException:
        if fd >= 0:
            os.close(fd)
        txn.abort()
        raise


__all__ = ["write_private"]
