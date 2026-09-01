"""text_mask `when` gate for chunked execution (Phase 4 slice 3).

Extracted to keep `_chunked.py` under the orchestration LOC cap, mirroring
`_chunked_dgrn.py` / `_chunked_group_key.py`. See `_chunked.py`'s module
docstring for the text_mask CHUNK_SAFE admission and
`docs/plans/2026-09-01-p4-slice3-text-mask-chunked.md` for the full design.

Unlike `windowed_date` (position-keyed) and `group_key` (sibling-keyed),
`text_mask` is keyed on its own column's value: it joins `CHUNK_SAFE_
STRATEGIES` directly (`_chunked_fk.py`) and needs no separate admitted set
or FK-exclusion here. The one thing this slice still rejects at compile
time is `text_mask` + `when:` (Trap E): the handler `str()`-converts every
non-null non-string cell (`_text_mask.py`), so a text_mask source can be
numeric. A when-gated column would leave non-matching rows at their
original (possibly numeric) dtype while matching rows become masked
strings, so a chunk of all-non-matching rows keeps the source dtype while
a chunk with matches turns to object -- a chunk-boundary-dependent output
dtype, the same hazard `reject_group_key_when` closes. The auto planner
already blanket-rejects `when` for auto-routing; this is the manual
`run_mask_pipeline_chunked` entry's own gate.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError


def reject_text_mask_when(table_cfg: dict[str, Any], *, table: str) -> None:
    """Reject a `text_mask` column that also carries a `when:` predicate.

    Raises:
        PlanCompileError: ``code='chunked_text_mask_when_not_supported'``.
    """
    text_mask_when_cols = sorted(
        str(col_entry.get("name", "?"))
        for col_entry in table_cfg.get("columns") or []
        if isinstance(col_entry, dict)
        and col_entry.get("strategy") == "text_mask"
        and col_entry.get("when")
    )
    if not text_mask_when_cols:
        return
    raise PlanCompileError(
        code="chunked_text_mask_when_not_supported",
        path=f"tables.{table}.columns",
        message=(
            f"column(s) {', '.join(text_mask_when_cols)} combine 'text_mask' "
            "with a 'when:' predicate, which is not supported on the chunked "
            "route: the handler str()-converts every non-null cell, so a "
            "when-gated column leaves non-matching rows at their original "
            "(possibly numeric) dtype while matching rows become masked "
            "strings -- a chunk-boundary-dependent output dtype."
        ),
    )
