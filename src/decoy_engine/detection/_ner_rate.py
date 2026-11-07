"""The NER cell sample, per-detector hit rate and rate bucket, shared by Pre-scan and Post-scan.

A column is scanned on its first `NER_SCAN_CELLS` non-null string cells. For each NER detector id
the rate is the share of scanned cells holding at least one span of that id; `NER_MIN_RATE` is to
be the rate at which a column counts as a hit. Spans are counted here and dropped: the helper returns
counts only, so no cell text reaches a log, a record or a report.

`NER_MIN_RATE` is to be calibrated, not borrowed. Rule (owner decision Q7, 2026-11-07), after the
Neyman-Pearson idea of fixing the false-positive bound first (Tong, Feng and Li 2018):

    NER_MIN_RATE = ceil_to_0.001(max(2 * highest observed false-hit rate,
                                     highest one-sided 95% Clopper-Pearson upper bound,
                                     0.01))

over known-negative columns, where a false-hit rate is the share of sampled cells with a hit of
that detector id. It acts on that cell share, never on spaCy's entity score, which is not a
calibrated probability (Presidio discussion #1997); AWS Glue's `ThresholdFraction` is the same
shape. A column is a negative only for the detector ids it does not contain (city and street
columns for `person_name` only). The corpus builder and the arithmetic live in
`tests/unit/detection_verification/test_ner_min_rate.py`, which recomputes the threshold and
asserts it equals the constant, so the constant cannot drift once set. Measured with en_core_web_sm 3.8.0:

    column                  detector     hits  cells    rate  upper_95
    redacted_notes          location        4   2000  0.0020    0.0046
    redacted_notes          person_name     0   2000  0.0000    0.0015
    fixture_mrn             location       86   2000  0.0430    0.0512
    fixture_mrn             person_name    93   2000  0.0465    0.0550
    fixture_health_plan_id  location      176   2000  0.0880    0.0991
    fixture_health_plan_id  person_name   162   2000  0.0810    0.0917
    fixture_iso_dates       location        0   2000  0.0000    0.0015
    fixture_iso_dates       person_name     0   2000  0.0000    0.0015
    faker_sentences         location        6   2000  0.0030    0.0059
    faker_sentences         person_name    22   2000  0.0110    0.0157
    faker_catch_phrases     location        0   2000  0.0000    0.0015
    faker_catch_phrases     person_name    20   2000  0.0100    0.0145
    faker_bs                location        7   2000  0.0035    0.0066
    faker_bs                person_name    44   2000  0.0220    0.0282
    company_names           location        1   2000  0.0005    0.0024
    company_names           person_name    36   2000  0.0180    0.0237
    product_names           location        2   2000  0.0010    0.0031
    product_names           person_name   557   2000  0.2785    0.2955
    city_names              person_name    76   2000  0.0380    0.0458  (negative for person_name only)
    street_addresses        person_name   305   2000  0.1525    0.1663  (negative for person_name only)

    2 x highest rate = 0.557; highest 95% bound = 0.2955; floor 0.01; rule output 0.557.

NER_MIN_RATE is deliberately not defined yet. The rule's output is above 0.5, which would make
the detector fire only on columns that are mostly names, so the value waits for an owner
decision (the calibration test is an xfail until then). Nothing in M1 reads it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from decoy_engine.detection.ner import DEFAULT_NER_MODEL, NER_ENTITY_MAP, iter_ner_spans_batch

NER_SCAN_CELLS = 2_000
# Bounds the span lists held at once; far below the pipe window in `ner.py`.
_WINDOW = 512


@dataclass(frozen=True)
class NerRate:
    """Cells holding at least one span of one detector id, out of the cells scanned."""

    detector_id: str
    hit_cells: int
    scanned_cells: int

    @property
    def rate(self) -> float:
        return self.hit_cells / self.scanned_cells if self.scanned_cells else 0.0

    @property
    def bucket(self) -> str:
        return rate_bucket(self.rate)


def sample_cells(values: Iterable[object], limit: int = NER_SCAN_CELLS) -> list[str]:
    """The first `limit` non-null, non-empty string cells, in order."""
    out: list[str] = []
    for value in values:
        if isinstance(value, str) and value:
            out.append(value)
            if len(out) >= limit:
                break
    return out


def ner_rates(values: Iterable[object], *, model: str = DEFAULT_NER_MODEL) -> dict[str, NerRate]:
    """Per-detector-id rates over the cell sample of `values`; every NER id is present."""
    cells = sample_cells(values)
    detector_ids = sorted(set(NER_ENTITY_MAP.values()))
    hits = dict.fromkeys(detector_ids, 0)
    for start in range(0, len(cells), _WINDOW):
        for spans in iter_ner_spans_batch(cells[start : start + _WINDOW], model=model):
            for detector_id in {span.detector_id for span in spans}:
                hits[detector_id] += 1
    return {i: NerRate(i, hits[i], len(cells)) for i in detector_ids}


def rate_bucket(rate: float) -> str:
    """`<10%`, `10-50%` or `>=50%`: the only form a rate takes in a record."""
    if rate < 0.10:
        return "<10%"
    return "10-50%" if rate < 0.50 else ">=50%"
