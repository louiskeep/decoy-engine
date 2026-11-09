"""`ExecutionError` and the lazy-source refusal, as a leaf.

`decoy_engine.execution._errors` re-exports both and stays the home every caller imports. The
class lives here so a lower layer (the lazy CSV source in `profile/`) can raise the refusal
without importing the execution package, which would load every execution module and cycle on
any future execution export that names a profile type.
"""

from __future__ import annotations

from decoy_engine import reasons
from decoy_engine._error_base import DecoyError


class ExecutionError(DecoyError):
    """Execution-boundary / runner failure. Carries a machine-readable code."""

    def __init__(self, *, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}" if message else f"[{code}]")


# Pickles, tracebacks and `__module__` consumers keep seeing the execution home, not this leaf.
ExecutionError.__module__ = "decoy_engine.execution._errors"


def lazy_source_changed(table: str, detail: str) -> ExecutionError:
    """The one constructor of the `lazy_source_changed` refusal, for every lazy source backend."""
    return ExecutionError(
        code=reasons.runtime.LAZY_SOURCE_CHANGED,
        message=f"the source of table {table!r} changed between routing and the read: {detail}.",
    )
