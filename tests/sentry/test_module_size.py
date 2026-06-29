"""Sentry test: module size is ratcheted, not allowed to grow unbounded.

engineering-best-practices section 4.1 caps orchestration modules at ~600
LOC (CLAUDE.md names `graph/runner.py` as the reference threshold). A module
that crosses that line is a signal to decompose, not to keep appending.

This sentry enforces the cap as a *ratchet* rather than a hard wall, because
the codebase already has five modules over the line (see ALLOWLIST). A blunt
`fail > 600` would land red against legitimately-large existing modules and
block every merge until a large refactor finished. Instead:

  - Any module NOT in the allowlist must stay at or under LIMIT.
  - Any allowlisted module may not exceed its recorded ceiling, so the known
    large files can only shrink. New bloat anywhere is blocked, and the
    allowlist documents exactly which files owe a decomposition.

To add a module to the allowlist you must cross LIMIT and record the current
size in the same PR, which puts the growth in the diff and the reviewer's
attention (the allowlist-as-ratchet pattern, best-practices section 5.1).
When you decompose an allowlisted file below LIMIT, delete its entry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SRC = Path(__file__).parents[2] / "src" / "decoy_engine"
REPO = Path(__file__).parents[2]

# Orchestration cap (best-practices section 4.1). New modules may not exceed it.
LIMIT = 600

# Census 2026-06-14: modules already over LIMIT, with their current line count
# as the ceiling. These may only shrink; decompose and remove the entry.
# Each owes a decomposition target (tracked via ADR-0005 / the hardening plan).
ALLOWLIST: dict[str, int] = {
    "src/decoy_engine/storm/detectors.py": 1049,
    "src/decoy_engine/generators/columns.py": 666,
    "src/decoy_engine/storm/profiler.py": 639,
    "src/decoy_engine/quality/synth_report.py": 863,
    # Sprint B (ML2.x): extended fixture corpus; decomposition deferred to
    # ML3.x corpus expansion sprint when fixture types are fully settled.
    "src/decoy_engine/storm/eval/fixtures.py": 726,
    # Sprint B (ML2.x): monolithic train_and_evaluate; decompose into
    # separate split / fit / calibrate / evaluate modules in ML3.x.
    "src/decoy_engine/storm/model_pack/trainer.py": 716,
    # SP-10 (2026-06-28): check_derived_column_refs (row 16) added to the
    # compile-check ownership table. Decompose the growing _checks.py into
    # per-strategy check sub-modules in a follow-up sprint when the check set
    # stabilises (post-SP-15 or when the next strategy batch lands).
    "src/decoy_engine/plan/_checks.py": 695,
    # SP-10c (2026-06-29): three new generate-path helpers extracted to
    # generation/_grouped_windowed_generators.py, but synthesize.py grew by
    # 13 lines (dispatch routing + lazy imports for grouped_series, windowed_date,
    # group_key). The 20-strategy dispatch table resists further decomposition
    # without splitting _generate_column itself; defer to SP-15 or when the
    # strategy count stabilises.
    "src/decoy_engine/generation/synthesize.py": 613,
    # SP-46 (2026-06-29): fpe_join_group tweak mirror added 9 lines to the
    # V1 FPEStrategy.apply in transforms/fpe.py, pushing it over the cap.
    # The module already owns the entire Feistel cipher implementation and
    # the apply method; decompose into fpe_cipher.py + fpe_strategy.py when
    # additional checksum schemes or a second cipher mode lands.
    "src/decoy_engine/transforms/fpe.py": 609,
}


def _loc(py_file: Path) -> int:
    """Newline count, matching the `wc -l` convention the cap is stated in
    (a final line without a trailing newline is not counted, exactly as wc -l)."""
    return py_file.read_text(encoding="utf-8").count("\n")


@pytest.mark.parametrize(
    "py_file",
    sorted(SRC.rglob("*.py")),
    ids=lambda p: str(p.relative_to(REPO)),
)
def test_module_within_size_budget(py_file: Path) -> None:
    """Non-allowlisted modules stay <= LIMIT; allowlisted modules may not grow."""
    rel = str(py_file.relative_to(REPO))
    loc = _loc(py_file)
    ceiling = ALLOWLIST.get(rel)
    if ceiling is not None:
        assert loc <= ceiling, (
            f"{rel} grew to {loc} LOC, over its recorded ceiling of {ceiling}. "
            f"Allowlisted modules may only shrink. Decompose toward <= {LIMIT} "
            f"LOC; do not raise the ceiling."
        )
    else:
        assert loc <= LIMIT, (
            f"{rel} is {loc} LOC, over the {LIMIT}-LOC orchestration cap "
            f"(best-practices section 4.1). Decompose it, or, if it genuinely "
            f"cannot be split now, add it to ALLOWLIST with its current size in "
            f"this same PR for tech-lead review."
        )


def test_allowlist_paths_exist() -> None:
    """A stale allowlist path (renamed/deleted file) would silently exempt nothing."""
    for rel in ALLOWLIST:
        assert (REPO / rel).exists(), f"ALLOWLIST lists a nonexistent file: {rel}"


def test_allowlist_entries_are_still_oversized() -> None:
    """Keep the allowlist honest: an entry that has dropped to <= LIMIT should be
    removed, not left to silently permit future regrowth up to its old ceiling.
    """
    for rel, ceiling in ALLOWLIST.items():
        loc = _loc(REPO / rel)
        assert loc > LIMIT, (
            f"{rel} is now {loc} LOC (<= {LIMIT}). It no longer needs an "
            f"allowlist entry. Delete it so the {LIMIT}-LOC cap applies normally. "
            f"(Recorded ceiling was {ceiling}.)"
        )
        assert loc <= ceiling, (
            f"{rel} ({loc} LOC) already exceeds its recorded ceiling {ceiling}; "
            f"update the census only by shrinking, never by raising."
        )


def test_sentry_catches_a_planted_violation(tmp_path: Path) -> None:
    """Meta-test: prove the LOC check actually trips on an oversized file."""
    big = tmp_path / "huge.py"
    big.write_text("\n".join(f"x{i} = {i}" for i in range(LIMIT + 50)) + "\n")
    assert _loc(big) > LIMIT
