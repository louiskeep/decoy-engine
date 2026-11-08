"""Byte-level characterization of the expanded ML corpus (H-1 Wave B E3).

Class 9 guard for the corpus split: a header pool, value generator or statement
order that changes while moving changes these digests. The LightGBM golden also
depends on this corpus but needs the ml extra and a retrain to notice.
"""

from __future__ import annotations

import hashlib
import json

from decoy_engine.profiling.eval.corpus import (
    build_cryptic_fixtures,
    build_extended_fixtures,
    build_ood_fixtures,
)

# Captured on engine main 313552c0, before the split: (fixture count, sha256).
EXPECTED = {
    "build_extended_fixtures": (
        2958,
        "1984c19fc01702fd89061823a37d12c5738bffc6d0cb366075926f9cb27fa09b",
    ),
    "build_ood_fixtures": (
        32,
        "3b35ebc51b934181696c51c50c0e11c64aad001f5fcb1b2b9acaf23d5469e701",
    ),
    "build_cryptic_fixtures": (
        210,
        "e9d5f64c75bb1aada3338c2555c3eeab3f805c18d199b3c31442f46cdc4b3b1f",
    ),
}


def corpus_digest(fixtures: list) -> str:
    rows = [
        {
            "name": f.name,
            "labels": f.labels,
            "columns": {str(c): f.df[c].tolist() for c in f.df.columns},
        }
        for f in fixtures
    ]
    blob = json.dumps(rows, ensure_ascii=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def test_corpus_digest_identical() -> None:
    got = {}
    for build in (build_extended_fixtures, build_ood_fixtures, build_cryptic_fixtures):
        fixtures = build()
        got[build.__name__] = (len(fixtures), corpus_digest(fixtures))
    assert got == EXPECTED
