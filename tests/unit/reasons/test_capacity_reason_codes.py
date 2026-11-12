"""The capacity reason-code set and its reasoned exclusions (one registry walk, no second list)."""

from __future__ import annotations

import re

import pytest

from decoy_engine import errors, reasons

_CAPACITY_WORDS = re.compile(r"memory|budget|capacity|oom|disk|spill|resource|rss")

EXPECTED = {
    "generation_budget_exceeded",
    "out_of_core_insufficient_memory",
    "out_of_core_fanin_exceeds_budget",
    "out_of_core_faker_pool_budget_exceeded",
    "out_of_core_reorder_budget_too_small",
    "pool_exceeds_cache_budget",
    "isolated_worker_out_of_memory",
    "process_out_of_memory",
}


def test_members() -> None:
    assert isinstance(errors.CAPACITY_REASON_CODES, frozenset)
    assert EXPECTED == errors.CAPACITY_REASON_CODES
    assert "CAPACITY_REASON_CODES" in errors.__all__
    for code in errors.CAPACITY_REASON_CODES:
        assert reasons.is_registered(code), code


def test_registry_walk() -> None:
    walked = {code for _namespace, code in reasons.REASONS if _CAPACITY_WORDS.search(code)}
    assert walked, "the walk found no capacity-looking code"
    capacity = set(errors.CAPACITY_REASON_CODES)
    excluded = set(errors.CAPACITY_EXCLUSIONS)
    assert not capacity & excluded
    assert walked == capacity | excluded
    for code, why in errors.CAPACITY_EXCLUSIONS.items():
        assert why.strip(), code


def test_exclusions_are_read_only() -> None:
    with pytest.raises(TypeError):
        errors.CAPACITY_EXCLUSIONS["any_code"] = "a reason"  # type: ignore[index]
    with pytest.raises(TypeError):
        del errors.CAPACITY_EXCLUSIONS[next(iter(errors.CAPACITY_EXCLUSIONS))]  # type: ignore[attr-defined]


def test_exclusion_texts_are_distinct_per_cause() -> None:
    texts = errors.CAPACITY_EXCLUSIONS
    unbudgeted = texts[reasons.runtime.OUT_OF_CORE_REORDER_UNBUDGETED]
    assert "not set" in unbudgeted
    assert "malformed" in texts[reasons.plan.DP_BUDGET_DECLARATION_MALFORMED]
    assert (
        texts[reasons.runtime.OUT_OF_CORE_TEMP_DISK_EXCEEDED]
        == texts[reasons.decoy.STORAGE_CAPACITY_EXHAUSTED]
    )


def test_full_frame_oom_risk_refusals_stay_excluded() -> None:
    for code in (
        reasons.runtime.FK_FULL_FRAME_OOM_RISK_REJECTED,
        reasons.runtime.FK_FULL_FRAME_OOM_RISK_REJECTED_ESTIMATED,
    ):
        assert code in errors.CAPACITY_EXCLUSIONS
        assert code not in errors.CAPACITY_REASON_CODES
