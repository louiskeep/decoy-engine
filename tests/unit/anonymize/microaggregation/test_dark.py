"""The package is reachable by import only; the engine's public surface is unchanged."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys

import decoy_engine

# Captured from engine main b5b1ad16.
MAIN_ALL_SHA256 = "2d89378637bbded8b13431d1e49af164664d4b781c48918c20d88435bde41a91"
MAIN_STRATEGIES = (
    "bucket_perturb", "bucketize", "categorical", "code_set", "date_shift", "derived",
    "derived_aggregate", "faker", "formula", "fpe", "geo_generalize", "group_key",
    "grouped_series", "hash", "joint_mask", "nested", "passthrough", "redact", "shuffle",
    "text_mask", "text_redact", "tokenize", "top_code", "truncate", "windowed_date",
)  # fmt: skip

_PROBE = """
import sys
import decoy_engine
assert "decoy_engine.anonymize" not in sys.modules, "root import pulls in anonymize"
import decoy_engine.anonymize.microaggregation
assert "anonymize" not in decoy_engine.__all__
assert "microaggregate" not in decoy_engine.__all__
print("ok")
"""


def test_root_import_does_not_load_the_package() -> None:
    out = subprocess.run(  # noqa: S603 -- args are test literals
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "ok"


def test_public_surface_unchanged() -> None:
    digest = hashlib.sha256(json.dumps(tuple(decoy_engine.__all__)).encode()).hexdigest()
    assert digest == MAIN_ALL_SHA256
    names = tuple(info.name for info in decoy_engine.strategy_catalog())
    assert names == MAIN_STRATEGIES
