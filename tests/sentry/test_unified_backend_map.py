"""The unified slice's operator-to-backend map must cover every admitted operator.

A new admitted operator then has to declare its planned backend, instead of the
evidence silently falling through to a default.
"""

from __future__ import annotations

from decoy_engine.execution import _unified_slice_admission as admission
from decoy_engine.execution.native._chunked_evidence import (
    ARROW_PYTHON,
    RUST_COMPANION,
    RUST_POOL_SELECT,
)


def test_backend_map_keys_equal_allowed_operator_ids() -> None:
    assert set(admission.BACKEND_BY_OPERATOR_ID) == set(admission.ALLOWED_OPERATOR_IDS)


def test_backend_map_values_use_the_chunked_vocabulary() -> None:
    assert set(admission.BACKEND_BY_OPERATOR_ID.values()) <= {
        RUST_COMPANION,
        RUST_POOL_SELECT,
        ARROW_PYTHON,
    }


def test_backend_map_pins_each_operator() -> None:
    assert dict(admission.BACKEND_BY_OPERATOR_ID) == {
        "native_keyed_hash": RUST_COMPANION,
        "native_fpe": RUST_COMPANION,
        "native_categorical": RUST_COMPANION,
        "native_bucket_perturb": RUST_COMPANION,
        "native_date_shift": RUST_COMPANION,
        "native_group_key": RUST_COMPANION,
        "native_faker_select": RUST_POOL_SELECT,
        "native_redact": ARROW_PYTHON,
        "native_truncate": ARROW_PYTHON,
        "native_text_redact": ARROW_PYTHON,
        "native_passthrough": ARROW_PYTHON,
    }
