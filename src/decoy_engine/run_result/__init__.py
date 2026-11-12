"""The ``decoy-run-result/v1`` run record: model (write and read) and the status derivation.

Public names are re-exported from ``decoy_engine``. The projection, builder and collector arrive
in later slices of the same package.
"""

from decoy_engine.run_result._model import (
    RUN_RESULT_SCHEMA,
    RunResult,
    RunResultRead,
    ValueFree,
    derive_read_model,
)
from decoy_engine.run_result._outcome import derive_outcome

# ``ValueFree`` and ``derive_read_model`` serve other versioned documents (for example the
# anonymity risk report); they are not re-exported from ``decoy_engine``.
__all__ = [
    "RUN_RESULT_SCHEMA",
    "RunResult",
    "RunResultRead",
    "ValueFree",
    "derive_outcome",
    "derive_read_model",
]
