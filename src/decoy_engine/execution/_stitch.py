"""Task 4.6 slice 5b-i: the single shared owner of the generate+mask output
stitch precedence -- `run_pipeline`'s former inline Step 3 (`_pipeline.py`
"mask wins ties") and the physical-plan shadow coordinator's independent-
mixed dispatch (`physical/_shadow_mixed.py`) both call this ONE function, so
the precedence rule cannot drift between the two implementations (Codex
plan-gate BLOCKER #1).

Lives at this PARENT `execution` level, not under `execution/physical/`:
`_pipeline.py` must never import the physical shadow seam (the disconnection
sentry, `tests/sentry/test_physical_seam_disconnection.py`, forbids it and
the fresh-import invariant would break), so a helper both sides share has to
sit outside that package. `execution/physical/_shadow_mixed.py` imports this
module instead -- the dependency points the other way, which the sentry
never restricts.
"""

from __future__ import annotations

import pyarrow as pa

__all__ = ["stitch_generate_mask_outputs"]


def stitch_generate_mask_outputs(
    generate_outputs: dict[str, pa.Table], mask_outputs: dict[str, pa.Table]
) -> dict[str, pa.Table]:
    """Union two output dicts, mask winning any name collision.

    `dict.update` preserves an existing key's position and only replaces its
    value, so the result is generate-insertion-order first, with mask
    entries appended after -- never repositioned on a tie-overwrite. Every
    table name in a real job maps to exactly one kind by construction
    (`classify_table_kinds`'s XOR), so a genuine collision never occurs; this
    function still states the precedence explicitly rather than leaving it
    to dict-iteration order, matching the oracle's original inline comment
    ("Mask wins ties"). Table objects pass through by identity -- this never
    copies or otherwise touches a value, only the two mappings' keys.
    """
    outputs: dict[str, pa.Table] = dict(generate_outputs)
    outputs.update(mask_outputs)
    return outputs
