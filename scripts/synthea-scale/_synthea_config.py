"""Build the realistic PHI mask config for the Synthea scale harness.

Derived entirely from the one column spec in `_synthea_layout.COLUMNS`, so the
dataset shape and the mask rule never drift. The strategies are the ones a
hospital customer would reach for on a Synthea export: FPE on the format-bearing
identifiers (SSN, licenses, costs), bucket-perturb (month generalization) on the
dates, deterministic `categorical` remap onto a synthetic pool for the
direct-identifier name/address cells and the small coded fields, keyed hash on
the UUID/code identifiers and the FK keys, truncation on ZIP, and text-redaction
on the free-text description columns.

The two FK edges mask their key and child columns with `hash` under one shared
namespace each (`NS_PATIENT`, `NS_ENCOUNTER`), and the relationship declares that
namespace, so the same source id masks to the same value on both ends and the FK
join survives. FK referential integrity is checked by the harness's own
order-independent parity sample (`_synthea_parity`); the engine's built-in
`post_validation` FK-join scan is NOT run here, because it requires the full
frame resident and is declined on the out-of-core route this harness measures
(it diverts the job to full-frame, defeating the out-of-core characterization),
so it is deliberately left off. See the measurement record for the full note.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from _synthea_layout import (
    COLUMNS,
    FK_EDGE,
    KEY_COLUMN,
    NS_ENCOUNTER,
    NS_PATIENT,
    TABLE_NAMES,
)

# The relationship namespace per edge is the one its parent key column declares.
_EDGE_NAMESPACE = {"patients": NS_PATIENT, "encounters": NS_ENCOUNTER}


# Strategies that compile only with a namespace (the key is derived from it). A
# column that does not set one gets a unique per-column namespace so, unlike the FK
# edges, these mask independently.
_NAMESPACE_REQUIRED = {"categorical"}


def _column_rule(table: str, spec: Any) -> dict[str, Any]:
    rule: dict[str, Any] = {"name": spec.name, "strategy": spec.strategy}
    namespace = spec.namespace
    if namespace is None and spec.strategy in _NAMESPACE_REQUIRED:
        namespace = f"synthea_{table}_{spec.name}"
    if namespace:
        rule["namespace"] = namespace
    # Deterministic categorical remap (same source value -> same masked value) is a
    # normal masking choice and lets the compiled native categorical kernel run on
    # the bounded sequential route instead of the Python oracle.
    if spec.strategy == "categorical":
        rule["deterministic"] = True
    if spec.provider:
        rule["provider"] = spec.provider
    if spec.provider_config:
        rule["provider_config"] = dict(spec.provider_config)
    return rule


def build_config(src_paths: dict[str, Path], out_dir: Path, seed: int = 7) -> dict[str, Any]:
    """The `run_pipeline` config for the chain under `src_paths`, writing to `out_dir`."""
    tables = [{"name": t, "columns": [_column_rule(t, c) for c in COLUMNS[t]]} for t in TABLE_NAMES]
    relationships = []
    for child, (fk_col, parent, parent_key) in FK_EDGE.items():
        relationships.append(
            {
                "parent": {"table": parent, "columns": [parent_key]},
                "children": [{"table": child, "columns": [fk_col]}],
                "orphan_policy": "preserve",
                "namespace": _EDGE_NAMESPACE[parent],
            }
        )
    return {
        "version": 1,
        # NOTE: `post_validation` is a run_pipeline runtime kwarg, not a
        # global_settings field -- setting it here would be inert. It is left off
        # on purpose (it forces the full-frame route; see the module docstring).
        "global_settings": {"seed": seed},
        "sources": {
            t: {"type": "file", "format": "parquet", "path": str(src_paths[t])} for t in TABLE_NAMES
        },
        "targets": {
            t: {"type": "file", "format": "parquet", "path": str(out_dir / f"{t}.parquet")}
            for t in TABLE_NAMES
        },
        "tables": tables,
        "relationships": relationships,
    }


def declared_strategies() -> dict[str, int]:
    """Count of columns per strategy, for the record (what the config exercises)."""
    counts: dict[str, int] = {}
    for table in TABLE_NAMES:
        for spec in COLUMNS[table]:
            counts[spec.strategy] = counts.get(spec.strategy, 0) + 1
    return counts


# re-exported so the worker needs only this module for config + key metadata.
__all__ = ["FK_EDGE", "KEY_COLUMN", "TABLE_NAMES", "build_config", "declared_strategies"]
