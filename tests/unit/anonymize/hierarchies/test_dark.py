"""Dark: importing the engine does not load the package; catalog and public API unchanged."""

from __future__ import annotations

import hashlib
import subprocess
import sys

import decoy_engine
from decoy_engine.execution._strategies import strategy_catalog

# Captured from engine main b5b1ad16.
MAIN_CATALOG = (
    "bucket_perturb", "bucketize", "categorical", "code_set", "date_shift", "derived",
    "derived_aggregate", "faker", "formula", "fpe", "geo_generalize", "group_key",
    "grouped_series", "hash", "joint_mask", "nested", "passthrough", "redact", "shuffle",
    "text_mask", "text_redact", "tokenize", "top_code", "truncate", "windowed_date",
)  # fmt: skip
MAIN_ALL_COUNT = 187
MAIN_ALL_SHA256 = "8df39ef029dceb225204c545fc7a4fcebc6a9d726e2cd9e30241e0157f872065"


def test_engine_import_does_not_load_anonymize() -> None:
    code = "import sys, decoy_engine; print('decoy_engine.anonymize' in sys.modules)"
    result = subprocess.run(  # noqa: S603 -- args are test literals
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


def test_catalog_and_public_api_unchanged() -> None:
    assert tuple(row.name for row in strategy_catalog()) == MAIN_CATALOG
    names = sorted(decoy_engine.__all__)
    assert len(names) == MAIN_ALL_COUNT
    assert hashlib.sha256("\n".join(names).encode()).hexdigest() == MAIN_ALL_SHA256
