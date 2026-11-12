"""A bare MemoryError has its own registered identity, and the capacity set carries it."""

from __future__ import annotations

from decoy_engine import errors, reasons
from decoy_engine.reason_adapter import reason_from_exception
from decoy_engine.reasons import UNRELEASED, RetryClass, get

_CODE = "process_out_of_memory"


def test_memory_error_identity() -> None:
    record = reason_from_exception(MemoryError())
    assert (record.namespace, record.code) == ("decoy", _CODE)
    assert record.retry_class is RetryClass.ENVIRONMENT_REPAIR
    assert _CODE in errors.CAPACITY_REASON_CODES
    assert get("decoy", _CODE).since == UNRELEASED


def test_memory_error_message_is_fixed_and_value_free() -> None:
    summary = get("decoy", _CODE).summary
    assert summary == "The process ran out of memory."
    # The exception text can carry sizes and names, so a raised message must not reach the record.
    leaky = reason_from_exception(MemoryError("secret-token-1234 /home/alice/data.csv"))
    assert "secret-token-1234" not in repr(leaky)
    assert "/home/alice" not in repr(leaky)


def test_symbol_is_registered() -> None:
    assert reasons.is_registered(reasons.decoy.PROCESS_OUT_OF_MEMORY)
