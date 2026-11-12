"""With the real model: free-text columns are flagged at `NER_MIN_RATE`, held-out negatives are not.

The cells come from the held-out sentence templates in `_ner_corpus.py`, which name companies,
products, cities and streets but no person. Person names come from a seeded Faker name pool and
are appended to a fixed share of cells. Seeds and ratios are fixed; the tests assert hit or no hit
and print the measured rates, so the margin over the threshold shows in the output.
"""

from __future__ import annotations

import random

from decoy_engine.detection import _ner_rate as nr
from tests._ner import needs_ner
from tests.unit.detection_verification import _ner_corpus as nc

SEED = 20261109
CELLS = 400


def _notes_column(every: int) -> list[str]:
    """CELLS no-name sentences; every `every`-th one also names a person."""
    from faker.providers.person.en_US import Provider as Person

    rng = random.Random(SEED)
    firsts, lasts = sorted(Person.first_names), sorted(Person.last_names)  # weighted dicts
    sentences = nc._prose(SEED, nc._HELD_OUT_COMPANY_PRODUCT_TEMPLATES, nc.person_pool(), CELLS)
    out = []
    for i, sentence in enumerate(sentences):
        if i % every == 0:
            name = f"{rng.choice(firsts)} {rng.choice(lasts)}"
            sentence = f"{sentence} Ask {name} to confirm."
        out.append(sentence)
    return out


@needs_ner
def test_a_notes_column_where_one_cell_in_four_names_a_person_is_a_hit() -> None:
    rate = nr.ner_rates(_notes_column(4))["person_name"]
    assert rate.scanned_cells == CELLS
    assert rate.hit, rate
    assert rate.rate >= nr.NER_MIN_RATE
    print("1-in-4 person_name rate", rate.rate, rate.hit_cells)


@needs_ner
def test_a_notes_column_where_one_cell_in_ten_names_a_person_is_visible_but_not_a_hit() -> None:
    rate = nr.ner_rates(_notes_column(10))["person_name"]
    assert rate.scanned_cells == CELLS
    assert 0 < rate.rate < nr.NER_MIN_RATE, rate
    assert not rate.hit
    print("1-in-10 person_name rate", rate.rate, rate.hit_cells)


@needs_ner
def test_no_held_out_column_is_a_hit_for_its_negative_ids() -> None:
    for column in nc.held_out_corpus():
        rates = nr.ner_rates(column.cells)
        for detector_id in column.negative_for:
            r = rates[detector_id]
            assert r.scanned_cells == nc.HELD_OUT_N, column.name
            assert not r.hit, (column.name, detector_id, r.rate)
            print(column.name, detector_id, r.hit_cells, r.scanned_cells, r.rate)
