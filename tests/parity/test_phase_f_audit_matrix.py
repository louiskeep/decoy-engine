"""F-audit (A): the matrix covers every generator kind exactly once per probed case.

Pure data checks, no generation. The expectations in `_f_audit_matrix` come from the dispatch
predicates; the sweep (`test_phase_f_audit_no_legacy.py`) compares them with the real
`classify_plan` and the real run, so a row that drifts from the code fails there.
"""

from __future__ import annotations

from collections import Counter
from typing import get_args

import pytest

from decoy_engine.config._tables import GenerateColumnConfig
from decoy_engine.generation._admission import ADMISSION_MATRIX
from tests.parity import _f_audit_matrix as mx


def _literal_kinds() -> set[str]:
    return set(get_args(GenerateColumnConfig.model_fields["type"].annotation))


def test_the_literal_has_eleven_kinds_and_the_admission_table_matches() -> None:
    kinds = _literal_kinds()
    assert len(kinds) == 11
    assert kinds == set(ADMISSION_MATRIX)


def test_every_literal_kind_has_a_row_and_composite_is_counted_apart() -> None:
    covered = {r.probe.kind for r in mx.ROWS}
    assert covered == _literal_kinds()
    assert "composite" not in _literal_kinds()
    assert mx.NON_LITERAL_EXCLUSIONS == ("composite",)


def test_exactly_one_row_matches_every_probed_case() -> None:
    for probe in mx.probe_grid():
        hits = [r.row_id for r in mx.matching_rows(probe)]
        assert len(hits) == 1, (probe, hits)


def test_every_row_is_reachable_and_its_representative_matches_only_itself() -> None:
    reached = Counter(r.row_id for p in mx.probe_grid() for r in mx.matching_rows(p))
    assert set(reached) == {r.row_id for r in mx.ROWS}
    assert len({r.row_id for r in mx.ROWS}) == len(mx.ROWS)
    for r in mx.ROWS:
        assert [x.row_id for x in mx.matching_rows(r.probe)] == [r.row_id]


def test_fields_are_separate_and_the_compute_enum_is_closed() -> None:
    for r in mx.ROWS:
        assert r.compute in mx.COMPUTES, r.row_id
        assert r.route in {mx.CORE, mx.LEGACY}
        assert r.witness in {mx.VALUE_EQUALITY, mx.EXCLUDED}
        # compute names a backend only: it never repeats a route or a witness status
        assert r.compute not in {mx.CORE, mx.LEGACY, mx.VALUE_EQUALITY, mx.EXCLUDED}
        assert (r.witness == mx.EXCLUDED) == bool(r.witness_why)
    assert len(mx.COMPUTES) == 7


@pytest.mark.parametrize(
    ("row_id", "route", "compute"),
    [
        ("sequence", mx.CORE, mx.NO_SELECTION),
        ("categorical", mx.CORE, mx.PYTHON_RNG),
        ("reference-sequential", mx.CORE, mx.NO_SELECTION),
        ("reference-random", mx.CORE, mx.PYTHON_RNG),
        ("reference-weighted", mx.CORE, mx.PYTHON_RNG),
        ("reference-empty-parent", mx.CORE, mx.NO_SELECTION),
        ("faker-pooled", mx.CORE, mx.NUMPY),
        ("faker-below-threshold", mx.CORE, mx.LIVE_PROVIDER),
        ("faker-opt-out", mx.CORE, mx.LIVE_PROVIDER),
        ("faker-uncertified", mx.CORE, mx.LIVE_PROVIDER),
        ("faker-build-refused", mx.CORE, mx.LIVE_PROVIDER),
        ("faker-custom-provider", mx.LEGACY, mx.LIVE_PROVIDER),
        ("windowed-date", mx.CORE, mx.WINDOWED_SHIFT),
        ("formula-inline", mx.LEGACY, mx.FORMULA),
        ("formula-referenced", mx.LEGACY, mx.FORMULA),
        ("derived", mx.LEGACY, mx.NO_SELECTION),
        ("derived-aggregate", mx.LEGACY, mx.NO_SELECTION),
        ("statistical", mx.LEGACY, mx.LEGACY_SYNTHESIZE),
        ("grouped-series", mx.LEGACY, mx.LEGACY_SYNTHESIZE),
        ("group-key", mx.LEGACY, mx.LEGACY_SYNTHESIZE),
        ("mixed-plan-forced-legacy", mx.LEGACY, mx.PYTHON_RNG),
    ],
)
def test_route_and_compute_are_the_hand_stated_expectations(
    row_id: str, route: str, compute: str
) -> None:
    """Spelled out independently of the derivation, so a predicate edit is a visible diff."""
    got = mx.row(row_id)
    assert (got.route, got.compute) == (route, compute)


def test_materialization_follows_the_route() -> None:
    for r in mx.ROWS:
        want = (mx.RESIDENT, mx.STREAMED) if r.route == mx.CORE else (mx.RESIDENT,)
        assert r.materialization == want, r.row_id
