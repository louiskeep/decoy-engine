"""A5: the three match modes, strongest first, one count per cell (F34 rules M1 to M4, I5).

Report-level facts (status, reasons, short count) come from feeding the matcher's counts through
E0's ``CanaryVerdictPolicy``; the end-to-end run through ``scan_canary_artifacts`` is E3's A5r.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from decoy_engine.verification.canary import (
    CanaryBindingSource,
    CanaryObservedReason,
    CanaryPolicyInputError,
    CanaryReason,
    CanaryStatus,
    CanaryVerdictPolicy,
)
from decoy_engine.verification.canary._match import (
    MAX_CELL_UTF8_BYTES,
    CellOutcome,
    CellTally,
    build_patterns,
    screen_canaries,
    tally_cells,
)

R = CanaryReason
OBS = CanaryObservedReason
ZERO = {
    "exact_cell": 0,
    "exact_span": 0,
    "nfc_whitespace_span": 0,
    "no_match": 0,
    "oversized": 0,
    "uncompared": 0,
}


@dataclass(frozen=True)
class Case:
    canaries: tuple[str, ...]
    cells: tuple[str, ...]
    tally: dict[str, int] = field(default_factory=dict)
    status: CanaryStatus = CanaryStatus.COMPLETE
    reasons: tuple[CanaryReason, ...] = ()
    short: int = 0
    span_min: int = 8


@dataclass(frozen=True)
class Verdict:
    tally: CellTally | None
    short: int | None
    status: CanaryStatus
    reasons: tuple[CanaryReason, ...]


def _verdict(canaries: tuple[str, ...], cells: tuple[str, ...], span_min: int) -> Verdict:
    binding = CanaryBindingSource.PUBLICATION_RECORD
    if not screen_canaries(canaries):
        kw = {"validation_failed": True, "binding_source": binding, "observed": frozenset()}
        status = CanaryVerdictPolicy.status(**kw)
        reasons = CanaryVerdictPolicy.reasons(**kw, has_findings=False, has_short_excluded=False)
        return Verdict(None, None, status, reasons)
    patterns = build_patterns(canaries, span_min)
    tally = tally_cells(patterns, cells)
    observed = set()
    if tally.uncompared:
        observed.add(OBS.CELL_UNCOMPARED)
    if tally.oversized:
        observed.add(OBS.RESOURCE_LIMIT)
    kw = {"validation_failed": False, "binding_source": binding, "observed": frozenset(observed)}
    short = patterns.counts.excluded_short_canary_count
    status = CanaryVerdictPolicy.status(**kw)
    reasons = CanaryVerdictPolicy.reasons(
        **kw, has_findings=tally.finding_cell_count > 0, has_short_excluded=short > 0
    )
    return Verdict(tally, short, status, reasons)


def _check(case: Case) -> None:
    got = _verdict(case.canaries, case.cells, case.span_min)
    assert got.status is case.status
    assert got.reasons == case.reasons
    if got.status is CanaryStatus.REJECTED:
        with pytest.raises(CanaryPolicyInputError):
            build_patterns(case.canaries, case.span_min)
        return
    assert got.tally is not None
    expected = {**ZERO, **case.tally}
    assert got.tally.as_counts() == expected
    assert sum(expected.values()) == len(case.cells)
    assert got.short == case.short


INCOMPLETE = CanaryStatus.INCOMPLETE
MODE_MATRIX = {
    "5a_whole_cell_is_exact_cell_only": Case(
        ("secret-value-1",), ("secret-value-1",), {"exact_cell": 1}, reasons=(R.MATCH,)
    ),
    "5b_contiguous_span": Case(
        ("ABCDEFGH",), ("xxABCDEFGHyy",), {"exact_span": 1}, reasons=(R.MATCH,)
    ),
    "5c_decomposed_cell_composed_canary": Case(
        ("r\u00e9sum\u00e9-0042",),
        ("xxre\u0301sume\u0301-0042yy",),
        {"nfc_whitespace_span": 1},
        reasons=(R.MATCH,),
    ),
    "5d_nbsp_and_tab_run_vs_one_space": Case(
        ("code 4471 99",),
        ("id:code\u00a0\t4471 99;",),
        {"nfc_whitespace_span": 1},
        reasons=(R.MATCH,),
    ),
    "5e_case_differs": Case(("ABCDEFGH",), ("xxabcdefghyy",), {"no_match": 1}),
    "5f_short_canary_exact_cell_only": Case(
        ("AB-12",),
        ("xxAB-12yy", "AB-12"),
        {"no_match": 1, "exact_cell": 1},
        reasons=(R.MATCH, R.SPAN_SHORT_EXCLUDED),
        short=1,
    ),
    "5g_fullwidth_digits_nfkc_only": Case(
        ("12345678",), ("\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18",), {"no_match": 1}
    ),
    "5h_split_across_two_cells": Case(("ABCDEFGH",), ("xxABCD", "EFGHyy"), {"no_match": 2}),
    "5i_canary_with_post_13_0_mark_rejected": Case(
        ("xxxxxx\u00e9\u1ac1",),
        ("xxxxxx\u00e9\u1ac1",),
        status=CanaryStatus.REJECTED,
        reasons=(R.INPUT_REJECTED,),
    ),
    "5j_cell_with_post_13_0_mark_uncompared": Case(
        ("xxxxxxx\u00e9",),
        ("xxxxxxxe\u0301\u1ac1",),
        {"uncompared": 1},
        status=INCOMPLETE,
        reasons=(R.SCAN_INCOMPLETE, R.CELL_UNCOMPARED),
    ),
    "5k_whitespace_canary_short_after_normalization": Case(
        ("A       ",),
        ("xA\t\t\t\t\t\t\tx",),
        {"no_match": 1},
        reasons=(R.SPAN_SHORT_EXCLUDED,),
        short=1,
    ),
}


@pytest.mark.parametrize("case", list(MODE_MATRIX))
def test_mode_matrix(case: str) -> None:
    _check(MODE_MATRIX[case])


MIB = MAX_CELL_UTF8_BYTES
SELF_ATTACK = {
    "cell_padded_is_span_not_exact_cell": Case(
        ("secret-value-1",), (" secret-value-1 ",), {"exact_span": 1}, reasons=(R.MATCH,)
    ),
    "canary_leading_whitespace_not_trimmed": Case((" 12345678",), ("x12345678",), {"no_match": 1}),
    "cell_only_whitespace": Case(("ab cd ef gh",), ("\t \u3000\u00a0", " "), {"no_match": 2}),
    "cell_exactly_1_mib_is_compared": Case(
        ("ABCDEFGH",), ("x" * (MIB - 8) + "ABCDEFGH",), {"exact_span": 1}, reasons=(R.MATCH,)
    ),
    "cell_1_mib_plus_one_byte_oversized": Case(
        ("ABCDEFGH",),
        ("x" * (MIB - 7) + "ABCDEFGH",),
        {"oversized": 1},
        status=INCOMPLETE,
        reasons=(R.SCAN_INCOMPLETE, R.RESOURCE_LIMIT),
    ),
    "cell_oversized_by_multibyte_scalars": Case(
        ("ABCDEFGH",),
        ("\U0001f600" * (MIB // 4) + "A",),
        {"oversized": 1},
        status=INCOMPLETE,
        reasons=(R.SCAN_INCOMPLETE, R.RESOURCE_LIMIT),
    ),
    "canary_normalizes_below_span_minimum": Case(
        ("e\u0301e\u0301e\u0301e\u0301",),
        ("xx\u00e9\u00e9\u00e9\u00e9xx", "e\u0301e\u0301e\u0301e\u0301"),
        {"no_match": 1, "exact_cell": 1},
        reasons=(R.MATCH, R.SPAN_SHORT_EXCLUDED),
        short=1,
    ),
    "post_13_0_mark_in_cell_matching_exact_span_counts_span": Case(
        ("ABCDEFGH",),
        ("ABCDEFGH\u1ac1",),
        {"exact_span": 1},
        reasons=(R.MATCH,),
    ),
    "strongest_mode_wins_once_per_cell": Case(
        ("ABCDEFGH", "ABCDEFGHIJ", "A B C D E F"),
        ("ABCDEFGH", "xABCDEFGHIJ", "A\tB C D E F", "zzz"),
        {"exact_cell": 1, "exact_span": 1, "nfc_whitespace_span": 1, "no_match": 1},
        reasons=(R.MATCH,),
    ),
}


@pytest.mark.parametrize("case", list(SELF_ATTACK))
def test_self_attack(case: str) -> None:
    _check(SELF_ATTACK[case])


def test_normalization_collision_counts_once() -> None:
    canaries = (
        "r\u00e9sum\u00e9-0042",
        "re\u0301sume\u0301-0042",
        "acct 1234 5678",
        "acct\t1234\u00a05678",
        "ZZZZZZZZ",
    )
    counts = build_patterns(canaries, 8).counts
    assert counts.exact_pattern_count == 5
    assert counts.normalized_pattern_count == 3
    assert counts.normalization_collision_count == 2
    assert counts.excluded_short_canary_count == 0
    patterns = build_patterns(canaries, 8)
    assert tally_cells(patterns, ("xxr\u00e9sume\u0301-0042",)).nfc_whitespace_span == 1


def test_outcome_and_tally_carry_no_text() -> None:
    marker = "MARKER-7f3a9c"
    patterns = build_patterns((marker,), 8)
    tally = tally_cells(patterns, ("xx" + marker, marker, "nothing"))
    assert tally.finding_cell_count == 2
    for obj in (tally, patterns.counts, patterns, *CellOutcome):
        assert marker not in repr(obj)
        assert marker not in str(obj)
    assert all(type(v) is int for v in tally.as_counts().values())


def test_matcher_rejects_wrong_input_types() -> None:
    with pytest.raises(CanaryPolicyInputError):
        build_patterns(["ABCDEFGH"], 8)  # type: ignore[arg-type]
    with pytest.raises(CanaryPolicyInputError):
        build_patterns((b"ABCDEFGH",), 8)  # type: ignore[arg-type]
    with pytest.raises(CanaryPolicyInputError):
        build_patterns(("ABCDEFGH",), True)
    with pytest.raises(CanaryPolicyInputError):
        build_patterns(("ABCDEFGH",), 0)
