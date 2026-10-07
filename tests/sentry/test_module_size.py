"""Sentry test: module size is ratcheted, with a soft goal and a hard ceiling.

engineering-best-practices section 4.1 treats ~600 LOC as the point where an
orchestration module should be decomposed (CLAUDE.md points at this census as
the enforced policy). LOC is a proxy for reviewability, not a truth: a
genuinely-dense 680-LOC module should not be force-split just to satisfy a round
number, but nothing should be allowed to grow without bound either. So this is a
two-line policy (2026-09-22 revision):

  - GOAL = 600. A module <= GOAL needs no entry and draws no attention.
  - MAX = 700. A hard ceiling for new work. No new module, and no growth of an
    existing dense module, may cross MAX. Over MAX means decompose.

The ALLOWLIST records every module over GOAL at its EXACT current LOC (a
ratchet), split by value, not by a second dict:

  - DENSE (GOAL < size <= MAX): a reviewed dense-module exception. It MAY grow,
    up to MAX, by bumping its recorded number in the same PR (the growth lands
    in the diff for the reviewer). It MUST ratchet DOWN when it shrinks.
    "A split would be artificial" is the named justification for such an entry:
    the module is one cohesive unit (a registry, a protocol catalogue, a single
    algorithm's helpers) and cutting it would add seams without removing any
    coupling. The census line states that reason, or the decomposition target
    that would replace it.
  - LEGACY (size > MAX): pre-existing debt from before this policy. It may ONLY
    shrink; its recorded number may never be raised. When it drops to <= MAX it
    becomes an ordinary dense entry. No file may CROSS MAX fresh -- decompose.

The recorded value must EQUAL the file's current LOC. That exact-match is what
closes the old hole where a file could shrink below its stale ceiling and then
silently regrow back up to it. A non-allowlisted file that crosses GOAL adds an
entry (<= MAX) in the same PR; one that drops to <= GOAL deletes its entry.

Guarantee boundary (what this test does and does not enforce): the exact-match
enforces the rules against DRIFT -- an accidental growth, a silent regrow, an
un-synced shrink all fail. It cannot by itself stop a DELIBERATE census edit in
the same PR: someone could add a fresh > MAX file to the allowlist, or raise a
legacy ceiling by growing the file and bumping its number together, and the test
would pass. Both now require an actual file edit plus a visible census-line
change in the diff, so they are caught by review of that diff, not by this test.
That is strictly better than the prior `loc <= ceiling` policy, which let a
ceiling be raised without even touching the file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SRC = Path(__file__).parents[2] / "src" / "decoy_engine"
REPO = Path(__file__).parents[2]

# Soft goal + hard ceiling (best-practices section 4.1; 2026-09-22 revision).
# A module <= GOAL needs no entry. Nothing new may cross MAX -- decompose instead.
GOAL = 600
MAX = 700

# Census: every module over GOAL, recorded at its EXACT current LOC (a ratchet).
# An entry <= MAX is a DENSE exception (may grow to MAX via a visible census bump,
# must ratchet down on shrink); an entry > MAX is pre-policy LEGACY debt (may only
# shrink, its number may never be raised). Recorded value must equal current LOC.
# Each owes a decomposition target (tracked via ADR-0005 / the hardening plan).
# One-line rationale per entry; the per-file growth history lives in
# docs/decisions/module-size-census-history.md.
ALLOWLIST: dict[str, int] = {
    # sampler start/finish and evidence threading in _spawn_and_classify; split the spawn/classify half into _isolated_spawn.py.
    "src/decoy_engine/execution/_isolated_run.py": 612,
    # fan-in guard on the budget resolver; split the disk-preflight helpers into _disk_budget.py.
    "src/decoy_engine/execution/out_of_core/_budget.py": 608,
    # float FK key token pricing; split the disk-width helpers into _spill_widths.py.
    "src/decoy_engine/execution/out_of_core/_spill_estimate.py": 623,
    # pinned-Plan generation threading through the per-generator dispatch; split the statistical/derived/formula branches.
    "src/decoy_engine/generation/synthesize.py": 632,
    "src/decoy_engine/storm/detectors.py": 1046,
    "src/decoy_engine/generators/columns.py": 660,
    "src/decoy_engine/storm/profiler.py": 635,
    "src/decoy_engine/quality/synth_report.py": 863,
    # expanded ML training corpus generators split out of fixtures.py; split value-generators from corpus builders next.
    "src/decoy_engine/storm/eval/corpus.py": 976,
    # monolithic train_and_evaluate; decompose into split/fit/calibrate/evaluate modules.
    "src/decoy_engine/storm/model_pack/trainer.py": 715,
    # compile-check ownership table; split into per-strategy check modules once the check set stabilises.
    "src/decoy_engine/plan/_checks.py": 783,
    # statistical-column check threading; same per-strategy-check decomposition target as _checks.py.
    "src/decoy_engine/plan/_compile.py": 703,
    # safety invariants documented in docstrings are part of the fix; extract the emission builders into _mem_telemetry_emit.py.
    "src/decoy_engine/execution/_mem_telemetry.py": 762,
    # chunk-parity invariant text and admission gate calls; dense exception.
    "src/decoy_engine/execution/_chunked.py": 626,
    # dispatch entry plus the table-keyed params, the job-seed hand-off and the eager string-pool
    # check for the position-keyed faker; dense exception, the pool check itself lives in _chunk_masking.
    "src/decoy_engine/execution/native/_chunked_entry.py": 615,
    # scale-aware chunked-FK dtype family and rootness check; split the FK helpers.
    "src/decoy_engine/execution/_chunked_fk.py": 970,
    # fail-closed output projection and corpus pinning on the mask route; split the FK-resolution helpers.
    "src/decoy_engine/execution/_pandas_adapter.py": 681,
    # routing dispatch; logic already lives in the _transforms_* siblings, dense reviewed exception.
    "src/decoy_engine/execution/_pipeline.py": 684,
    # resident Arrow type resolution; move it into its own module with the next operator gate.
    "src/decoy_engine/execution/native/_requirements.py": 644,
    # sequential FK route threading; split the per-table mask/quarantine loop.
    "src/decoy_engine/execution/_sequential.py": 652,
    # transactional publish hardening and row mask; split the publish cluster.
    "src/decoy_engine/quarantine.py": 645,
    # code_set provenance and FF1 notice plumbing through the streaming runner.
    "src/decoy_engine/execution/out_of_core/_runner.py": 707,
    # corpus schema checks and verify_corpus; split into _codeset_verify.py when next touched.
    "src/decoy_engine/transforms/_codeset_loader.py": 606,
    # DRAW_SITES catalogue; split into _draw_sites_mask.py / _draw_sites_gen.py if it grows.
    "src/decoy_engine/execution/native/_determinism_protocol.py": 928,
    # one provider class per draw mechanism across 30 sites; the size is fan-out, not tangle.
    "src/decoy_engine/execution/native/_draw_site_providers.py": 985,
    # load-time self-test and native_threads plumbing; no cleaner class split identified.
    "src/decoy_engine/execution/out_of_core/_stream_join.py": 720,
    # custom-provider registry snapshot for the shadow-parity gate; one cohesive module, no split identified.
    "src/decoy_engine/internal/faker_setup.py": 622,
    # FF1 span path and sub-floor policy; split into _text_mask_sub_floor.py when next touched.
    "src/decoy_engine/transforms/text_mask.py": 722,
}


def _loc(py_file: Path) -> int:
    """Newline count, matching the `wc -l` convention the cap is stated in
    (a final line without a trailing newline is not counted, exactly as wc -l)."""
    return py_file.read_text(encoding="utf-8").count("\n")


def _size_verdict(rel: str, loc: int, ceiling: int | None) -> str | None:
    """Pure policy decision: a failure message, or None to pass. `ceiling` is the
    module's ALLOWLIST value (None if unrecorded). Kept side-effect-free so every
    branch is unit-testable without planting real files."""
    if ceiling is None:
        if loc > MAX:
            return (
                f"{rel}: {loc} LOC, over the {MAX}-LOC hard max. Decompose to "
                f"<= {MAX}; a new module cannot be grandfathered above {MAX}."
            )
        if loc > GOAL:
            return (
                f"{rel}: {loc} LOC, over the {GOAL}-LOC goal. Decompose to <= {GOAL}, "
                f"or (if it is genuinely dense) add it to ALLOWLIST at {loc} in this "
                f"same PR for review -- allowed up to {MAX}."
            )
        return None
    if loc == ceiling:
        return None
    if loc < ceiling:
        return (
            f"{rel}: shrank to {loc} LOC (census records {ceiling}). Ratchet the "
            f"census down to {loc} in this PR (or delete the entry if <= {GOAL})."
        )
    # loc > ceiling: the module grew.
    if ceiling > MAX:
        return (
            f"{rel}: legacy over-max module grew to {loc} LOC (census {ceiling}). "
            f"A module over {MAX} may only shrink -- decompose, never raise a "
            f"legacy ceiling."
        )
    if loc > MAX:
        return (
            f"{rel}: grew to {loc} LOC, over the {MAX}-LOC hard max. Decompose to "
            f"<= {MAX}; a dense entry's ceiling may not cross {MAX}."
        )
    return (
        f"{rel}: grew to {loc} LOC (census {ceiling}). Allowed (<= {MAX}); bump "
        f"the census to {loc} in this same PR so the growth is visible in review."
    )


@pytest.mark.parametrize(
    "py_file",
    sorted(SRC.rglob("*.py")),
    ids=lambda p: str(p.relative_to(REPO)),
)
def test_module_within_size_budget(py_file: Path) -> None:
    """A module <= GOAL needs no entry; a recorded module must match its census
    exactly (dense may grow to MAX with a visible bump, legacy may only shrink);
    nothing may cross MAX fresh."""
    rel = str(py_file.relative_to(REPO))
    reason = _size_verdict(rel, _loc(py_file), ALLOWLIST.get(rel))
    assert reason is None, reason


@pytest.mark.parametrize(
    "loc, ceiling, passes",
    [
        (GOAL, None, True),  # exactly the goal, unrecorded: fine
        (GOAL + 1, None, False),  # over goal, unrecorded: must add an entry
        (MAX, None, False),  # dense but unrecorded: must add an entry
        (MAX + 1, None, False),  # over max, unrecorded: hard fail
        (660, 660, True),  # dense, synced: fine
        (683, 660, False),  # dense grew within band: must bump the census (visible)
        (700, 660, False),  # dense grew to max: must bump (still <= MAX)
        (701, 660, False),  # dense grew past max: decompose, ceiling can't cross MAX
        (620, 660, False),  # dense shrank: must ratchet the census down (regrow guard)
        (809, 809, True),  # legacy, synced: fine
        (815, 809, False),  # legacy grew: may only shrink, never raise
        (800, 809, False),  # legacy shrank: must ratchet down
    ],
)
def test_size_verdict_branches(loc: int, ceiling: int | None, passes: bool) -> None:
    assert (_size_verdict("x/y.py", loc, ceiling) is None) is passes


def test_allowlist_paths_exist() -> None:
    """A stale allowlist path (renamed/deleted file) would silently exempt nothing."""
    for rel in ALLOWLIST:
        assert (REPO / rel).exists(), f"ALLOWLIST lists a nonexistent file: {rel}"


def test_allowlist_entries_are_still_over_goal() -> None:
    """Keep the census honest: an entry that dropped to <= GOAL should be deleted,
    not left to silently permit regrowth up to its old ceiling. (The exact-match
    in test_module_within_size_budget already forces the shrink-to-ratchet-down.)
    """
    for rel, ceiling in ALLOWLIST.items():
        loc = _loc(REPO / rel)
        assert loc > GOAL, (
            f"{rel} is now {loc} LOC (<= {GOAL}). It no longer needs an entry. "
            f"Delete it so the {GOAL}-LOC goal applies normally. "
            f"(Recorded ceiling was {ceiling}.)"
        )


def test_sentry_catches_a_planted_violation(tmp_path: Path) -> None:
    """Meta-test: prove the measurement AND the verdict trip on an over-max file."""
    big = tmp_path / "huge.py"
    big.write_text("\n".join(f"x{i} = {i}" for i in range(MAX + 50)) + "\n")
    loc = _loc(big)
    assert loc > MAX
    assert _size_verdict("planted.py", loc, None) is not None
