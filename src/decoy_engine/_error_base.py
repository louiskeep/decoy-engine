"""The root of the engine exception hierarchy, as a leaf.

`decoy_engine.errors` re-exports `DecoyError` and stays the public home of every typed
exception. The root lives here so a leaf package that `errors` itself imports (the reason
registry) can still derive its own exception from the engine base without an import cycle.
"""

from __future__ import annotations


class DecoyError(Exception):
    """Base class for all decoy_engine exceptions."""


# Pickles, tracebacks and `__module__` consumers keep seeing the public home, not this leaf.
DecoyError.__module__ = "decoy_engine.errors"
