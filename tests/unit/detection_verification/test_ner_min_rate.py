"""NER_MIN_RATE is the calibrated threshold, recomputed here from free-text known negatives.

Rule (owner decision Q7, 2026-11-07; Neyman-Pearson style false-positive control, Tong, Feng and
Li 2018; Clopper and Pearson 1934):

    threshold = max(2 * highest observed false-hit rate,
                    highest one-sided 95% Clopper-Pearson upper bound across the negative columns,
                    0.01)

A false-hit rate is the share of sampled cells holding at least one span of one detector id. The
rule works on that cell share, never on a spaCy score (spaCy scores are not calibrated). NER only
scores free-text columns (`is_free_text`, owner decision 2026-11-07), so the corpus holds
free-text negatives only. A column is a known negative only for the detector ids it does not
contain: `NER_ENTITY_MAP` sends GPE, LOC and FAC to `location`, so sentences naming cities and
streets are negatives for `person_name` only.

Rounding: the rule value is rounded UP to the next 0.001, so the constant never sits below the
rule. The test asserts `NER_MIN_RATE == ceil_to_thousandth(threshold)`, so the constant cannot
drift from the corpus or the model, and it is the evidence for the table in `_ner_rate.py`.

The corpus is built and pinned by digest in `_ner_corpus.py` / `test_ner_corpus_frozen.py`; it is
deterministic (seeded, no network). Set DECOY_NER_RATE_EVIDENCE=<path> to write the measured table
as JSON.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest

from decoy_engine.detection import _ner_rate as nr
from decoy_engine.detection.ner import NER_ENTITY_MAP
from tests._ner import needs_ner
from tests.unit.detection_verification import _ner_corpus as nc


def clopper_pearson_upper(hits: int, cells: int, confidence: float = 0.95) -> float:
    """One-sided upper bound of the hit rate: scipy's beta quantile, exact closed form at zero hits."""
    if hits >= cells:
        return 1.0
    try:
        from scipy.stats import beta
    except ImportError:
        if hits != 0:
            pytest.fail("scipy is needed for a non-zero Clopper-Pearson bound")
        return 1.0 - (1.0 - confidence) ** (1.0 / cells)
    return float(beta.ppf(confidence, hits + 1, cells - hits))


def ceil_to_thousandth(value: float) -> float:
    return math.ceil(round(value * 1000, 9)) / 1000


@needs_ner
def test_ner_min_rate_is_the_calibrated_threshold() -> None:
    table = []
    for column in nc.corpus():
        rates = nr.ner_rates(column.cells)
        assert set(rates) == set(NER_ENTITY_MAP.values()), column.name
        for detector_id in sorted(column.negative_for):
            r = rates[detector_id]
            assert r.scanned_cells == nc.N, (column.name, "was not scored as free text")
            table.append(
                {
                    "column": column.name,
                    "detector_id": detector_id,
                    "hits": r.hit_cells,
                    "cells": r.scanned_cells,
                    "rate": r.rate,
                    "upper_95": clopper_pearson_upper(r.hit_cells, r.scanned_cells),
                }
            )
    highest_rate = max(row["rate"] for row in table)
    highest_upper_95 = max(row["upper_95"] for row in table)
    threshold = max(2 * highest_rate, highest_upper_95, 0.01)
    expected = ceil_to_thousandth(threshold)
    evidence = {
        "rows": table,
        "2x_highest_rate": 2 * highest_rate,
        "highest_upper_95": highest_upper_95,
        "threshold": threshold,
        "NER_MIN_RATE": expected,
    }
    path = os.environ.get("DECOY_NER_RATE_EVIDENCE")
    if path:
        Path(path).write_text(json.dumps(evidence, indent=1))
    print(json.dumps(evidence, indent=1))
    assert expected <= 0.25, f"moved from the accepted 0.211: report it, do not tune\n{evidence}"
    assert expected == nr.NER_MIN_RATE
