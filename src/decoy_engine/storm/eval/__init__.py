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
    HIGH_PRECISION_FLOOR,
    LATENCY_BUDGET_MS,
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
    "HIGH_PRECISION_FLOOR",
    "LATENCY_BUDGET_MS",
    # fixtures
    "NO_DETECTOR",
    "REVIEW_PRECISION_FLOOR",
    # bands
    "Band",
    # harness
    "ColumnResult",
    "FieldTypeMetrics",
    "HarnessReport",
    "LabeledFixture",
    "benchmark_all_fixture_columns",
    "benchmark_column_latency",
    "build_fixtures",
    "classify_band",
    # split
    "held_out_split",
    "make_group_key",
    "make_iban",
    "make_npi",
    "make_pan",
    "make_split_inputs",
    "run_baseline",
]
