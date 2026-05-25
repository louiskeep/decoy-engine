"""ApplyContext: runtime data the dispatcher resolves at apply time.

V2 Phase 3 Distribution Integrity, Sprint D5c.

Background. The masking strategy interface has always been:

    def apply(self, column: pd.Series, rule: dict) -> pd.Series

`rule` was the operator's configuration; everything the strategy
needed at apply time had to come from there. That worked for
strategies whose behavior depends only on the column being masked
(hash, redact, faker draws-per-row), but it falls apart for any
strategy that needs information about OTHER columns at apply time:

    - hash that preserves joint relationships:
      "every (city, zip) pair in the source maps to the same
      (hashed_city, hashed_zip) pair in the output" requires the
      hash strategy to SEE the source `city` column when masking
      `zip` (and vice versa).
    - date_shift with a per-subject offset:
      derive the offset from a subject_key column the strategy
      doesn't otherwise touch.
    - bucketize that respects an income/zip pairing.

Two interface options were considered (see
2026-05-24 distribution-integrity sprint plan discussion):

    A. Put column references in the rule dict; strategies look up
       the actual data through some shared dispatcher state.
    B. Add a `ctx: ApplyContext` arg carrying the actual data.

Option B locked. Reasoning:
    - The rule dict was already getting strained — date_shift's
      subject_key and FK preservation's parent references had been
      encoded in rule dicts in ways that blurred config-vs-runtime.
      D6 (statistical generation) and D7 (privacy reporting) would
      push it further.
    - A typed context object lets each strategy declare what it
      needs from ctx; mypy + IDE catch typos.
    - The dispatcher gets a single resolve-once point for joint
      data instead of strategies each looking it up.

Scope rule (to keep `ctx` from becoming a god object):
    ApplyContext carries data the DISPATCHER resolved AT APPLY TIME
    from the current row set. Configuration stays in `rule`.
    Cross-job state (model pack metadata, etc.) does not belong here.

Backwards compatibility:
    `ctx` defaults to None at every call site. Strategies that don't
    care about joint data simply don't read it; existing strategies
    pass type checks without changes. The dispatcher constructs an
    ApplyContext for every apply call so strategies that DO read it
    always get a valid object (never have to None-guard).

D5c part 1: this module. Part 2 threads it through the strategy
base + dispatcher. Part 3 updates the hash strategy to read
`ctx.joint_columns` and preserve the joint distribution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    import pandas as pd


# Frozen empty mapping reused as the no-joints default. MappingProxyType
# is read-only, so accidental mutation by a strategy raises rather than
# silently leaking state across calls.
_EMPTY_JOINT_COLUMNS: Mapping[str, "pd.Series"] = MappingProxyType({})


@dataclass(frozen=True)
class ApplyContext:
    """Runtime context the dispatcher resolves at apply time.

    Mental model: `rule` is what the operator configured; `ctx` is
    what the dispatcher computed from the actual data being masked
    right now. Strategies are encouraged to read whichever fields
    they need and ignore the rest; the object is intentionally
    small so future cross-cutting concerns can be added without
    breaking strategies that don't read them.

    Attributes:
        joint_columns: name -> actual pandas Series for source
            columns the strategy needs to be CONSISTENT with at
            apply time. Empty by default; populated by the
            dispatcher when the rule declares `joint_with`.

            The Series are aligned to the column being masked (same
            index, same length, same row order), so a strategy can
            zip them with the input column without re-alignment.

            Per the scope rule, joint_columns is for READ access
            only. Strategies must not mutate the Series.
    """

    joint_columns: Mapping[str, "pd.Series"] = field(
        default_factory=lambda: _EMPTY_JOINT_COLUMNS,
    )

    @classmethod
    def empty(cls) -> "ApplyContext":
        """Return the no-joint-data instance.

        Use as the default at call sites that don't have joint
        information to pass. Cheap because the underlying mapping
        is the module-level frozen empty.
        """
        return cls()
