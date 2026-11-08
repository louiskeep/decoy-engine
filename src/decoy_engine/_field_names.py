"""Which field names repeat in a source schema or frame.

The shared scan behind every duplicate-name refusal: the run entry and ingest guards
(`execution._guards`), the profile walk and the name-keyed carry and slice admission. Each
boundary keeps its own exception and wording; only the scan is shared. Dependency-free on
purpose, so profile and execution can both import it.
"""

from __future__ import annotations

from collections.abc import Iterable


def repeated_names(names: Iterable[str]) -> frozenset[str]:
    """The names that appear more than once, empty when every name is unique."""
    seen: set[str] = set()
    repeated: set[str] = set()
    for name in names:
        (repeated if name in seen else seen).add(name)
    return frozenset(repeated)
