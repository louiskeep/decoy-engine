"""Production-boundary guard for the C0 `build_pool_values` seam extraction
(`docs/plans/plan_faker_determinism_harness_v2.md` C0).

`tests/fixtures/golden/faker_pool_seam_characterization.json` was captured by
running the PRE-EXTRACTION `build_and_sample` (the single-function
`make_faker` -> `resolve_pool_provider` -> `seed_instance` -> provider-loop ->
`PoolSampler.sample` implementation) over a representative (faker_type,
locale, kwargs) set spanning every locale in the harness matrix, a non-empty
`faker_kwargs` mapping (dropped by the underlying provider but must still
thread through without changing behavior -- see `build_and_sample`'s "explicit
seam arg" contract), and both `None` and per-column locale overrides.

This test freezes that capture as the pre-refactor baseline and replays it
against whatever `build_and_sample` implementation is currently checked in.
Per the snapshot-before-extraction rule (CLAUDE.md engineering-best-practices
section 4.1 / V2.0-A), this file must pass BEFORE the seam extraction lands
(proving the baseline is a real capture, not a tautology) and AFTER it
(proving the extraction changed no observable byte of output). A failure here
means the extraction shifted production output -- fix the extraction, never
this fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.generation import _faker_pool
from decoy_engine.generators.derivation import GenDeriveContext

FIXTURE_PATH = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "golden"
    / "faker_pool_seam_characterization.json"
)


def _load_cases() -> list[dict[str, Any]]:
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


_CASES = _load_cases()
assert len(_CASES) >= 8, "characterization fixture unexpectedly shrank"


@pytest.mark.parametrize(
    "case", _CASES, ids=lambda c: f"{c['faker_type']}@{c['locale']}#{c['n']}"
)
def test_build_and_sample_output_matches_pre_extraction_baseline(case: dict[str, Any]) -> None:
    col = {
        "name": "val",
        "type": "faker",
        "faker_type": case["faker_type"],
        "locale": case["locale"],
        "faker_kwargs": case["kwargs"],
    }
    gen_ctx = GenDeriveContext.for_column(
        derive_key=None, column_config=col, fallback_seed=case["seed"]
    )
    result = _faker_pool.build_and_sample(
        faker_type=case["faker_type"],
        faker_kwargs=case["kwargs"],
        n=case["n"],
        gen_ctx=gen_ctx,
        effective_locale=case["locale"],
    )
    assert result == case["expected_output"], (
        f"build_and_sample output for {case['faker_type']!r}@{case['locale']!r} diverged from "
        "the pre-extraction baseline -- the C0 seam extraction must be byte-identical"
    )
