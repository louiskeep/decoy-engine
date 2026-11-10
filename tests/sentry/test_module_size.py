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

The ALLOWLIST records every module over GOAL (2026-11-10 revision: a dense entry
is a CEILING with headroom, not an exact count), split by value, not by a second
dict:

  - DENSE (GOAL < ceiling <= MAX): a reviewed dense-module exception. The recorded
    number is a ceiling: the file passes while loc <= ceiling. A fresh pin is
    ceiling_for(loc) = min(MAX, loc + MARGIN), so it starts with 20 lines of
    initial headroom, and the effective stale slack is bounded at 2 * MARGIN
    (40): a file more than 40 lines under its ceiling fails and must ratchet
    DOWN to ceiling_for(loc). Growth past the ceiling fails; the fix raises the
    ceiling by at most the lines the change adds (never above MAX) and lists the
    moved pin in the PR report. "A split would be artificial" is the named
    justification for such an entry: the module is one cohesive unit (a
    registry, a protocol catalogue, a single algorithm's helpers) and cutting it
    would add seams without removing any coupling. The census line states that
    reason, or the decomposition target that would replace it.
  - LEGACY (ceiling > MAX): pre-existing debt from before this policy. It stays an
    EXACT match and may ONLY shrink; its recorded number may never be raised. When
    it drops to <= MAX it becomes an ordinary dense entry. No file may CROSS MAX
    fresh -- decompose.

A non-allowlisted file that crosses GOAL adds an entry (<= MAX) in the same PR;
one that drops to <= GOAL deletes its entry.

Rise rule (review-enforced, not machine-enforced): a ceiling may only rise by the
PR's own LOC growth, `new_ceiling - old_ceiling <= max(0, new_loc - old_loc)`.
ceiling_for(new_loc) is for an initial conversion or a required shrink only. This
test reads one head and cannot see the previous ceiling without git history
machinery, which is not added here; the reviewer checks the census diff against
the file's own diff.

Guarantee boundary (what this test does and does not enforce): the ceiling check
enforces the rules against DRIFT -- an unrecorded growth past the ceiling, a
stale ceiling more than 2 * MARGIN above the file, an un-synced large shrink all
fail. It cannot by itself stop a DELIBERATE census edit in the same PR: someone
could add a fresh > MAX file to the allowlist, raise a legacy ceiling by growing
the file and bumping its number together, or lift a dense ceiling by more than
the change grew, and the test would pass. Each requires a visible census-line
change in the diff, so they are caught by review of that diff, not by this test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SRC = Path(__file__).parents[2] / "src" / "decoy_engine"
REPO = Path(__file__).parents[2]
# Tooling under scripts/ is held to the same limits: moving a module out of src/
# must not be a way to hide its size.
SCRIPTS = REPO / "scripts"

# Soft goal + hard ceiling (best-practices section 4.1; 2026-09-22 revision).
# A module <= GOAL needs no entry. Nothing new may cross MAX -- decompose instead.
GOAL = 600
MAX = 700
# Headroom a fresh dense pin starts with; stale slack may reach 2 * MARGIN.
MARGIN = 20


def ceiling_for(loc: int) -> int:
    """Fresh dense pin for a file at `loc`: 20 lines of headroom, never above MAX.
    For an initial conversion or a required shrink only; a growth bump follows the
    rise rule in the module docstring."""
    return min(MAX, loc + MARGIN)


# Census: every module over GOAL. An entry <= MAX is a DENSE ceiling (loc may sit
# up to 2 * MARGIN under it; growth past it needs a visible bump, never above MAX);
# an entry > MAX is pre-policy LEGACY debt (exact match, may only shrink, its number
# may never be raised).
# Each owes a decomposition target (tracked via ADR-0005 / the hardening plan).
# One-line rationale per entry; the per-file growth history lives in
# docs/decisions/module-size-census-history.md.
ALLOWLIST: dict[str, int] = {
    # LIB-L1a: the records package exports (DecodedRecord etc.) join the one public surface; move the
    # export blocks into a generated exports module when the package split lands.
    "src/decoy_engine/__init__.py": 605,
    # F06: one declared-casting route guard per route owner (the sentry in test_f06_declared_casting
    # inventories them) and the cast_run threaded through the sequential and out-of-core executors;
    # no casting logic of its own lives in these routers.
    "src/decoy_engine/execution/_pipeline_route_exec.py": 632,
    # sampler start/finish and evidence threading in _spawn_and_classify; split the spawn/classify half into _isolated_spawn.py.
    "src/decoy_engine/execution/_isolated_run.py": 672,
    # wiring of the fk-dtype hazard and D3a validator signals into the one routing call site, plus
    # the B4 source-type admission threading (_source_arrow_types + the source_types param); split
    # the probe-recovery resolver into its own module.
    "src/decoy_engine/execution/_pipeline_routing_signals.py": 633,
    # fan-in guard on the budget resolver; split the disk-preflight helpers into _disk_budget.py.
    "src/decoy_engine/execution/out_of_core/_budget.py": 629,
    # float FK key token pricing; split the disk-width helpers into _spill_widths.py.
    "src/decoy_engine/execution/out_of_core/_spill_estimate.py": 665,
    "src/decoy_engine/detection/detectors.py": 1046,
    "src/decoy_engine/generators/columns.py": 678,
    "src/decoy_engine/profiling/profiler.py": 660,
    # monolithic train_and_evaluate; decompose into split/fit/calibrate/evaluate modules.
    "src/decoy_engine/profiling/model_pack/trainer.py": 712,
    # compile-check ownership table; split into per-strategy check modules once the check set stabilises.
    "src/decoy_engine/plan/_checks.py": 712,
    # statistical-column check threading; same per-strategy-check decomposition target as _checks.py.
    "src/decoy_engine/plan/_compile.py": 700,
    # dispatch entry plus the table-keyed params, the job-seed hand-off and the eager string-pool
    # check for the position-keyed faker; dense exception, the pool check itself lives in _chunk_masking.
    # OBS-2: threads the decline records and branch-call snapshot onto each chunk's evidence.
    "src/decoy_engine/execution/native/_chunked_entry.py": 689,
    # OBS-2b: one structured decline record paired with each existing decision source; the record
    # types live in _route_decline_codes.py. Split the static admission pass out next.
    "src/decoy_engine/execution/native/_dispatch.py": 688,
    # fail-closed output projection and corpus pinning on the mask route; split the FK-resolution helpers.
    # cross-column V1 folds joint_mask key_by into the source-anchor snapshot build (a comment, no new logic).
    # MG-5 vault: the _fk_key_value compat wrapper moved to _fk_key_compat.py (pure move, zero behavior change).
    "src/decoy_engine/execution/_pandas_adapter.py": 700,
    # resident Arrow type resolution and the faker seed/node precondition pair (one predicate, two
    # entry points, so the chunked and sequential routes cannot drift); move the type resolution
    # into its own module with the next operator gate.
    "src/decoy_engine/execution/native/_requirements.py": 677,
    # SeqNativeRun: one bespoke per-strategy seam method per tenant around a shared admit/pin/apply
    # core; cohesive, the descriptors and decision types are already split out, and text_mask +
    # group_key keep their seam in sibling modules (_sequential_native_text_mask/_group_key).
    "src/decoy_engine/execution/_sequential_native.py": 700,
    # sequential FK route threading; split the per-table mask/quarantine loop.
    # cross-column V1 folds joint_mask key_by into the source-anchor build (a comment, no new logic).
    "src/decoy_engine/execution/_sequential.py": 680,
    # transactional publish hardening and row mask; split the publish cluster.
    "src/decoy_engine/quarantine.py": 658,
    # corpus schema checks and verify_corpus; split into _codeset_verify.py when next touched.
    "src/decoy_engine/transforms/_codeset_loader.py": 700,
    # one StreamFkJoiner lifecycle class (stage, orphan count, ordered and bounded-reorder joins, resolve); explain/optimizer helpers live in _stream_join_plan.py.
    "src/decoy_engine/execution/out_of_core/_stream_join.py": 665,
    # custom-provider registry snapshot for the shadow-parity gate; one cohesive module, no split identified.
    "src/decoy_engine/internal/faker_setup.py": 688,
    # FF1 span path and sub-floor policy (synthetic values split to _text_mask_synthetic.py).
    "src/decoy_engine/transforms/text_mask.py": 670,
    # scripts/: pre-existing one-off tooling, recorded when the sentry started scanning scripts/.
    # Legacy (> MAX) entries may only shrink; split each into sibling modules when next touched.
    "scripts/audit/probe.py": 775,
    "scripts/bench-generation-pool/bench_compare_gen.py": 908,
    "scripts/bench-unified-slice/bench_compare.py": 894,
    "scripts/build_golden_fixtures.py": 626,
    "scripts/faker-determinism/check_determinism.py": 621,
    "scripts/fk_memory_probe.py": 1056,
    "scripts/gen_proof_manifest.py": 1230,
    "scripts/native-baseline/bench_c1_oracle.py": 759,
    "scripts/tb3_cgroup_validation.py": 623,
    "scripts/tq_mutate.py": 822,
}


def _budgeted_files() -> list[Path]:
    return sorted(SRC.rglob("*.py")) + sorted(SCRIPTS.rglob("*.py"))


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
    if ceiling > MAX:
        # LEGACY: exact match, shrink-only.
        if loc == ceiling:
            return None
        if loc < ceiling:
            return (
                f"{rel}: shrank to {loc} LOC (census records {ceiling}). Ratchet the "
                f"census down to {loc} in this PR (or delete the entry if <= {GOAL})."
            )
        return (
            f"{rel}: legacy over-max module grew to {loc} LOC (census {ceiling}). "
            f"A module over {MAX} may only shrink -- decompose, never raise a "
            f"legacy ceiling."
        )
    # DENSE: the recorded value is a ceiling with bounded stale slack.
    if loc > MAX:
        return (
            f"{rel}: grew to {loc} LOC, over the {MAX}-LOC hard max. Decompose to "
            f"<= {MAX}; a dense entry's ceiling may not cross {MAX}."
        )
    if loc > ceiling:
        return (
            f"{rel}: grew to {loc} LOC (ceiling {ceiling}). Allowed (<= {MAX}); raise "
            f"the ceiling by at most the lines this change added (never above {MAX}) "
            f"and list the moved pin in the report."
        )
    if ceiling - loc > 2 * MARGIN:
        return (
            f"{rel}: {loc} LOC is {ceiling - loc} under its ceiling {ceiling} (more "
            f"than {2 * MARGIN}). Ratchet the census down to {ceiling_for(loc)} in "
            f"this PR (or delete the entry if <= {GOAL})."
        )
    return None


@pytest.mark.parametrize(
    "py_file",
    _budgeted_files(),
    ids=lambda p: str(p.relative_to(REPO)),
)
def test_module_within_size_budget(py_file: Path) -> None:
    """A module <= GOAL needs no entry; a dense module sits at or under its ceiling
    with bounded slack (growth needs a visible bump, never above MAX); legacy
    matches exactly and may only shrink; nothing may cross MAX fresh."""
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
        (660, 670, True),  # dense, under its ceiling: fine
        (671, 670, False),  # dense grew past its ceiling: must bump (visible)
        (700, 700, True),  # dense at the cap ceiling: fine
        (701, 700, False),  # dense past the cap: decompose
        (630, 670, True),  # slack exactly 2 * MARGIN: fine
        (620, 661, False),  # slack 41: ratchet down
        (690, 710, False),  # recorded above MAX is legacy: exact match only
        (660, 660, True),  # dense, synced: fine
        (683, 660, False),  # dense grew within band: must bump the census (visible)
        (700, 660, False),  # dense grew to max: must bump (still <= MAX)
        (701, 660, False),  # dense grew past max: decompose, ceiling can't cross MAX
        (619, 660, False),  # dense shrank past the slack bound: ratchet down (regrow guard)
        (809, 809, True),  # legacy, synced: fine
        (815, 809, False),  # legacy grew: may only shrink, never raise
        (800, 809, False),  # legacy shrank: must ratchet down
    ],
)
def test_size_verdict_branches(loc: int, ceiling: int | None, passes: bool) -> None:
    assert (_size_verdict("x/y.py", loc, ceiling) is None) is passes


def test_ratchet_down_message_names_the_fresh_pin() -> None:
    reason = _size_verdict("x/y.py", 620, 661)
    assert reason is not None and f"down to {ceiling_for(620)}" in reason


def test_ceiling_for() -> None:
    assert ceiling_for(600) == 620
    assert ceiling_for(690) == 700
    assert ceiling_for(700) == 700


def test_caps_do_not_rise() -> None:
    assert MAX <= 700
    assert GOAL <= 600
    assert MARGIN <= 20


def test_allowlist_paths_exist() -> None:
    """A stale allowlist path (renamed/deleted file) would silently exempt nothing."""
    for rel in ALLOWLIST:
        assert (REPO / rel).exists(), f"ALLOWLIST lists a nonexistent file: {rel}"


def test_allowlist_entries_are_still_over_goal() -> None:
    """Keep the census honest: an entry that dropped to <= GOAL should be deleted,
    not left to silently permit regrowth up to its old ceiling. (The slack bound
    in test_module_within_size_budget already forces a large shrink to ratchet down.)
    """
    for rel, ceiling in ALLOWLIST.items():
        loc = _loc(REPO / rel)
        assert loc > GOAL, (
            f"{rel} is now {loc} LOC (<= {GOAL}). It no longer needs an entry. "
            f"Delete it so the {GOAL}-LOC goal applies normally. "
            f"(Recorded ceiling was {ceiling}.)"
        )


def test_sentry_scans_scripts_dir() -> None:
    """scripts/ is inside the budgeted set (a file moved out of src/ stays measured)."""
    assert SCRIPTS / "mem_telemetry.py" in _budgeted_files()


def test_sentry_catches_a_planted_violation(tmp_path: Path) -> None:
    """Meta-test: prove the measurement AND the verdict trip on an over-max file."""
    big = tmp_path / "huge.py"
    big.write_text("\n".join(f"x{i} = {i}" for i in range(MAX + 50)) + "\n")
    loc = _loc(big)
    assert loc > MAX
    assert _size_verdict("planted.py", loc, None) is not None
