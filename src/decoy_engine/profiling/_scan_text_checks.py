"""Value-free checks that scan runs on the raw text of a delimited file.

Two checks, both counts or booleans and never a cell value:

- the header guard asks whether a row declared as a header looks like data, so a headerless
  file read with `has_header: true` is flagged instead of silently losing its first record;
- the leading-zero count reads the sampled rows as text, because type inference turns `007`
  into the integer 7 and the zero is gone by the time anything downstream could notice.

Both read through the shared CSV reader (`read_csv_source` with the resolved dialect) and add
no pandas read of their own.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import pandas as pd

from decoy_engine.detection.detectors import run_all_detectors

# Detectors that recognise a value by its own shape. A name-hinted detector (person name,
# address, first name) needs a column label to fire, and the guard labels its cells c0..cn so a
# header such as `Name,City` can never trip a hint. Those detectors are absent on purpose.
GUARD_DETECTORS = frozenset({"email", "ssn", "us_phone", "pan", "iban", "ipv4"})
_DATE_DETECTORS = frozenset({"iso_date", "us_date", "eu_date"})


def _is_number(cell: str) -> bool:
    try:
        float(cell)
    except ValueError:
        return False
    return True


def header_looks_like_data(cells: Sequence[str]) -> bool:
    """True when the header holds a structured PII value, or every cell is a number or a date.

    A header of words (`first_name,email`, `Name,City,State,Zip`) is never flagged. A person
    name or an address in the first record is not detected: that is a stated limit.
    """
    if not cells:
        return False
    every_cell_is_data = True
    for position, cell in enumerate(cells):
        label = f"c{position}"
        found = {m.detector_id for m in run_all_detectors(pd.Series([cell], name=label), label)}
        if found & GUARD_DETECTORS:
            return True
        if not (_is_number(cell) or found & _DATE_DETECTORS):
            every_cell_is_data = False
    return every_cell_is_data


def count_leading_zeros(frames: Iterable[pd.DataFrame]) -> dict[str, int]:
    """Per column, how many non-null text values start with `0` followed by a digit.

    Each frame is consumed before the next arrives, so a chunked read of a large file holds
    one chunk at a time. `0.5` and a lone `0` do not count: no zero is lost there.
    """
    counts: dict[str, int] = {}
    for frame in frames:
        for column in frame.columns:
            hits = frame[column].astype("string").str.match(r"0\d", na=False)
            counts[str(column)] = counts.get(str(column), 0) + int(hits.sum())
    return counts
