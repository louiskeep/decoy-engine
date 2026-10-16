"""Phase F F0, deliverable 3.1: the whole-table generate-parity oracle.

What this freezes. Each fixture in `_f0_fixtures.FIXTURES` runs its NAMED entry point
(`generate_tables`, `run_pipeline` for the generated-parent FK boundary, or the composite
`generate_bundle` samplers) and its canonical Arrow fingerprint is compared with the digests
checked in at `_f0_expected.EXPECTED`. Expectations were captured once from the pre-F1 code and are
never recomputed here, so a test run cannot launder a behavior change into a new baseline.

Equality is canonical Arrow equality (`_f0_canonical`): schema (names, types, nullability, field
and schema metadata), column and row order, values with null distinct from NaN, Python value types
tagged; equivalent chunk layouts compare equal. It is NOT a comparison of serialized bytes.

How F1 reuses this. F1 (bounded generation) produces each SUPPORTED fixture's tables through its
partitioned path, using IRREGULAR partitions, concatenates the batches (any chunk layout), and
asserts `fingerprints_diff(EXPECTED[fid][table], table_fingerprint(actual)) == []`. The
helper `_f0_canonical.assert_matches_oracle` is that comparison, written so F1 can call it with its own
tables. F0 does not run a partitioned generation (none exists yet) and slicing an already
generated table would prove nothing, so F0 supplies the oracle, the invariants
(`test_phase_f_f0_invariants.py`) and the witness (`test_phase_f_f0_witness.py`), not a partitioned
run.

These tests need no native companion and passed unchanged under the companion venv and the
no-companion CI-mirror venv (the captured digests are identical across both).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import faker
import pytest

from decoy_engine.generation import _faker_pool
from tests.parity import _f0_fixtures as fx
from tests.parity._f0_canonical import assert_matches_oracle, table_fingerprint
from tests.parity._f0_expected import EXPECTED


def test_dependency_pins() -> None:
    """Faker output is a function of the Faker data tables; name the cause of a mass failure."""
    assert faker.VERSION == fx.PINNED_FAKER_VERSION, (
        f"Faker {faker.VERSION} != pinned {fx.PINNED_FAKER_VERSION}: every faker fixture digest "
        "moves with it. Re-capture deliberately (see _f0_capture.py), never edit digests."
    )


def test_every_fixture_has_a_checked_in_expectation() -> None:
    assert set(EXPECTED) == {f.fid for f in fx.FIXTURES}
    for fid, tables in EXPECTED.items():
        for name, fp in tables.items():
            assert set(fp) == {"schema", "rows", "columns", "table"}, (fid, name)
            assert len(fp["table"]) == 64


@pytest.mark.parametrize("fixture", fx.FIXTURES, ids=lambda f: f.fid)
def test_oracle_fixture(fixture: fx.Fixture) -> None:
    assert_matches_oracle(fixture.fid, fixture.run())


def test_pooled_fixture_sits_on_the_real_admission_threshold() -> None:
    """The pooled fixture must take the REAL pooled path, not a monkeypatched threshold."""
    assert _faker_pool.N_THRESHOLD == 50_000
    pooled = EXPECTED["pooled_faker"]["pooled"]
    assert pooled["rows"] == _faker_pool.N_THRESHOLD


def test_oracle_is_reproducible_in_process() -> None:
    """Two runs in one process agree (guards hidden global state such as the thread-local Faker)."""
    first = fx.run_generate_tables(fx.cfg_live_faker())
    second = fx.run_generate_tables(fx.cfg_live_faker())
    assert {k: table_fingerprint(v) for k, v in first.items()} == {
        k: table_fingerprint(v) for k, v in second.items()
    }


def test_statistical_seed_does_not_depend_on_the_snapshot_path(tmp_path: Path) -> None:
    """The matrix claims statistical output is keyed on the snapshot's content digest, so the
    checkout location must not move the oracle."""
    elsewhere = tmp_path / "relocated" / "snapshot_copy.json"
    elsewhere.parent.mkdir()
    shutil.copy(fx.SNAPSHOT_PATH, elsewhere)
    cfg: dict[str, Any] = fx.cfg_statistical()
    for col in cfg["tables"][0]["generate_columns"]:
        col["snapshot_file"] = str(elsewhere)
    assert_matches_oracle("statistical", fx.run_generate_tables(cfg))


def test_oracle_has_teeth_against_a_changed_job_seed() -> None:
    """A different seed must fail the oracle for a seeded family (non-vacuity of the baseline)."""
    cfg = fx.cfg_categorical()
    cfg["global_settings"]["seed"] += 1
    with pytest.raises(AssertionError, match="values differ"):
        assert_matches_oracle("categorical", fx.run_generate_tables(cfg))
