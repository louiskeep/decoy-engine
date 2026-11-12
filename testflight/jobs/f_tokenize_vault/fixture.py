"""Job F seeded fixture generator: a single table of opaque case references.

Planted shapes:
  - `case_ref`: "CASE-{n:04d}" strings repeating every REPEAT_EVERY rows, so the
    vault-reuse path (same plaintext -> same token) is exercised.
  - one null at NULL_IDX (tokenize preserves nulls).
  - row SENTINEL_IDX carries SENTINEL_REF, a value that must not survive in any output.
"""

from __future__ import annotations

import pandas as pd

from testflight._fixtures import verify_fingerprint

CASES_COUNT = 80
REPEAT_EVERY = 40
NULL_IDX = 7
SENTINEL_IDX = 3
SENTINEL_REF = "CASE-SENTINEL-TOKENIZE-0001"

_CASES_FINGERPRINT = "8631dcf7aeee364bdae8f70a18b0834453b56f5c470e524651d9616f4a0c99ac"


def build_cases(seed: int = 47) -> pd.DataFrame:
    """Build the cases source frame (deterministic; `seed` kept for the builder contract)."""
    refs: list[str | None] = []
    for i in range(CASES_COUNT):
        if i == NULL_IDX:
            refs.append(None)
        elif i == SENTINEL_IDX:
            refs.append(SENTINEL_REF)
        else:
            refs.append(f"CASE-{i % REPEAT_EVERY:04d}")
    df = pd.DataFrame({"case_ref": refs})
    assert len(df) == CASES_COUNT
    verify_fingerprint(df, _CASES_FINGERPRINT, label="cases")
    return df
