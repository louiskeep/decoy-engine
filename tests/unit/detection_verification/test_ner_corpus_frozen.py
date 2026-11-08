"""The NER calibration and held-out corpora are frozen: their cells are pinned by digest.

The digests were taken before the first measurement. A change to a generator, a template, a seed
or the Faker data moves a digest, and the threshold in `_ner_rate.py` would then describe a
different corpus. If a digest has to move, say why in the commit: a corpus bug found after a
measurement is the only acceptable reason. No spaCy is needed here.
"""

from __future__ import annotations

from typing import Any

import pytest

from decoy_engine.detection import _ner_rate as nr
from decoy_engine.profiling.eval import _corpus_values as cv
from tests.unit.detection_verification import _ner_corpus as nc

CALIBRATION_DIGEST = "6d85b234c48f55cfa2c7fbacc9ac75dca8a3ef71ce8b22f296c7ae101b5b93f0"
HELD_OUT_DIGEST = "58d9f2d48329f407e102a5856226d8c66e58115dcbf7c03d967b0c56a42f5f36"
CALIBRATION_COLUMNS = (
    "redacted_notes",
    "faker_sentences",
    "faker_catch_phrases",
    "faker_bs",
    "prose_company_product",
    "prose_cities",
    "prose_streets",
)
HELD_OUT_COLUMNS = ("held_out_faker_sentences", "held_out_company_product", "held_out_places")


def test_the_calibration_corpus_is_frozen() -> None:
    columns = nc.corpus()
    assert tuple(c.name for c in columns) == CALIBRATION_COLUMNS
    assert nc.digest(columns) == CALIBRATION_DIGEST


def test_the_held_out_corpus_is_frozen() -> None:
    columns = nc.held_out_corpus()
    assert tuple(c.name for c in columns) == HELD_OUT_COLUMNS
    assert nc.digest(columns) == HELD_OUT_DIGEST


def test_the_digest_moves_when_a_single_cell_changes() -> None:
    columns = nc.corpus()
    first = columns[0]
    edited = nc.Negative(first.name, first.negative_for, (first.cells[0] + " x", *first.cells[1:]))
    assert nc.digest([edited, *columns[1:]]) != nc.digest(columns)


def test_the_two_corpora_share_no_cell() -> None:
    calibration = {cell for c in nc.corpus() for cell in c.cells}
    held_out = {cell for c in nc.held_out_corpus() for cell in c.cells}
    assert not calibration & held_out


@pytest.mark.parametrize(
    ("columns", "size"),
    [(nc.corpus, nc.N), (nc.held_out_corpus, nc.HELD_OUT_N)],
)
def test_every_column_is_full_size_free_text_and_free_of_known_names(
    columns: Any, size: int
) -> None:
    for column in columns():
        assert len(column.cells) == size, column.name
        assert all(isinstance(c, str) and c for c in column.cells), column.name
        assert nr.is_free_text(list(column.cells)), column.name
        if column.name.startswith(("faker_", "prose_", "held_out_")):
            prose = column.name.startswith(("prose_", "held_out_company", "held_out_places"))
            pool = nc.person_pool() if prose else nc._name_pool()
            for cell in column.cells:
                tokens = {t.strip(".,;:!?()[]").lower() for t in cell.split()}
                assert not tokens & pool, (column.name, "holds a known name")


def test_short_value_columns_are_not_free_text_and_are_never_scored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a cell reached the NER pipeline")

    monkeypatch.setattr(nr, "iter_ner_spans_batch", boom)
    short_columns = {
        "fixture_mrn": cv._institutional_values_for_col(0, nc.N, cv._MRN_PREFIXES),
        "fixture_health_plan_id": cv._institutional_values_for_col(1, nc.N, cv._HP_PREFIXES),
        "fixture_iso_dates": cv._iso_date_values_from_base(cv._ISO_DATE_TRAIN_BASE, 0, nc.N),
        "bare_cities": [nc._CITIES[i % len(nc._CITIES)] for i in range(nc.N)],
    }
    for name, cells in short_columns.items():
        assert not nr.is_free_text(cells), name
        for rate in nr.ner_rates(cells).values():
            assert (rate.hit_cells, rate.scanned_cells) == (0, 0), name
