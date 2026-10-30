"""Stable, value-free tokens for anything a receipt, refusal or log may say about an OS error.

`str(OSError)` embeds paths and file names, so publication code never interpolates an
exception: it names the errno (`ENOSPC`) or, when there is none, the exception type.
"""

from __future__ import annotations

import errno


def os_error_token(exc: BaseException) -> str:
    code = getattr(exc, "errno", None)
    if isinstance(code, int):
        return errno.errorcode.get(code, type(exc).__name__)
    return type(exc).__name__


def os_error_label(exc: BaseException) -> str:
    """`OSError/ENOSPC` for refusal messages: the type and the errno name, nothing else."""
    token = os_error_token(exc)
    name = type(exc).__name__
    return token if token == name else f"{name}/{token}"
