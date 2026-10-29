"""F29 acceptance A3: a hostile exception never escapes through a reason record.

Each marker is injected into a different channel (message, code, class name, cause, filename,
context). The record, its payload and its JSON are then searched for it, next to positive
controls that prove the record still carries the identity, phase and retry advice.
"""

from __future__ import annotations

import errno
import json

import pytest

from decoy_engine import reasons
from decoy_engine.errors import ConfigError
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.reason_adapter import capture_failure, failure_scope, reason_from_exception
from decoy_engine.reasons import Operation, Phase, ReasonContext, ReasonRecord, RetryClass

MARK = "ZXQ/SECRET:7731 postgres://user:pw@host/db"


def _text(record: ReasonRecord) -> str:
    return json.dumps(record.to_payload(), sort_keys=True) + repr(record)


class _HostileError(Exception):
    def __str__(self) -> str:
        raise RuntimeError(MARK)

    __repr__ = __str__

    @property
    def code(self) -> str:
        raise RuntimeError(MARK)


def _named(name: str) -> type[Exception]:
    return type(name, (Exception,), {})


def test_message_class_name_cause_and_filename_never_escape() -> None:
    cause = ValueError(MARK)
    boom = _named(f"Boom{MARK.replace(' ', '_').replace('/', '_').replace(':', '_')}")(MARK)
    boom.__cause__ = cause
    boom.filename = MARK  # type: ignore[attr-defined]
    record = reason_from_exception(boom, phase=Phase.MASK)
    assert "ZXQ" not in _text(record)
    assert "postgres" not in _text(record)
    assert (record.namespace, record.code) == ("decoy", "unexpected_error")
    assert record.phase is Phase.MASK
    assert record.retry_class is RetryClass.UNKNOWN


def test_engine_family_with_a_data_derived_code_is_not_echoed() -> None:
    record = reason_from_exception(ExecutionError(code=f"leak_{MARK}", message=MARK))
    assert (record.namespace, record.code) == ("decoy", "unregistered_reason")
    assert "ZXQ" not in _text(record)


def test_an_untrusted_exception_with_a_known_code_is_not_trusted() -> None:
    class ImposterError(Exception):
        code = "missing_mask_secret"

    record = reason_from_exception(ImposterError("x"))
    assert (record.namespace, record.code) == ("decoy", "unexpected_error")
    # The identical code on a trusted family is honoured: the control for the assertion above.
    real = reason_from_exception(
        ExecutionError(code=reasons.runtime.CYCLIC_WORK_ORDERING), phase=Phase.MASK
    )
    assert real.code == "cyclic_work_ordering"


def test_raising_str_repr_and_code_property_are_survived() -> None:
    record = reason_from_exception(_HostileError(MARK), phase=Phase.FINALIZE)
    assert (record.namespace, record.code) == ("decoy", "unexpected_error")
    assert "ZXQ" not in _text(record)


def test_hostile_code_on_a_trusted_family_does_not_break_the_boundary() -> None:
    class EvilError(ExecutionError):
        @property
        def code(self) -> str:  # type: ignore[override]
            raise RuntimeError(MARK)

        @code.setter
        def code(self, value: str) -> None:
            pass

    record = reason_from_exception(EvilError(code="x", message=MARK))
    assert record.code == "unregistered_reason"


def test_the_original_exception_is_preserved() -> None:
    exc = PlanCompileError(code=reasons.plan.UNKNOWN_PROVIDER, path=MARK, message=MARK)
    before = (exc.args, exc.code, exc.path, exc.message)
    record = reason_from_exception(exc, phase=Phase.COMPILE)
    assert (exc.args, exc.code, exc.path, exc.message) == before
    assert record.code == "unknown_provider"
    assert record.namespace == "plan"
    assert record.retry_class is RetryClass.CONFIGURATION_REPAIR
    assert MARK not in _text(record)


def test_a_path_in_an_oserror_never_reaches_the_record() -> None:
    exc = OSError(errno.ENOSPC, "No space left on device", f"/data/{MARK}/out.parquet")
    context = ReasonContext.build(operation=Operation.WRITE, storage_ref="output")
    record = reason_from_exception(exc, phase=Phase.PUBLISH, context=context)
    assert record.code == "storage_capacity_exhausted"
    assert record.retry_class is RetryClass.ENVIRONMENT_REPAIR
    assert record.context.storage_ref == "output"
    assert "ZXQ" not in _text(record)
    assert "/data" not in _text(record)


def test_capacity_needs_typed_evidence_and_a_publication_boundary() -> None:
    generic = reason_from_exception(
        OSError(errno.EIO, "io"),
        phase=Phase.PUBLISH,
        context=ReasonContext.build(operation=Operation.WRITE),
    )
    assert generic.code == "publication_failed"
    off_boundary = reason_from_exception(OSError(errno.ENOSPC, "full"), phase=Phase.MASK)
    assert off_boundary.code == "unexpected_error"
    quota = reason_from_exception(OSError(errno.EDQUOT, "quota"), phase=Phase.PUBLISH)
    assert quota.code == "storage_capacity_exhausted"


@pytest.mark.parametrize(
    "bad",
    [
        {"object_ref": MARK},
        {"object_ref": "a" * 500},
        {"storage_ref": "/etc/passwd"},
        {"storage_ref": "s3://bucket/key"},
    ],
)
def test_unapproved_identifiers_are_rejected_at_construction(bad: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        ReasonContext.build(**bad)


def test_untrusted_payload_fields_are_dropped_not_echoed() -> None:
    payload = reasons.ReasonRecord.for_reason(reasons.runtime.MISSING_MASK_SECRET).to_payload()
    payload["context"] = {
        "object_ref": MARK,
        "storage_ref": MARK,
        "extra": MARK,
        "counts": {"rows": 3, MARK: 1},
    }
    payload["retry_class"] = "retry_operation"  # a forged upgrade
    parsed = ReasonRecord.from_payload(payload)
    assert "ZXQ" not in _text(parsed)
    assert parsed.retry_class is RetryClass.CONFIGURATION_REPAIR  # recomputed from the registry
    assert dict(parsed.context.counts) == {"rows": 3}
    assert parsed.context.truncated


def test_a_pathologically_large_context_is_bounded() -> None:
    big = {f"k{i}": i for i in range(3_000_000)}
    parsed = ReasonContext.from_payload({"counts": big})
    assert parsed.truncated
    assert len(parsed.counts) == 0  # none of the keys is in the closed vocabulary
    wide = {"rows": 1, **{f"c{i}": 1 for i in range(2_000_000)}}
    parsed = ReasonContext.from_payload({"counts": wide})
    assert parsed.truncated and len(parsed.counts) <= reasons._record.MAX_COUNTS
    assert dict(parsed.counts) == {"rows": 1}


def test_unknown_identity_degrades_without_losing_the_audit_trail() -> None:
    payload = ReasonRecord.for_reason(reasons.runtime.MISSING_MASK_SECRET).to_payload()
    payload["code"] = "brand_new_future_code"
    parsed = ReasonRecord.from_payload(payload)
    assert (parsed.namespace, parsed.code) == ("decoy", "unknown_reason_from_future")
    assert parsed.context.original_identity == ("runtime", "brand_new_future_code")
    payload["code"] = MARK
    unsafe = ReasonRecord.from_payload(payload)
    assert unsafe.context.original_identity is None
    assert "ZXQ" not in _text(unsafe)


def test_unsupported_versions_are_fixed_diagnostics() -> None:
    payload = ReasonRecord.for_reason(reasons.runtime.MISSING_MASK_SECRET).to_payload()
    assert (
        ReasonRecord.from_payload({**payload, "schema_version": 2}).code
        == "unsupported_schema_version"
    )
    assert (
        ReasonRecord.from_payload({**payload, "registry_version": "2.0.0"}).code
        == "unsupported_schema_version"
    )
    assert ReasonRecord.from_payload("not a mapping").code == "reason_record_invalid"
    assert ReasonRecord.from_payload(None).code == "reason_record_invalid"


def test_the_first_failure_stays_primary_and_scopes_do_not_share_state() -> None:
    first, cleanup = ExecutionError(code=reasons.runtime.CYCLIC_WORK_ORDERING), OSError("x")
    with failure_scope() as outer:
        with failure_scope() as inner:
            a = capture_failure(first, phase=Phase.MASK)
            b = capture_failure(cleanup, phase=Phase.CLEANUP, secondary=True)
        assert b.code == a.code == "cyclic_work_ordering"
        assert [r.code for r in inner.secondary] == ["unexpected_error"]
        # The outer scope sees the same exception propagate and reuses its record.
        again = capture_failure(first, phase=Phase.UNKNOWN)
        assert again.code == "cyclic_work_ordering"
        assert outer.first is not None and outer.first.phase is Phase.MASK
    with failure_scope() as later:
        assert later.first is None and later.secondary == []


def test_config_error_without_a_code_maps_to_a_fixed_reason() -> None:
    record = reason_from_exception(ConfigError(MARK), phase=Phase.CONFIGURATION)
    assert (record.namespace, record.code) == ("config", "untagged")
    assert MARK not in _text(record)


def test_interrupts_are_control_outcomes() -> None:
    record = reason_from_exception(KeyboardInterrupt())
    assert record.code == "run_cancelled"
    assert record.outcome is reasons.Outcome.CANCELLED
