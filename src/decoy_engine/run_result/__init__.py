"""The ``decoy-run-result/v1`` run record: model (write and read) and the status derivation.

Public names are re-exported from ``decoy_engine``. The projection, builder and collector arrive
in later slices of the same package.
"""

from decoy_engine.run_result._model import RUN_RESULT_SCHEMA, RunResult, RunResultRead
from decoy_engine.run_result._outcome import Outcome, derive_outcome

__all__ = ["RUN_RESULT_SCHEMA", "Outcome", "RunResult", "RunResultRead", "derive_outcome"]
