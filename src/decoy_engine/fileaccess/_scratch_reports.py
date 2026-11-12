"""Frozen, value-free reports the scratch arena returns or attaches to an error.

Pattern: the typed reports of the revision-1 scratch branch (`scratch/_reports.py`), trimmed to
the fields F25 V1 freezes. Every field is a closed code, an enum or a count, so `str()` and
`repr()` can carry no path, table, column or cell value; a value that is not a known code is
refused at construction, never stored.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Literal, get_args

Dimension = Literal["bytes", "unknown"]
Route = Literal["engine.scratch", "out_of_core", "declared_staging", "isolated", "snapshot"]


class Disposition(str, enum.Enum):
    """What happened to an arena's bytes. A failed removal is `RETAINED`, never a success."""

    REMOVED = "removed"
    RETAINED = "retained"


def _check(disposition: object, *counts: int | None) -> None:
    """Refuse anything but a Disposition and non-negative int counts (bool is a caller bug)."""
    if not isinstance(disposition, Disposition) or any(
        c is not None and (type(c) is not int or c < 0) for c in counts
    ):
        raise ValueError("a scratch report takes only a Disposition and non-negative counts")


@dataclass(frozen=True, slots=True, kw_only=True)
class QuotaReport:
    """Why a scratch write stopped: out of budget (`bytes`) or an errno-less failure (`unknown`,
    which names no limit, named limit L3: its cap and observation are None)."""

    route: Route = "engine.scratch"
    dimension: Dimension
    effective_bytes_cap: int | None = None
    observed_bytes: int | None = None
    disposition: Disposition = Disposition.RETAINED

    def __post_init__(self) -> None:
        if self.route not in get_args(Route) or self.dimension not in get_args(Dimension):
            raise ValueError("a quota report takes only its closed route and dimension codes")
        _check(self.disposition, self.effective_bytes_cap, self.observed_bytes)


@dataclass(frozen=True, slots=True, kw_only=True)
class CleanupReport:
    """The outcome of one `reclaim()`. Counts are None when a retained arena was not measured."""

    disposition: Disposition
    retained_bytes: int | None = 0
    retained_inodes: int | None = 0

    def __post_init__(self) -> None:
        _check(self.disposition, self.retained_bytes, self.retained_inodes)


__all__ = ["CleanupReport", "Disposition", "QuotaReport"]
