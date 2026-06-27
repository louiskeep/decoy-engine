"""decoy_engine.storm.eval - field-recognition evaluation tooling (BF2 / ML0).

Off the public run path on purpose: this is measurement/eval tooling
(labeled fixtures + a regex-detector baseline harness), not part of a
scan. Namespaced to the storm subpackage and intentionally NOT
re-exported from ``decoy_engine.__init__``.

Extended for ml-benchmarking-and-privacy.md §A.1 / §A.3 / §A.4 / §A.7:
- harness: F2, confusion matrix, FP/FN enumeration, aggregate metrics
- split:   StratifiedGroupKFold split scaffolding (leakage guard)
- bands:   confidence-band thresholds + per-column latency benchmark
"""

from decoy_engine.storm.eval.bands import (
    LATENCY_BUDGET_MS,
    HIGH_PRECISION_FLOOR,
    REVIEW_PRECISION_FLOOR,
    Band,
    benchmark_all_fixture_columns,
    benchmark_column_latency,
    classify_band,
)
from decoy_engine.storm.eval.fixtures import (
    NO_DETECTOR,
    LabeledFixture,
    build_fixtures,
    make_iban,
    make_npi,
    make_pan,
)
from decoy_engine.storm.eval.harness import (
    ColumnResult,
    FieldTypeMetrics,
    HarnessReport,
    run_baseline,
)
from decoy_engine.storm.eval.split import (
    held_out_split,
    make_group_key,
    make_split_inputs,
)

__all__ = [
    # fixtures
    "NO_DETECTOR",
    "LabeledFixture",
    "build_fixtures",
    "make_iban",
    "make_npi",
    "make_pan",
    # harness
    "ColumnResult",
    "FieldTypeMetrics",
    "HarnessReport",
    "run_baseline",
    # split
    "held_out_split",
    "make_group_key",
    "make_split_inputs",
    # bands
    "Band",
    "HIGH_PRECISION_FLOOR",
    "LATENCY_BUDGET_MS",
    "REVIEW_PRECISION_FLOOR",
    "benchmark_all_fixture_columns",
    "benchmark_column_latency",
    "classify_band",
]
