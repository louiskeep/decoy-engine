"""C5: the faker pool determinism harness wired as a pytest gate
(plan_faker_determinism_harness_v2.md).

Two tiers:

1. A small, deterministic subset of `golden_digests.json` re-run through
   the real driver at the full K=8 -- fast (a handful of candidates, not
   the ~195-candidate matrix), so it stays in the default test loop as a
   cheap regression guard that the driver + committed golden still agree.
2. The exhaustive full-matrix sweep against the same golden, marked
   `determinism_harness` and excluded from the default loop (runtime: a
   genuine multi-minute run spawning thousands of subprocesses) -- see
   `pyproject.toml`'s marker description and this directory's sibling
   `scripts/faker-determinism/README.md`.

A third, always-fast check reads the committed `certified_pairs.json`
directly (no subprocess) and confirms VERIFY item 2: every positive-control
type is certified at every matrix locale where Faker actually has that
provider.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[2]
HARNESS_DIR = ENGINE_ROOT / "scripts" / "faker-determinism"
GOLDEN_PATH = HARNESS_DIR / "golden_digests.json"
CERTIFIED_PAIRS_PATH = HARNESS_DIR / "certified_pairs.json"

_spec = importlib.util.spec_from_file_location(
    "check_determinism", HARNESS_DIR / "check_determinism.py"
)
assert _spec is not None and _spec.loader is not None
check_determinism = importlib.util.module_from_spec(_spec)
sys.modules["check_determinism"] = check_determinism
_spec.loader.exec_module(check_determinism)


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


_GOLDEN = _load_json(GOLDEN_PATH) or {}
_CERTIFIED_PAIRS = _load_json(CERTIFIED_PAIRS_PATH) or []


def _fast_subset_from_golden(limit: int = 4) -> tuple[list[str], list[str]]:
    """A small, en_US-only sample of the committed golden -- one locale
    keeps the driver's types x locales cross product minimal (no wasted
    combinations that aren't in golden), and en_US is guaranteed populated
    since every positive-control type resolves there."""
    en_us_entries = [v for v in _GOLDEN.values() if v["locale"] == "en_US"]
    sample = en_us_entries[:limit]
    types = sorted({v["faker_type"] for v in sample})
    return types, ["en_US"]


@pytest.mark.skipif(not _GOLDEN, reason="golden_digests.json is empty or not yet generated")
def test_fast_subset_matches_committed_golden(tmp_path: Path) -> None:
    types, locales = _fast_subset_from_golden()
    assert types, "expected at least one en_US golden entry to sample from"

    exit_code = check_determinism.main(
        [
            "--types",
            ",".join(types),
            "--locales",
            ",".join(locales),
            "--k",
            "8",
            "--jobs",
            "2",
            "--certified-out",
            str(tmp_path / "certified.json"),
            "--report-out",
            str(tmp_path / "report.json"),
            "--golden-path",
            str(GOLDEN_PATH),
        ]
    )
    report = json.loads((tmp_path / "report.json").read_text())
    assert exit_code == 0, f"regressions: {report.get('regressions')}"
    assert report["run_ok"] is True
    assert not report["regressions"]


@pytest.mark.skipif(
    not _CERTIFIED_PAIRS, reason="certified_pairs.json is empty or not yet generated"
)
def test_positive_control_types_are_certified_where_available() -> None:
    """VERIFY item 2: every one of the 10 current pooled types must be
    certified at every matrix locale where Faker actually has that method.
    No subprocess: reads the committed certified_pairs.json and checks
    in-process which (type, locale) pairs genuinely exist, so this stays
    fast enough for the default loop."""
    from decoy_engine.generation import _faker_pool
    from decoy_engine.internal.faker_setup import make_faker, resolve_pool_provider

    certified_keys = {
        (row["faker_type"], row["locale"]) for row in _CERTIFIED_PAIRS if row["kwargs"] == {}
    }

    missing: list[tuple[str, str]] = []
    for faker_type in sorted(_faker_pool.POOL_ELIGIBLE_FAKER_TYPES):
        for locale in check_determinism.DEFAULT_LOCALES:
            faker_inst = make_faker(locale)
            _callable, exact_available, custom_override = resolve_pool_provider(
                faker_inst, faker_type
            )
            if not exact_available or custom_override:
                continue
            if (faker_type, locale) not in certified_keys:
                missing.append((faker_type, locale))

    assert not missing, (
        f"positive-control (type, locale) pairs available in this Faker install but not "
        f"certified in certified_pairs.json: {missing}"
    )


@pytest.mark.determinism_harness
@pytest.mark.skipif(not _GOLDEN, reason="golden_digests.json is empty or not yet generated")
def test_full_matrix_against_golden(tmp_path: Path) -> None:
    """The real cross-time regression gate: the full default candidate
    matrix, K=8, asserted against the committed golden. Deliberately slow
    (spawns thousands of subprocesses) -- excluded from the default loop,
    run explicitly via `pytest -m determinism_harness`."""
    exit_code = check_determinism.main(
        [
            "--certified-out",
            str(tmp_path / "certified.json"),
            "--report-out",
            str(tmp_path / "report.json"),
            "--golden-path",
            str(GOLDEN_PATH),
        ]
    )
    report = json.loads((tmp_path / "report.json").read_text())
    assert exit_code == 0, f"regressions: {report.get('regressions')}"
    assert report["run_ok"] is True
