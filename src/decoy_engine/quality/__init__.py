"""V2 Distribution Integrity, Sprint D1a: Measurement Foundation.

Pure-compute distribution snapshot module. Exposes the deterministic,
JSON-serializable per-column + per-joint snapshot that later D1 sub-sprints
(diagnostic, fidelity, report assembly) consume to compare source vs output
dataframes.

This package will grow over D1b-D1d to include `diagnostic`, `fidelity`,
and `report`. D1a only lands the measurement primitive so it can be
exercised, golden-tested, and reviewed in isolation. Per Dennis-style
sub-sprint discipline: ship the smallest defensible unit, prove it, then
stack on top.

Public surface (V2.0+):
    compute_distribution_snapshot(df, *, joint_columns=None, ...) -> dict

The returned dict is keyed `schema_version = "distribution-snapshot/v1"`
so downstream consumers can branch on schema evolution without sniffing
shape. The shape is documented in `snapshot.compute_distribution_snapshot`
and pinned by tests/snapshots/test_distribution_snapshot_baseline.py.

Package exports are resolved LAZILY (PEP 562 `__getattr__`). Every
submodule reachable eagerly here pulls pandas (and pyarrow), so an eager
re-export made `import decoy_engine.quality.carriers` -- the pandas-free
DP carrier core -- drag pandas in via this `__init__` (guide
2026-07-23-dps-codec-implementation-guide.md section 3.8). Deferring the
imports to first attribute access keeps the public names working while
letting the carrier subtree be imported clean; the import-isolation test
in tests/unit/quality/test_carriers.py is the proof.
"""

import importlib
from typing import Any

# name -> submodule providing it. Kept explicit (not derived) so the
# public surface is auditable and a typo fails loudly at access time.
_LAZY_EXPORTS: dict[str, str] = {
    "QUALITY_DIAGNOSTIC_SCHEMA_VERSION": "diagnostic",
    "compute_diagnostic": "diagnostic",
    "QUALITY_FIDELITY_SCHEMA_VERSION": "fidelity",
    "compute_fidelity": "fidelity",
    "QUALITY_POLICY_SCHEMA_VERSION": "policy",
    "apply_quality_policy": "policy",
    "QUALITY_REPORT_SCHEMA_VERSION": "report",
    "assemble_quality_report": "report",
    "compute_quality_report": "report",
    "score_to_grade": "report",
    "QUALITY_SHAPE_FIDELITY_SCHEMA_VERSION": "shape_fidelity",
    "compute_shape_fidelity": "shape_fidelity",
    "DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION": "snapshot",
    "compute_distribution_snapshot": "snapshot",
    "SYNTH_REPORT_SCHEMA_VERSION": "synth_report",
    "assemble_synth_report": "synth_report",
    "compute_attack_metrics": "synth_report",
    "compute_dcr": "synth_report",
    "compute_new_row_synthesis": "synth_report",
}


def __getattr__(name: str) -> Any:
    submodule = _LAZY_EXPORTS.get(name)
    if submodule is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{submodule}"), name)
    globals()[name] = value  # cache so subsequent access skips __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(list(globals()) + list(_LAZY_EXPORTS))


__all__ = [
    "DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION",
    "QUALITY_DIAGNOSTIC_SCHEMA_VERSION",
    "QUALITY_FIDELITY_SCHEMA_VERSION",
    "QUALITY_POLICY_SCHEMA_VERSION",
    "QUALITY_REPORT_SCHEMA_VERSION",
    "QUALITY_SHAPE_FIDELITY_SCHEMA_VERSION",
    "SYNTH_REPORT_SCHEMA_VERSION",
    "apply_quality_policy",
    "assemble_quality_report",
    "assemble_synth_report",
    "compute_attack_metrics",
    "compute_dcr",
    "compute_diagnostic",
    "compute_distribution_snapshot",
    "compute_fidelity",
    "compute_new_row_synthesis",
    "compute_quality_report",
    "compute_shape_fidelity",
    "score_to_grade",
]
