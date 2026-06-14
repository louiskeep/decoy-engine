"""Release phase: the single switch the pre-GA to GA rule inversions read.

Several rules flip at general availability (engineering-best-practices section
8.1 and the compatibility contract): pre-GA we hard-delete removed code with no
migration shim; post-GA we keep shims and the compatibility contract becomes
binding; the cross-version compatibility corpus gate flips from capture/advisory
to blocking. Rather than scatter that judgement across modules and CI, the gates
read RELEASE_PHASE from here. Flipping it at launch is one reviewed change; the
gates that branch on it are wired ahead of time so the flip is the last step,
not a scramble.

This module is the engine-owned source of truth. The platform reads it across
the in-process boundary (ADR-0001): `from decoy_engine import RELEASE_PHASE`.
There is no separate platform constant to keep in sync.
"""

from __future__ import annotations

from typing import Literal

ReleasePhase = Literal["pre-ga", "ga"]

# Current phase. Flip to "ga" at the first general-availability release, in a
# single reviewed commit, once every gate that branches on it (the compat
# corpus gate, the section 8.1 deletion policy, the contract-binding checks) is
# wired and verified against this constant.
RELEASE_PHASE: ReleasePhase = "pre-ga"


def is_pre_ga() -> bool:
    """True before general availability.

    Gates call this to decide whether the pre-GA rules apply: hard-delete is
    allowed, the compatibility contract is advisory, and the cross-version
    compatibility corpus runs in capture/no-op mode rather than blocking.
    """
    return RELEASE_PHASE == "pre-ga"
