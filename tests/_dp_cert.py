"""Shared DP certification predicate for the test suite (CI fix #4).

`fit_dp_snapshot` only completes on the certified 77-dist `dev+lint+vault`
Python 3.10.20 profile (`decoy_engine.quality.dp_provenance.
check_fit_environment`); every other installed profile -- including the
default `regression-gate` CI job -- fails closed with `dp_stack_uncertified`
before any private cell is read. Tests that call `fit_dp_snapshot` expecting a
successful fit must therefore skip off that gate rather than error.

This module WRAPS the real gate instead of re-reading the private
`_CERTIFIED_STACKS` manifest: the gate is the single source of truth for what
counts as certified, and a test-side reimplementation could silently drift
from it (e.g. admit a row the real gate has since retired). Any exception
during the check -- not only the expected `ProvenanceError` -- degrades to
"not certified" rather than propagating, so a collection-time surprise here
can never crash the whole test run; the worst case is an item that should run
being skipped instead, never a collection error.
"""

from __future__ import annotations

from decoy_engine.quality.dp_provenance import check_fit_environment


def is_certified_dp_env() -> bool:
    """True iff this process is running on the certified DP proof-stack.

    A thin wrapper around `check_fit_environment`, not a duplicate of its
    logic: it is the exact gate `fit_dp_snapshot` runs, so this predicate can
    never disagree with the real fit's own verdict."""
    try:
        check_fit_environment()
        return True
    except Exception:
        # ProvenanceError is the documented refusal; any other exception here
        # is unexpected but must still degrade to "skip", never crash
        # collection (a hook calling this runs for every test item).
        return False


def should_skip_dp(*, has_marker: bool, certified: bool) -> bool:
    """Pure decision the `dp_certified` collection hook applies per item.

    Factored out of `pytest_collection_modifyitems` (which cannot be
    exercised by a normal test -- it runs before any test body or fixture)
    so the actual skip DECISION has a deterministic, env-independent unit
    test (see tests/unit/quality/test_dp_cert_marking.py)."""
    return has_marker and not certified
