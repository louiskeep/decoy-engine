"""F22 4.5, 8: the publication errors reach registered reasons, and outcomes map to typed errors
(slice a3).

Every publication failure family is classified by `reason_adapter` (no `UNCLASSIFIED` entry):
`PublicationRefused` and `PublicationFailed` carry a registered `decoy` code, and the four
errno-only file-access errors of slice a1b have one fixed meaning each. Their text is a fixed
summary plus a closed token.
"""

from __future__ import annotations

import errno

import pytest

PUBLICATION_CODES = (
    "publication_lock_unavailable",
    "publication_lock_timeout",
    "publication_target_occupied",
    "publication_operation_conflict",
    "publication_commit_uncertain",
    "publication_commit_not_visible",
    "publication_vault_mismatch",
    "publication_vault_conflict",
    "publication_write_in_progress",
    "publication_commit_in_progress",
    "publication_recovery_in_progress",
    "publication_attempt_sealed",
    "publication_attempt_aborted",
)

HEX = "1" * 64


def _receipt(outcome: str, code: str | None, **over: object) -> object:
    from decoy_engine.publication import CommitReceipt

    fields: dict[str, object] = {
        "outcome": outcome,
        "operation_id": "op-1",
        "reason_code": code,
        "result_id": HEX,
        "descriptor_digest": None,
        "vault_digest": None,
        "artifacts": (),
        "visible": False,
        "durable": False,
        "os_error": None,
    }
    fields.update(over)
    return CommitReceipt(**fields)  # type: ignore[arg-type]


def test_the_publication_codes_are_registered_decoy_publication_reasons() -> None:
    from decoy_engine import reasons

    for code in PUBLICATION_CODES:
        spec = reasons.find("decoy", code)
        assert spec is not None, code
        assert spec.owner == "decoy_engine.publication"
        assert spec.default_rule.outcome is reasons.Outcome.PUBLICATION


@pytest.mark.parametrize(
    "make, code",
    [
        (lambda m: m.LockUnavailable(errno.ENOLCK), "publication_lock_unavailable"),
        (lambda m: m.LockTimeout(errno.EWOULDBLOCK), "publication_lock_timeout"),
        (lambda m: m.CommitUncertain(errno.EIO), "publication_commit_uncertain"),
        (lambda m: m.LeafTooLarge(errno.EFBIG), "publication_failed"),
    ],
)
def test_file_access_publication_errors_have_fixed_reasons(make: object, code: str) -> None:
    from decoy_engine import fileaccess
    from decoy_engine.reason_adapter import reason_from_exception
    from decoy_engine.reasons import Phase

    record = reason_from_exception(make(fileaccess), phase=Phase.PUBLISH)  # type: ignore[operator]
    assert (record.namespace, record.code) == ("decoy", code)


@pytest.mark.parametrize(
    "outcome, code, raised",
    [
        ("refused", "publication_target_occupied", "PublicationRefused"),
        ("refused", "publication_lock_timeout", "PublicationRefused"),
        ("operation_conflict", "publication_operation_conflict", "PublicationRefused"),
        ("refused", "publication_vault_mismatch", "PublicationRefused"),
        ("failed", "publication_failed", "PublicationFailed"),
    ],
)
def test_raise_for_outcome_maps_each_refusal_to_its_registered_reason(
    outcome: str, code: str, raised: str
) -> None:
    from decoy_engine import publication
    from decoy_engine.reason_adapter import reason_from_exception
    from decoy_engine.reasons import Phase

    receipt = _receipt(outcome, code, os_error="EIO")
    with pytest.raises(getattr(publication, raised)) as caught:
        publication.raise_for_outcome(receipt)
    exc = caught.value
    assert exc.receipt is receipt and exc.code == code
    assert str(exc) == f"{exc.summary}: {code}"
    record = reason_from_exception(exc, phase=Phase.PUBLISH)
    assert (record.namespace, record.code) == ("decoy", code)


def test_raise_for_outcome_passes_success_and_raises_commit_uncertain() -> None:
    from decoy_engine import fileaccess, publication

    for outcome in ("published", "already_committed"):
        receipt = _receipt(outcome, None, visible=True, durable=True)
        assert publication.raise_for_outcome(receipt) is receipt
    uncertain = _receipt(
        "commit_uncertain", "publication_commit_uncertain", visible=True, os_error="EIO"
    )
    with pytest.raises(fileaccess.CommitUncertain) as caught:
        publication.raise_for_outcome(uncertain)
    assert caught.value.errno == errno.EIO


def test_a_receipt_code_must_be_a_registered_decoy_reason() -> None:
    with pytest.raises(ValueError, match="reason_code"):
        _receipt("refused", "publication_not_a_code")
    with pytest.raises(ValueError, match="reason_code"):
        _receipt("refused", "missing_mask_secret")  # registered, but in another namespace


def test_refusal_errors_take_registered_codes_only() -> None:
    from decoy_engine import publication

    receipt = _receipt("refused", "publication_target_occupied")
    with pytest.raises(ValueError):
        publication.PublicationRefused("publication_not_a_code", receipt)  # type: ignore[arg-type]
