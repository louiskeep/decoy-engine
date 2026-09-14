"""Task 4.4 C4: the frozen difference-code catalog is closed, and
`CryptoExtensionUnavailableError` maps to `native_companion_unavailable`.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.physical._plan import ExecutionBinding, KeyBinding
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_diff_codes import (
    DIFFERENCE_CODES,
    NATIVE_COMPANION_UNAVAILABLE,
    ShadowDifference,
)
from decoy_engine.execution.physical._shadow_operators import OperatorCallEvidence, run_operator

_EXPECTED_CODES = frozenset(
    {
        "schema-diff",
        "row-count-diff",
        "row-order-diff",
        "cell-value-diff",
        "null-mask-diff",
        "diagnostics-diff",
        "planned-vs-actual-route-diff",
        "operator-not-executed",
        "snapshot-identity-diff",
        "resource-limit-breach",
        "publication-attempt",
        "native_companion_unavailable",
    }
)


def test_catalog_is_exactly_the_frozen_set_named_in_the_plan() -> None:
    assert DIFFERENCE_CODES == _EXPECTED_CODES


def test_shadow_difference_rejects_an_unknown_code() -> None:
    with pytest.raises(ValueError, match="not in the frozen difference-code catalog"):
        ShadowDifference(code="not-a-real-code", detail="x")


def test_shadow_difference_never_carries_a_value_in_its_str() -> None:
    diff = ShadowDifference(code="cell-value-diff", detail="table.col: values differ")
    assert "cell-value-diff" in str(diff)
    assert "table.col" in str(diff)


def _hash_binding(namespace: str = "n") -> ExecutionBinding:
    return ExecutionBinding(
        operator_id="native_keyed_hash",
        operator_reason="slice_native_admitted:hash",
        resolved_config=(),
        input_schema=pa.schema([pa.field("c", pa.string())]),
        output_schema=pa.schema([pa.field("c", pa.string())]),
        determinism_family="source_keyed_hmac",
        determinism_version=2,
        key_binding=KeyBinding(key_source="mask_key", namespace=namespace),
        diagnostic_obligations=(),
        required_prepasses=(),
        batch_estimate=None,
    )


def test_crypto_extension_unavailable_maps_to_native_companion_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_unavailable() -> None:
        raise CryptoExtensionUnavailableError("companion missing in this test")

    monkeypatch.setattr(
        "decoy_engine.execution.native._kernels_keyed.load_compiled_crypto_kernel",
        _raise_unavailable,
    )
    ctx = ShadowContext(mask_key=b"\x01" * 32)
    binding = _hash_binding()
    evidence = OperatorCallEvidence(planned_operator=binding.operator_id)
    array = pa.array(["a", "b"], type=pa.string())

    with pytest.raises(ShadowDifference) as excinfo:
        run_operator(array, binding=binding, ctx=ctx, evidence=evidence)
    assert excinfo.value.code == NATIVE_COMPANION_UNAVAILABLE
    assert isinstance(excinfo.value.__cause__, CryptoExtensionUnavailableError)
    # Never falls back: no evidence of a successful call is recorded.
    assert evidence.executed is False
    assert evidence.compiled_kernel_executed is False
