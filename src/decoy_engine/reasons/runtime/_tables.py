"""Finite code families of the ``runtime`` namespace: one explicit entry per member.

A code that varies with a small closed set (here, the native sequential strategy) is a table of
registry symbols selected by key, never a string built at the raise site.
"""

from __future__ import annotations

from collections.abc import Mapping

from decoy_engine.reasons import runtime
from decoy_engine.reasons._types import ReasonCode

__all__ = ["SEQUENTIAL_INPUT_TYPE_DRIFT_BY_STRATEGY"]

# Keys are the native sequential strategies (`_sequential_native._OPS`); a test pins that equality.
SEQUENTIAL_INPUT_TYPE_DRIFT_BY_STRATEGY: Mapping[str, ReasonCode] = {
    "hash": runtime.SEQUENTIAL_HASH_INPUT_TYPE_DRIFT,
    "fpe": runtime.SEQUENTIAL_FPE_INPUT_TYPE_DRIFT,
    "categorical": runtime.SEQUENTIAL_CATEGORICAL_INPUT_TYPE_DRIFT,
    "bucket_perturb": runtime.SEQUENTIAL_BUCKET_PERTURB_INPUT_TYPE_DRIFT,
    "date_shift": runtime.SEQUENTIAL_DATE_SHIFT_INPUT_TYPE_DRIFT,
    "faker": runtime.SEQUENTIAL_FAKER_INPUT_TYPE_DRIFT,
    "text_redact": runtime.SEQUENTIAL_TEXT_REDACT_INPUT_TYPE_DRIFT,
    "text_mask": runtime.SEQUENTIAL_TEXT_MASK_INPUT_TYPE_DRIFT,
    "group_key": runtime.SEQUENTIAL_GROUP_KEY_INPUT_TYPE_DRIFT,
}
