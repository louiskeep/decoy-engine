"""Phase F F0, deliverable 3.2 (part 1): the supported/deferred coverage matrix is complete and honest.

The matrix is derived from the dispatch, so these tests tie it back to the code: every generator
kind the config accepts must appear in a row, every SUPPORTED row must point at a real fixture
whose entry point actually exercises that family, and every DEFERRED row must say why.
"""

from __future__ import annotations

import inspect
from typing import get_args

from decoy_engine.config._tables import GenerateColumnConfig
from decoy_engine.generation import synthesize
from tests.parity import _f0_fixtures as fx
from tests.parity import _f0_matrix as mx

# Families whose fixture is not a plain generate_tables call, and the entry point they must carry.
_ENTRY_BY_FIXTURE = {
    "mixed_fk_generated_parent": fx.ENTRY_RUN_PIPELINE,
    "composite_person_deterministic": fx.ENTRY_COMPOSITE,
    "composite_person_seeded_pool": fx.ENTRY_COMPOSITE,
    "composite_name_email_deterministic": fx.ENTRY_COMPOSITE,
    "composite_name_email_seeded_pool": fx.ENTRY_COMPOSITE,
}


def _config_kinds() -> set[str]:
    return set(get_args(GenerateColumnConfig.model_fields["type"].annotation))


def test_every_dispatched_generator_kind_has_a_matrix_row() -> None:
    covered = {k for row in mx.MATRIX for k in row.kinds}
    assert _config_kinds() <= covered, f"unmapped kinds: {sorted(_config_kinds() - covered)}"


def test_dispatch_ladder_and_config_literal_agree() -> None:
    """The kinds `_generate_column` branches on are exactly the config's accepted kinds, so the
    matrix cannot be derived from a stale list."""
    src = inspect.getsource(synthesize._generate_column)
    for kind in _config_kinds():
        assert f'"{kind}"' in src, f"{kind} is accepted by config but not dispatched"
    assert "composite" not in src, "composites became dispatchable: add an end-to-end fixture"
    assert "composite" not in _config_kinds()


def test_supported_rows_point_at_real_fixtures() -> None:
    for row in mx.MATRIX:
        if row.status == mx.SUPPORTED:
            assert row.fixtures, row.family
            for fid in row.fixtures:
                assert fid in fx.FIXTURES_BY_ID, (row.family, fid)


def test_deferred_rows_carry_a_reason_and_no_fixture() -> None:
    deferred = [r for r in mx.MATRIX if r.status == mx.DEFERRED]
    assert deferred
    for row in deferred:
        assert not row.fixtures, f"{row.family}: a deferred row must not claim a fixture"
        assert len(row.note) > 40, f"{row.family}: deferral needs a stated reason"


def test_fixtures_name_the_entry_point_they_exercise() -> None:
    for fixture in fx.FIXTURES:
        assert fixture.entry == _ENTRY_BY_FIXTURE.get(fixture.fid, fx.ENTRY_GENERATE_TABLES), (
            fixture.fid
        )


def test_composites_are_never_claimed_end_to_end() -> None:
    for fixture in fx.FIXTURES:
        if fixture.family == "composite":
            assert fixture.entry == fx.ENTRY_COMPOSITE


def test_every_family_in_the_brief_is_classified() -> None:
    families = " | ".join(r.family for r in mx.MATRIX).lower()
    for needed in (
        "sequence",
        "categorical",
        "pooled-faker",
        "live-faker",
        "composite",
        "formula",
        "referenced-formula",
        "derived",
        "reference",
        "derived_aggregate",
        "conditional",
        "statistical",
        "grouped",
        "null",
        "masked-child",
    ):
        assert needed in families, needed
    # derived and reference are distinct standalone rows, not folded into formula.
    assert any(r.family == "derived" for r in mx.MATRIX)
    assert any(r.family == "reference" for r in mx.MATRIX)


def test_reuse_instructions_are_documented() -> None:
    assert "irregular" in mx.HOW_F1_REUSES.lower()
    assert "table_fingerprint" in mx.HOW_F1_REUSES
    assert "classify_columns" in mx.HOW_F3_ASSERTS_WITNESS
