"""The NER cell sample, per-detector hit rate and rate bucket, shared by Pre-scan and Post-scan.

A column is scanned on its first `NER_SCAN_CELLS` non-null string cells. NER only scores free-text
columns: the median `len(cell.split())` of the sample must reach `FREE_TEXT_MIN_TOKENS`
(`is_free_text`, owner decision 2026-11-07); any other column is not scored and has no hit. For each
NER detector id the rate is the share of scanned cells holding at least one span of that id. Spans
are counted here and dropped: the helper returns counts only, so no cell text reaches a log, a
record or a report.

`NER_MIN_RATE` is calibrated, not borrowed. Rule, after the Neyman-Pearson idea of fixing the
false-positive bound first (Tong, Feng and Li 2018):

    NER_MIN_RATE = ceil_to_0.001(max(2 * highest observed false-hit rate,
                                     highest one-sided 95% Clopper-Pearson upper bound,
                                     0.01))

over known-negative free-text columns, where a false-hit rate is the share of sampled cells with a
hit of that detector id. It acts on that cell share, never on spaCy's entity score, which is not a
calibrated probability (Presidio discussion #1997); AWS Glue's `ThresholdFraction` is the same
shape. A column is a negative only for the detector ids it does not contain. The corpus is in
`tests/unit/detection_verification/_ner_corpus.py`, pinned by digest, and the arithmetic in
`test_ner_min_rate.py`, which recomputes the threshold and asserts it equals the constant. Measured
with en_core_web_sm 3.8.0:

    column                 detector     hits  cells    rate  upper_95
    redacted_notes         location        4   2000  0.0020    0.0046
    redacted_notes         person_name     0   2000  0.0000    0.0015
    faker_sentences        location        6   2000  0.0030    0.0059
    faker_sentences        person_name    22   2000  0.0110    0.0157
    faker_catch_phrases    location        0   2000  0.0000    0.0015
    faker_catch_phrases    person_name    20   2000  0.0100    0.0145
    faker_bs               location        7   2000  0.0035    0.0066
    faker_bs               person_name    44   2000  0.0220    0.0282
    prose_company_product  location       14   2000  0.0070    0.0109
    prose_company_product  person_name   169   2000  0.0845    0.0954
    prose_cities           person_name   112   2000  0.0560    0.0652  (negative for person_name only)
    prose_streets          person_name   211   2000  0.1055    0.1175  (negative for person_name only)

    2 x 0.1055 = 0.211; highest 95% bound = 0.1175; floor 0.01; rule output 0.211.

The owner accepted 0.211 on 2026-11-07. A free-text column is flagged when about 1 cell in 5 or
more holds a span of that id; lower nonzero rates stay visible as rates.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from decoy_engine.detection.ner import DEFAULT_NER_MODEL, NER_ENTITY_MAP, iter_ner_spans_batch

NER_SCAN_CELLS = 2_000
# A column is free text when the median whitespace-token count of its sampled cells reaches this.
FREE_TEXT_MIN_TOKENS = 3
# Share of scanned cells at which a free-text column counts as a hit; measured, see above.
NER_MIN_RATE = 0.211
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
    def hit(self) -> bool:
        """The one place an NER hit is decided; a column that was not scored never hits."""
        return self.scanned_cells > 0 and self.rate >= NER_MIN_RATE

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


def is_free_text(cells: Sequence[str]) -> bool:
    """True when the median `len(cell.split())` over `cells` is at least `FREE_TEXT_MIN_TOKENS`.

    The one place this rule is computed. An empty sample is not free text, and an even sample's
    median is the mean of its two middle counts (2 and 3 give 2.5, which is not enough). It is not
    the free-text notion of `profiling/_distributions.py` (cardinality, for display) or
    `quality/_freetext_advisory.py` (length and distinctness, for the HC-7 advisory).
    """
    if not cells:
        return False
    return statistics.median(len(cell.split()) for cell in cells) >= FREE_TEXT_MIN_TOKENS


def ner_rates(values: Iterable[object], *, model: str = DEFAULT_NER_MODEL) -> dict[str, NerRate]:
    """Per-detector-id rates over the cell sample of `values`; every NER id is present.

    The only NER scoring entry. A column that is not free text is not scored: no cell reaches
    spaCy and every rate is over zero scanned cells.
    """
    cells = sample_cells(values)
    detector_ids = sorted(set(NER_ENTITY_MAP.values()))
    if not is_free_text(cells):
        return {i: NerRate(i, 0, 0) for i in detector_ids}
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
