"""FK preservation findings: one per declared edge, from the facts of its child key, which
`_keys.py` counts once against the union of every parent of that child tuple. Severity follows the
key's codes and baseline, never an orphan rate (`types.FKPreservationFinding`). Findings hold names
and counts, never a key value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
from pandas.api.types import is_float_dtype

from decoy_engine import fileaccess, reasons
from decoy_engine.config import RelationshipConfig, RelationshipEnd, parse_relationship_config
from decoy_engine.profiling import MEMORY_LIMIT
from decoy_engine.verification.postmask._keys import KeyCounts, KeyResult, check_keys
from decoy_engine.verification.postmask._stream_input import TableBatches, from_table
from decoy_engine.verification.postmask.types import FKPreservationFinding, Severity

_ORPHANS = reasons.post_scan.FOREIGN_KEY_ORPHANS_INTRODUCED


def fk_findings(
    config: Mapping[str, Any],
    source_frames: Mapping[str, pd.DataFrame],
    output_frames: Mapping[str, pd.DataFrame],
    keys: Sequence[KeyResult] | None = None,
) -> list[FKPreservationFinding]:
    """One finding per declared edge. Without `keys` the facts are counted here, over the edges
    whose tables are all in `output_frames`: a table the caller left out has nothing to check."""
    rels = [
        parse_relationship_config(raw, prefix=f"relationship {n} is invalid")
        for n, raw in enumerate(config.get("relationships") or ())
    ]
    present = None
    if keys is None:
        keys, present = _frame_keys(rels, source_frames, output_frames), output_frames
    by_end = {(k.table, k.columns): k for k in keys if k.kind == "foreign"}
    return [
        _finding(rel, child, by_end.get((child.table, tuple(child.columns))))
        for rel in rels
        for child in rel.children
        if present is None or {rel.parent.table, child.table} <= present.keys()
    ]


def _frame_keys(
    rels: Sequence[RelationshipConfig],
    source_frames: Mapping[str, pd.DataFrame],
    output_frames: Mapping[str, pd.DataFrame],
) -> tuple[KeyResult, ...]:
    if not rels:
        return ()
    wanted: dict[str, set[str]] = {}
    for rel in rels:
        for end in (rel.parent, *rel.children):
            wanted[end.table] = wanted.get(end.table, set()) | set(end.columns)

    def tables(frames: Mapping[str, pd.DataFrame]) -> dict[str, TableBatches]:
        # Only key columns are converted; one that is missing stays missing, so the key says so.
        out = {}
        for name, frame in frames.items():
            if name in wanted:
                key = frame[[c for c in frame.columns if c in wanted[name]]]
                out[name] = from_table(
                    name, pa.Table.from_pandas(_whole(key), preserve_index=False)
                )
        return out

    with fileaccess.protected_scratch_dir(prefix="decoy-post-scan-keys-") as scratch:
        return check_keys(
            rels,
            tables(source_frames),
            tables(output_frames),
            scratch=scratch,
            # The key scan's own cap; a caller that holds its run's limit passes `keys` instead.
            memory_limit=MEMORY_LIMIT,
        )


def _whole(key: pd.DataFrame) -> pd.DataFrame:
    """pandas reads a nullable integer column as float, so read a float key column (NumPy or
    nullable extension) whose non-null values are all whole and in the Int64 range as Int64. The
    whole column decides; every other column keeps its dtype."""
    whole = {c: "Int64" for c, col in key.items() if is_float_dtype(col.dtype) and _integral(col)}
    return key.astype(whole) if whole else key


def _integral(column: pd.Series) -> bool:
    # One NumPy view for every float dtype; a null is NaN there and is left out.
    values = column.to_numpy(dtype="float64", na_value=np.nan)
    values = values[~np.isnan(values)]
    return bool(np.all((values == np.trunc(values)) & (values >= -(2.0**63)) & (values < 2.0**63)))


def _finding(
    rel: RelationshipConfig, child: RelationshipEnd, key: KeyResult | None
) -> FKPreservationFinding:
    severity, message = _severity(key)
    out = KeyCounts() if key is None else key.output
    rows, orphans = out.rows - out.null_key_rows, out.orphan_rows
    return FKPreservationFinding(
        parent_table=rel.parent.table,
        parent_column=", ".join(rel.parent.columns),
        child_table=child.table,
        child_column=", ".join(child.columns),
        severity=severity,
        orphan_count=orphans,
        total_child_rows=rows,
        orphan_rate=orphans / rows if rows else 0.0,
        namespace=rel.namespace,
        message=message,
        parent_columns=tuple(rel.parent.columns),
        child_columns=tuple(child.columns),
        source_orphan_count=None if key is None or key.source is None else key.source.orphan_rows,
    )


def _severity(key: KeyResult | None) -> tuple[Severity, str]:
    if key is None:
        return "error", "the key was not checked."
    # The 4.2.2 rows that block without counts; a mismatch over a mismatched source only warns.
    if key.status in ("error", "key_column_missing") or (
        key.status == "key_type_mismatch" and key.baseline != "key_type_mismatch"
    ):
        return "error", f"the key could not be checked ({key.status})."
    orphans = key.output.orphan_rows
    if _ORPHANS in key.codes:
        return "fail", f"{orphans} orphan row(s); the run introduced orphans."
    if orphans:
        return "warning", f"{orphans} orphan row(s), all already in the source."
    if key.codes:
        return "warning", "the key carries only warning codes."
    if key.output.rows == key.output.null_key_rows:
        return "info", "no non-null child key; nothing to check."
    return "info", "all child keys resolve."
