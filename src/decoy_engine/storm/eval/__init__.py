"""decoy_engine.storm.eval - field-recognition evaluation tooling (BF2 / ML0).

Off the public run path on purpose: this is measurement/eval tooling
(labeled fixtures + a regex-detector baseline harness), not part of a
scan. Namespaced to the storm subpackage and intentionally NOT
re-exported from ``decoy_engine.__init__``.
"""

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

__all__ = [
    "NO_DETECTOR",
    "ColumnResult",
    "FieldTypeMetrics",
    "HarnessReport",
    "LabeledFixture",
    "build_fixtures",
    "make_iban",
    "make_npi",
    "make_pan",
    "run_baseline",
]
