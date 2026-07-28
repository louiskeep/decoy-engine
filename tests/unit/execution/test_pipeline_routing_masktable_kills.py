"""Mutation-kill tests for the MASK-TABLE-ROWS + OOC-SIGNALS functions of
`execution/_pipeline_routing_signals.py` (TQ isolated-substrate grade).

Scope: the four size/routing-signal functions a `tq_mutate.py` survivor sweep
left un-pinned -- `largest_mask_table_rows`, `largest_mask_table_rows_from_profile`,
`_resolve_largest_mask_table_rows`, and `out_of_core_routing_signals`. Each test
pins the EXACT machine field the target mutant changes: the largest-mask row
count, the resident-vs-lazy per-table reconciliation, the `(rows, exact)` flag
threading, the warn-emission gate + warning category, and the inert OOC default
tuple. These are fast direct-unit tests -- they build tiny attribute-only stubs
and call the trampoline-exported functions directly, never the ~42s integration
harness.

Ledger: docs/quality/mutation-ledgers/execution_pipeline_routing_masktable.md.
"""

from __future__ import annotations

import warnings
from types import SimpleNamespace

import pytest

import decoy_engine.execution._pipeline_routing_signals as prs
from decoy_engine.execution._pipeline_routing_signals import (
    _resolve_largest_mask_table_rows,
    largest_mask_table_rows,
    largest_mask_table_rows_from_profile,
    out_of_core_routing_signals,
)


def _tbl(name: str, row_count: int, *, exact: bool = True) -> SimpleNamespace:
    """A minimal TableProfile stub: the only fields these functions read."""
    return SimpleNamespace(name=name, row_count=row_count, row_count_exact=exact, columns=())


def _profile(tables, *, relationships=()) -> SimpleNamespace:
    return SimpleNamespace(tables=tuple(tables), relationships=tuple(relationships))


def _src(num_rows: int) -> SimpleNamespace:
    """A resident-source stub: `_resolve` reads only `.num_rows` off it."""
    return SimpleNamespace(num_rows=num_rows)


# ===========================================================================
# largest_mask_table_rows  -- max resident mask-kind row count, else None
# ===========================================================================


def test_lmtr_mut1_mask_rows_not_nulled() -> None:
    # #1 nulls the mask_rows list: the resident mask source's count must survive.
    assert largest_mask_table_rows({"m": _src(50)}, table_kinds={"m": "mask"}) == 50


def test_lmtr_mut2_kind_lookup_uses_table_name() -> None:
    # #2 keys table_kinds.get on None, not the table name -> nothing matches.
    assert largest_mask_table_rows({"m": _src(50)}, table_kinds={"m": "mask"}) == 50


def test_lmtr_mut3_selects_mask_not_the_complement() -> None:
    # #3 flips == to !=: it would return the NON-mask source's count instead.
    got = largest_mask_table_rows(
        {"m": _src(50), "g": _src(999)}, table_kinds={"m": "mask", "g": "generate"}
    )
    assert got == 50


def test_lmtr_mut4_mask_literal_not_placeholder() -> None:
    # #4 compares against the "XXmaskXX" placeholder -> never matches.
    assert largest_mask_table_rows({"m": _src(50)}, table_kinds={"m": "mask"}) == 50


def test_lmtr_mut5_mask_literal_is_lowercase() -> None:
    # #5 upper-cases the literal to "MASK" -> never matches "mask".
    assert largest_mask_table_rows({"m": _src(50)}, table_kinds={"m": "mask"}) == 50


def test_lmtr_mut6_max_over_the_row_list() -> None:
    # #6 rewrites max(mask_rows) -> max(None) (a TypeError); the real max is 80.
    assert largest_mask_table_rows({"m": _src(80)}, table_kinds={"m": "mask"}) == 80


# ===========================================================================
# largest_mask_table_rows_from_profile  -- (rows, exact) of largest mask table
# ===========================================================================


def test_lmtrfp_mut1_mask_tables_not_nulled() -> None:
    # #1 nulls mask_tables -> the None-guard fires and returns None.
    prof = _profile([_tbl("t", 100)])
    assert largest_mask_table_rows_from_profile(prof, table_kinds={"t": "mask"}) == (100, True)


def test_lmtrfp_mut2_kind_lookup_uses_table_name() -> None:
    # #2 keys table_kinds.get on None -> no mask table found -> None.
    prof = _profile([_tbl("t", 100)])
    assert largest_mask_table_rows_from_profile(prof, table_kinds={"t": "mask"}) == (100, True)


def test_lmtrfp_mut3_selects_mask_not_complement() -> None:
    # #3 flips == to !=: it would pick the generate table (rows=7) instead.
    prof = _profile([_tbl("t", 100, exact=True), _tbl("g", 7, exact=True)])
    got = largest_mask_table_rows_from_profile(prof, table_kinds={"t": "mask", "g": "generate"})
    assert got == (100, True)


def test_lmtrfp_mut4_mask_literal_not_placeholder() -> None:
    prof = _profile([_tbl("t", 100)])
    assert largest_mask_table_rows_from_profile(prof, table_kinds={"t": "mask"}) == (100, True)


def test_lmtrfp_mut5_mask_literal_is_lowercase() -> None:
    prof = _profile([_tbl("t", 100)])
    assert largest_mask_table_rows_from_profile(prof, table_kinds={"t": "mask"}) == (100, True)


def test_lmtrfp_mut6_empty_guard_keeps_not() -> None:
    # #6 drops `not`: with a real mask table it wrongly returns None.
    prof = _profile([_tbl("t", 100)])
    assert largest_mask_table_rows_from_profile(prof, table_kinds={"t": "mask"}) == (100, True)


def test_lmtrfp_mut7_largest_bound_to_max() -> None:
    # #7 sets largest = None -> None.row_count raises; the real result is (100, True).
    prof = _profile([_tbl("t", 100)])
    assert largest_mask_table_rows_from_profile(prof, table_kinds={"t": "mask"}) == (100, True)


def test_lmtrfp_mut8_max_iterable_is_mask_tables() -> None:
    # #8 passes None as max's iterable -> TypeError.
    prof = _profile([_tbl("t", 100)])
    assert largest_mask_table_rows_from_profile(prof, table_kinds={"t": "mask"}) == (100, True)


def test_lmtrfp_mut9_max_key_is_row_count() -> None:
    # #9 sets key=None -> None is not callable -> TypeError.
    prof = _profile([_tbl("t", 100)])
    assert largest_mask_table_rows_from_profile(prof, table_kinds={"t": "mask"}) == (100, True)


def test_lmtrfp_mut10_max_takes_the_iterable_positional() -> None:
    # #10 drops the positional iterable -> max() missing argument -> TypeError.
    prof = _profile([_tbl("t", 100)])
    assert largest_mask_table_rows_from_profile(prof, table_kinds={"t": "mask"}) == (100, True)


def test_lmtrfp_mut11_max_uses_a_key() -> None:
    # #11 drops key= entirely: max() then compares TableProfile stubs directly,
    # which are unorderable -> TypeError. Two tables force the comparison.
    prof = _profile([_tbl("a", 10, exact=True), _tbl("b", 99, exact=False)])
    got = largest_mask_table_rows_from_profile(prof, table_kinds={"a": "mask", "b": "mask"})
    assert got == (99, False)


def test_lmtrfp_mut12_key_reads_row_count() -> None:
    # #12 makes the key lambda return None for every table -> max returns the
    # FIRST (rows=10), not the largest (rows=99).
    prof = _profile([_tbl("a", 10, exact=True), _tbl("b", 99, exact=False)])
    got = largest_mask_table_rows_from_profile(prof, table_kinds={"a": "mask", "b": "mask"})
    assert got == (99, False)


# ===========================================================================
# _resolve_largest_mask_table_rows  -- per-table resident/lazy reconciliation
# ===========================================================================

# --- selection of the profile mask set (#1-#6): a lazy mask table (rows=100,
#     absent from caller_sources) must resolve to (100, True); each mutant
#     instead empties/miskeys the set and falls through to the empty-caller
#     fallback (None), or wrongly enters the empty-guard branch. ---


def _lazy_single_mask():
    return _profile([_tbl("t", 100, exact=True)]), {}, {"t": "mask"}


def test_resolve_mut1_mask_tables_not_nulled() -> None:
    prof, caller, kinds = _lazy_single_mask()
    assert _resolve_largest_mask_table_rows(prof, caller_sources=caller, table_kinds=kinds) == (
        100,
        True,
    )


def test_resolve_mut2_kind_lookup_uses_table_name() -> None:
    prof, caller, kinds = _lazy_single_mask()
    assert _resolve_largest_mask_table_rows(prof, caller_sources=caller, table_kinds=kinds) == (
        100,
        True,
    )


def test_resolve_mut3_selects_mask_not_complement() -> None:
    # #3 flips == to !=: it would resolve the generate table (rows=7) instead.
    prof = _profile([_tbl("t", 100, exact=True), _tbl("g", 7, exact=True)])
    got = _resolve_largest_mask_table_rows(
        prof, caller_sources={}, table_kinds={"t": "mask", "g": "generate"}
    )
    assert got == (100, True)


def test_resolve_mut4_mask_literal_not_placeholder() -> None:
    prof, caller, kinds = _lazy_single_mask()
    assert _resolve_largest_mask_table_rows(prof, caller_sources=caller, table_kinds=kinds) == (
        100,
        True,
    )


def test_resolve_mut5_mask_literal_is_lowercase() -> None:
    prof, caller, kinds = _lazy_single_mask()
    assert _resolve_largest_mask_table_rows(prof, caller_sources=caller, table_kinds=kinds) == (
        100,
        True,
    )


def test_resolve_mut6_empty_guard_keeps_not() -> None:
    # #6 drops `not`: with a real mask table it wrongly returns the empty
    # fallback (None) instead of iterating the mask set.
    prof, caller, kinds = _lazy_single_mask()
    assert _resolve_largest_mask_table_rows(prof, caller_sources=caller, table_kinds=kinds) == (
        100,
        True,
    )


# --- the no-mask-in-profile fallback (#7-#11): profile carries no mask table,
#     so the resolver delegates to largest_mask_table_rows over the resident
#     caller sources (rows=50) and stamps exact=True. ---


def _fallback_case():
    return _profile([]), {"m": _src(50)}, {"m": "mask"}


def test_resolve_mut7_fallback_passes_caller_sources() -> None:
    # #7 passes None for caller_sources -> None.items() raises inside the delegate.
    prof, caller, kinds = _fallback_case()
    assert _resolve_largest_mask_table_rows(prof, caller_sources=caller, table_kinds=kinds) == (
        50,
        True,
    )


def test_resolve_mut8_fallback_passes_table_kinds() -> None:
    # #8 passes table_kinds=None -> None.get(...) raises inside the delegate.
    prof, caller, kinds = _fallback_case()
    assert _resolve_largest_mask_table_rows(prof, caller_sources=caller, table_kinds=kinds) == (
        50,
        True,
    )


def test_resolve_mut9_fallback_keeps_caller_positional() -> None:
    # #9 drops the caller_sources positional -> missing-argument TypeError.
    prof, caller, kinds = _fallback_case()
    assert _resolve_largest_mask_table_rows(prof, caller_sources=caller, table_kinds=kinds) == (
        50,
        True,
    )


def test_resolve_mut10_fallback_keeps_table_kinds_kw() -> None:
    # #10 drops the table_kinds keyword -> missing-argument TypeError.
    prof, caller, kinds = _fallback_case()
    assert _resolve_largest_mask_table_rows(prof, caller_sources=caller, table_kinds=kinds) == (
        50,
        True,
    )


def test_resolve_mut11_fallback_exact_is_true() -> None:
    # #11 flips the fallback exact flag True -> False.
    prof, caller, kinds = _fallback_case()
    assert _resolve_largest_mask_table_rows(prof, caller_sources=caller, table_kinds=kinds) == (
        50,
        True,
    )


# --- resident vs lazy per-table pick (#15, #16, #17): the resident source
#     (num_rows=555) is authoritative and always exact; the profile count
#     (100, estimate) must NOT be substituted. ---


def _resident_mismatch_case():
    # exact=False so no reconciliation warning fires (isolates the pick logic).
    return _profile([_tbl("t", 100, exact=False)]), {"t": _src(555)}, {"t": "mask"}


def test_resolve_mut15_reads_the_resident_source() -> None:
    # #15 hardcodes resident=None -> it would fall to the lazy count (100, False).
    prof, caller, kinds = _resident_mismatch_case()
    assert _resolve_largest_mask_table_rows(prof, caller_sources=caller, table_kinds=kinds) == (
        555,
        True,
    )


def test_resolve_mut16_resident_lookup_uses_table_name() -> None:
    # #16 looks caller_sources up by None -> misses the resident source.
    prof, caller, kinds = _resident_mismatch_case()
    assert _resolve_largest_mask_table_rows(prof, caller_sources=caller, table_kinds=kinds) == (
        555,
        True,
    )


def test_resolve_mut17_resident_present_branch() -> None:
    # #17 inverts `is not None`: a present resident would take the lazy branch
    # (100, False) -- or, when truly absent, dereference None.
    prof, caller, kinds = _resident_mismatch_case()
    assert _resolve_largest_mask_table_rows(prof, caller_sources=caller, table_kinds=kinds) == (
        555,
        True,
    )


# --- exact flag on the resident branch (#19, #20): a resident table is always
#     exact=True; the second field must be True. ---


def _resident_match_case():
    return _profile([_tbl("t", 555, exact=True)]), {"t": _src(555)}, {"t": "mask"}


def test_resolve_mut19_resident_exact_is_true() -> None:
    # #19 sets table_exact=None on the resident branch.
    prof, caller, kinds = _resident_match_case()
    assert _resolve_largest_mask_table_rows(prof, caller_sources=caller, table_kinds=kinds) == (
        555,
        True,
    )


def test_resolve_mut20_resident_exact_not_false() -> None:
    # #20 sets table_exact=False on the resident branch.
    prof, caller, kinds = _resident_match_case()
    assert _resolve_largest_mask_table_rows(prof, caller_sources=caller, table_kinds=kinds) == (
        555,
        True,
    )


# --- the reconciliation-warning GATE (#21, #22): warn iff (profile exact AND
#     counts disagree). Each mutant widens/flips the gate so it warns on an
#     input where the original stays silent. ---


def test_resolve_mut21_warn_gate_is_and_not_or() -> None:
    # #21: `and` -> `or`. With a CSV estimate (exact=False) that disagrees
    # (100 vs 555), the original must NOT warn; `or` would.
    prof = _profile([_tbl("t", 100, exact=False)])
    caller = {"t": _src(555)}
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        got = _resolve_largest_mask_table_rows(
            prof, caller_sources=caller, table_kinds={"t": "mask"}
        )
    assert got == (555, True)


def test_resolve_mut22_warn_gate_disagreement_is_neq() -> None:
    # #22: `!=` -> `==`. When an exact profile count AGREES with the resident
    # count (555 == 555), the original must NOT warn; `==` would.
    prof = _profile([_tbl("t", 555, exact=True)])
    caller = {"t": _src(555)}
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        got = _resolve_largest_mask_table_rows(
            prof, caller_sources=caller, table_kinds={"t": "mask"}
        )
    assert got == (555, True)


def test_resolve_warn_category_is_runtimewarning() -> None:
    # Kills #24 (category->None coerces to UserWarning), #25 (stacklevel->None
    # raises TypeError), #26 (message becomes the class, category->UserWarning),
    # and #27 (category dropped -> UserWarning). The reconciliation warning is
    # contractually a RuntimeWarning; an exact profile count (100) disagreeing
    # with the resident count (200) fires it.
    prof = _profile([_tbl("t", 100, exact=True)])
    caller = {"t": _src(200)}
    with pytest.warns(RuntimeWarning):
        got = _resolve_largest_mask_table_rows(
            prof, caller_sources=caller, table_kinds={"t": "mask"}
        )
    assert got == (200, True)


# --- lazy-branch count + flag threading (#30, #31): an absent mask table
#     resolves to its profile (row_count, row_count_exact). ---


def test_resolve_mut30_lazy_count_is_row_count() -> None:
    # #30 nulls the lazy table_rows -> the max would collapse to None.
    prof, caller, kinds = _lazy_single_mask()
    assert _resolve_largest_mask_table_rows(prof, caller_sources=caller, table_kinds=kinds) == (
        100,
        True,
    )


def test_resolve_mut31_lazy_exact_is_row_count_exact() -> None:
    # #31 nulls the lazy table_exact -> the exact flag would be None, not True.
    prof, caller, kinds = _lazy_single_mask()
    assert _resolve_largest_mask_table_rows(prof, caller_sources=caller, table_kinds=kinds) == (
        100,
        True,
    )


def test_resolve_mut34_strict_greater_than_keeps_first_on_tie() -> None:
    # #34: `>` -> `>=`. Two lazy mask tables with EQUAL rows (100) but different
    # exact flags. Strict `>` keeps the first table's exact (True); `>=` would
    # overwrite it with the later table's flag (False).
    prof = _profile([_tbl("t1", 100, exact=True), _tbl("t2", 100, exact=False)])
    got = _resolve_largest_mask_table_rows(
        prof, caller_sources={}, table_kinds={"t1": "mask", "t2": "mask"}
    )
    assert got == (100, True)


# ===========================================================================
# out_of_core_routing_signals  -- the (compatible, code, rows, exact) tuple
# ===========================================================================

# The admission sub-call is patched to a known verdict so the gate + default
# logic is isolated from the heavy static compatibility check.


def _patch_admission(monkeypatch, verdict=(True, "CODE")):
    monkeypatch.setattr(prs, "out_of_core_admission", lambda plan, *, registry, graph: verdict)


def _fk_mask_profile():
    return _profile([_tbl("t", 100, exact=True)], relationships=("rel",))


def test_oocrs_mut1_gate_is_negated(monkeypatch) -> None:
    # #1 drops the leading `not`: a relationship+mask job (which should PROCEED)
    # would instead return the inert default.
    _patch_admission(monkeypatch)
    got = out_of_core_routing_signals(
        _fk_mask_profile(),
        plan=None,
        registry=None,
        graph=None,
        caller_sources={},
        table_kinds={"t": "mask"},
        has_mask_table=True,
    )
    assert got == (True, "CODE", 100, True)


def test_oocrs_mut2_gate_is_and_not_or(monkeypatch) -> None:
    # #2 `and` -> `or`: relationships present but has_mask_table=False must
    # return the default; `or` would proceed to admission.
    _patch_admission(monkeypatch)
    got = out_of_core_routing_signals(
        _fk_mask_profile(),
        plan=None,
        registry=None,
        graph=None,
        caller_sources={},
        table_kinds={"t": "mask"},
        has_mask_table=False,
    )
    assert got == (False, None, None, True)


def test_oocrs_mut3_reads_relationships_off_profile(monkeypatch) -> None:
    # #3 getattr(None, ...): the relationships probe is hardcoded to None, so a
    # real FK+mask job would wrongly return the default.
    _patch_admission(monkeypatch)
    got = out_of_core_routing_signals(
        _fk_mask_profile(),
        plan=None,
        registry=None,
        graph=None,
        caller_sources={},
        table_kinds={"t": "mask"},
        has_mask_table=True,
    )
    assert got == (True, "CODE", 100, True)


def test_oocrs_mut7_getattr_default_is_none() -> None:
    # #7 drops getattr's None default: a profile with no `relationships`
    # attribute would raise AttributeError instead of returning the default.
    prof = SimpleNamespace(tables=(_tbl("t", 100),))  # no `relationships` attr
    got = out_of_core_routing_signals(
        prof,
        plan=None,
        registry=None,
        graph=None,
        caller_sources={},
        table_kinds={"t": "mask"},
        has_mask_table=True,
    )
    assert got == (False, None, None, True)


def test_oocrs_mut8_attr_name_is_relationships(monkeypatch) -> None:
    # #8 misspells the attribute ("XXrelationshipsXX") -> always None -> default.
    _patch_admission(monkeypatch)
    got = out_of_core_routing_signals(
        _fk_mask_profile(),
        plan=None,
        registry=None,
        graph=None,
        caller_sources={},
        table_kinds={"t": "mask"},
        has_mask_table=True,
    )
    assert got == (True, "CODE", 100, True)


def test_oocrs_mut9_attr_name_is_lowercase(monkeypatch) -> None:
    # #9 upper-cases the attribute ("RELATIONSHIPS") -> always None -> default.
    _patch_admission(monkeypatch)
    got = out_of_core_routing_signals(
        _fk_mask_profile(),
        plan=None,
        registry=None,
        graph=None,
        caller_sources={},
        table_kinds={"t": "mask"},
        has_mask_table=True,
    )
    assert got == (True, "CODE", 100, True)


def test_oocrs_mut10_default_compatible_is_false() -> None:
    # #10 flips the default's first field False -> True. Empty relationships =>
    # the inert default path.
    prof = _profile([_tbl("t", 100)], relationships=())
    got = out_of_core_routing_signals(
        prof,
        plan=None,
        registry=None,
        graph=None,
        caller_sources={},
        table_kinds={"t": "mask"},
        has_mask_table=True,
    )
    assert got == (False, None, None, True)


def test_oocrs_mut11_default_exact_is_true() -> None:
    # #11 flips the default's last field True -> False.
    prof = _profile([_tbl("t", 100)], relationships=())
    got = out_of_core_routing_signals(
        prof,
        plan=None,
        registry=None,
        graph=None,
        caller_sources={},
        table_kinds={"t": "mask"},
        has_mask_table=True,
    )
    assert got == (False, None, None, True)
