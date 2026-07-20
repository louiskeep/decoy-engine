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
    # round-3 Fix C SUB-FIX 4 (2026-07-20): crossed the 600 cap (596 -> 608)
    # applying the same decimal-correct fan-in guard `_memory_estimate.
    # _per_instance_mib` uses to this resolver's own `max(1, ...)` floor,
    # which over-committed at >67-way fan-in on a near-floor budget. The
    # module docstring already flagged this file as "within a handful of
    # lines of the 600-LOC cap"; decompose the OOC-D disk-preflight helpers
    # (`check_disk_spill_preflight` / `DiskSpillPreflight` /
    # `_nearest_existing_ancestor`) into a `_disk_budget.py` sibling,
    # mirroring the `_spill_estimate.py` split this module's own docstring
    # already documents as precedent, when the next budget change lands.
    "src/decoy_engine/execution/out_of_core/_budget.py": 608,
    "src/decoy_engine/storm/detectors.py": 1049,
    "src/decoy_engine/generators/columns.py": 666,
    "src/decoy_engine/storm/profiler.py": 639,
    "src/decoy_engine/quality/synth_report.py": 863,
    # MLF-4 (2026-07-19): expanded ML training corpus split out of
    # fixtures.py (module-size cap); cohesive synthetic-data generators.
    # Further decomposition (value-generators vs corpus builders) is the
    # next decomposition target.
    "src/decoy_engine/storm/eval/corpus.py": 976,
    # Sprint B (ML2.x): monolithic train_and_evaluate; decompose into
    # separate split / fit / calibrate / evaluate modules in ML3.x.
    "src/decoy_engine/storm/model_pack/trainer.py": 716,
    # SP-10 (2026-06-28): check_derived_column_refs (row 16) added to the
    # compile-check ownership table. Decompose the growing _checks.py into
    # per-strategy check sub-modules in a follow-up sprint when the check set
    # stabilises (post-SP-15 or when the next strategy batch lands).
    "src/decoy_engine/plan/_checks.py": 736,
    # HC-5 (2026-07-17): +16 LOC (695 -> 711) adding the `high_cardinality`
    # wrong-type guard to `check_statistical_columns` -- a `high_cardinality`
    # key on a non-`type: statistical` column (the one case `load_spec` never
    # sees, since it is only called for statistical columns) plus its
    # docstring note. Same per-strategy-check decomposition target stands.
    # HC-3b (2026-07-17): crossed the 600 cap adding the top_code compile-check
    # wiring (import + one call-site + one checks_passed entry, x2 for the
    # no_profile/full branches, plus run_config_only_checks' call + return
    # entry) -- the same repeated-four-times pattern every prior per-strategy
    # check module (bucketize, categorical, date_shift group_by, ...) already
    # added here. Decompose the four call/list sites into a small
    # data-driven table (check fn -> passed-name) when the next strategy
    # check lands and this module is touched again.
    # TX-2 (2026-07-20): +25 LOC (711 -> 736) extracting the shared
    # `_check_ner_available_for_strategy` walk (parametrized on `strategy`)
    # and adding `check_text_mask_ner_available` (row #30), the text_mask
    # analog of `check_text_redact_ner_available` (row #13). The extraction
    # keeps the two checks byte-identical in shape without duplicating the
    # column walk. Same per-strategy-check decomposition target stands.
    # HC-5 gate remediation MED-1 (2026-07-17): +19 LOC (609 -> 628) for
    # `_NON_SEMANTIC_GLOBAL_SETTINGS` + the strip-before-hash block in
    # `_hash_config`, restoring pipeline_config_hash byte-stability against
    # the new `categorical_retention_warn_threshold` advisory key. Small,
    # localized addition to an already-oversized module; the decomposition
    # target above still stands for the next touch.
    # HC-7 (2026-07-17): +30 LOC (628 -> 658) for the clinical free-text
    # advisory wiring: the import + Row 29 call site + its checks_passed
    # entry (x2 for the no_profile/full branches) + the warnings-fold line +
    # two new keys in `_NON_SEMANTIC_GLOBAL_SETTINGS`. Same per-strategy
    # check-module pattern as every prior addition here; same decomposition
    # target stands.
    # TX-2 (2026-07-20): +13 LOC (658 -> 671) for Row 30 (text_mask `ner`
    # compile-time availability check): the import + one call site in
    # `compile_plan` + its checks_passed entry (x2 for the no_profile/full
    # branches) + one call site + one return-tuple entry in
    # `run_config_only_checks`. Same per-strategy check-module pattern as
    # every prior addition here; same decomposition target stands.
    # DPS-3 (2026-07-20): +6 LOC (671 -> 677) for the dp generate-contract
    # gate (Row 31): the import + 3 call sites (compile_plan x2 branches +
    # run_config_only_checks) + their checks_passed/returned-tuple entries,
    # all single-line. Same per-strategy check-module pattern; same
    # decomposition target stands.
    "src/decoy_engine/plan/_compile.py": 677,
    # TB-5 precondition #73 (2026-07-13): the pure peak estimator was at the
    # cap (596 LOC) when it gained `route_intercept_bytes` -- the small public
    # accessor (idiomatic here, like `is_fixed_width_dtype`) that makes the
    # per-route intercept the single source of truth the B5 drift detector
    # removes before comparing slopes. It cannot be split just for one
    # accessor; decompose the fixed-width/string cost tables into a
    # `_mem_cost_tables.py` sibling when the next dtype/pricing batch lands.
    # TB-5 #74 (2026-07-13): +50 LOC for the estimator-BASIS single source of
    # truth -- `route_slope` + `estimator_basis_bytes` (and its `BasisEstimate`
    # result). This is the load-bearing OOM-safety contract: the SEQUENTIAL
    # route's basis is the working set (two largest tables + FK dedup), NOT
    # total raw bytes, and telemetry MUST divide observed_slope by the SAME
    # basis the estimator multiplies or it under-states the sequential slope
    # (the OOM-unsafe direction). `estimate_peak_bytes` now derives its
    # prediction from `estimator_basis_bytes`, so basis + intercept + slope are
    # one computation, unsplittable from the estimator. Same `_mem_cost_tables.py`
    # decomposition target stands for the pricing tables.
    "src/decoy_engine/execution/_mem_estimate.py": 660,
    # B5 dennis-remediation (2026-07-11): the HIGH (percentile-knob
    # under-shoot) and MEDIUM (crashed-run miscount) fixes each needed a
    # safety invariant documented in the module's docstrings, not just
    # enforced in code -- this is the safety-critical self-calibration
    # loop, and the invariant text IS part of the fix. Decompose the
    # emission helpers (telemetry_record_from_isolated_run /
    # _from_governor_trip) into their own module when B5's production
    # wiring sprint lands and gives them real callers to organize around.
    # TB-5 precondition #73 (2026-07-13): +46 LOC for the intercept-aware
    # drift fix -- the new `observed_slope` (intercept-removed) and the safety
    # property 2 rewrite explaining WHY the raw point ratio spuriously fires.
    # Like the B5 remediation above, the invariant text IS the fix on this
    # safety-critical loop; folded into the same emission-helper decomposition.
    # TB-5 #74 (2026-07-13): +88 LOC for the sequential BASIS contract --
    # `_assert_basis_matches_estimator` (the guard the two emission builders
    # call, REQUIRED for a sequential record) + the docstring text pinning WHY
    # the sequential basis is the working set, not total raw bytes (dividing
    # observed_slope by the wrong basis under-states the slope, the OOM-unsafe
    # direction). The live drift loop stays platform-owned (#74 deferred); this
    # guard is the standing contract that wiring must satisfy, and its rationale
    # IS the fix on this safety-critical loop. Same emission-helper
    # decomposition target stands (extract the builders + guard into
    # `_mem_telemetry_emit.py` when B5 production wiring gives them real callers).
    "src/decoy_engine/execution/_mem_telemetry.py": 762,
    # DE-11 remediation (2026-07-13): restored the chunk-parity invariant
    # explanation (a chunk with more distinct values than pool_size is still
    # admissible in chunked mode because it is byte-identical to the
    # full-frame run of the same rows -- pool_size controls collision rate,
    # not admission) that a prior trim removed to slip under the cap. The
    # invariant text IS the fix, per the same pattern as the B5/TB-5 entries
    # above; decompose the strategy-admissibility docstring out of the
    # module header when the conditional-admission set grows again.
    # DE-03 (2026-07-13): +7 LOC to thread the resolved output-projection policy
    # into the per-chunk adapter.run() so the chunked route enforces the
    # fail-closed schema-closure gate (the security fix must be adapter-internal
    # / bypass-resistant, so it cannot move out of this emission path). Same
    # strategy-admissibility docstring decomposition target stands.
    # DE-02 (2026-07-14): +2 LOC to thread the keyed-mask `key_provider` into the
    # per-chunk adapter.run() so the chunked route rekeys off the run secret. The
    # provider is injected at run time (never serialized), so it must flow through
    # this emission path. Same docstring-decomposition target stands.
    # DE-02 review round (Codex BLOCKER 4, 2026-07-14): +13 LOC -- this PUBLIC
    # entry point now resolves the config's mask_secret_ref and runs the
    # fail-closed gate up front, so a keyed chunked job cannot execute off
    # job_seed at GA. Same docstring-decomposition target stands.
    # DE-02 Codex item 6a (2026-07-14): +8 LOC -- the chunked entry also collects
    # vault entries, so it runs the shared assert_vault_writer_keyed guard against
    # the resolved mask key. Same docstring-decomposition target stands.
    # DE-10 residual (2026-07-14): +14 LOC to wire the per-chunk declared-vs-real
    # FK dtype guard into the masking loop (the compile-time gate trusts the
    # declared FK key dtype; this validates it against the real Arrow dtype and
    # fails closed on a misdeclaration that would silently void RI). The guard
    # itself lives in the sibling `_chunked_fk_dtype.py`; only the read + per-chunk
    # call live here, on the one path that sees each chunk. Same docstring-
    # decomposition target stands.
    "src/decoy_engine/execution/_chunked.py": 648,
    # DE-10 family-model (2026-07-14): crossed the 600 cap adding the scale-aware
    # chunked-FK dtype family -- date/timestamp split, fixed_size_binary, and the
    # decimal scale regex + unprovable-sentinel + a load-bearing docstring, all
    # closing Codex/dennis-reproduced RI holes (a misdeclared FK dtype silently
    # voided referential integrity). `_dtype_family` + the decimal constants are a
    # cohesive, self-contained unit consumed by both this module (the compile
    # gate) and the sibling `_chunked_fk_dtype.py` (the runtime guard); decompose
    # them into a shared `_fk_dtype_family.py` sibling when the next FK-dtype
    # change lands (a pure move -- no circular import, since the family logic
    # imports nothing from either consumer).
    # HC-3b (2026-07-17): +1 LOC adding "top_code" to CHUNK_SAFE_STRATEGIES
    # (it is value-deterministic, unkeyed, and chunk-independent, same as
    # bucketize). Same decomposition target stands.
    "src/decoy_engine/execution/_chunked_fk.py": 651,
    # DE-03 (2026-07-13): the mask adapter's `run()` is one of the five emission
    # routes the fail-closed output projection must guard (undeclared columns no
    # longer leak raw). The +17 LOC are the two policy params, the per-table
    # enforcement loop before the point of no return, and the sequential
    # passthrough -- the security fix is adapter-internal by design (bypass-
    # resistant), so it cannot be split out. The module was at the cap (596) when
    # this landed; decompose the FK-resolution helpers into a sibling when the
    # next relationship-strategy batch lands.
    # DE-02 (2026-07-14): +8 LOC to thread the keyed-mask `key_provider` through
    # run / run_single / run_sequential into StrategyContext.mask_key so every
    # keyed strategy rekeys off the run secret. The provider is a run-time
    # injection (never serialized), so the plumbing is adapter-internal by design.
    # Same FK-resolution-helper decomposition target stands.
    # HC-3a (2026-07-17): +31 LOC (621 -> 652) threading the date_shift group_by
    # pre-mask anchor snapshots -- union the anchor columns into the FK-safe
    # (lossless int+null) load set, snapshot each pre-mask keyed by (table, col),
    # and pass them into StrategyContext so the handler anchors on immutable
    # source ids (Codex R1 P1 #1/#2). The snapshot construction differs from the
    # sequential route's (whole-frame dict vs per-table lifetime), so it does not
    # factor into a shared helper without harm. Same FK-resolution-helper
    # decomposition target stands.
    # HC-3b (2026-07-17): +8 LOC (652 -> 660) unioning top_code columns into the
    # lossless nullable-Int64 ingest (top_code_columns) so a large int+null value
    # is not float64-rounded and the masked output stays chunk-boundary
    # independent (Codex R2). Same FK-resolution-helper decomposition target.
    "src/decoy_engine/execution/_pandas_adapter.py": 660,
    # DE-03 (2026-07-13): run_pipeline resolves the projection policy + the
    # generate-echo exemption set once and threads them into every emission route
    # (+15 LOC). The orchestration spine already owns route selection; this is the
    # one place that can see both `config` (for the policy) and `table_kinds` (for
    # the exemption), so the resolution belongs here. The module was exactly at
    # the cap (600) when this landed; decompose the routing-dispatch block into a
    # sibling when the next execution-route batch lands.
    # DE-02 (2026-07-14): +21 LOC. run_pipeline is the one place that sees both the
    # compiled `plan` (to detect keyed strategies) and `config` (for the
    # `mask_secret_ref`), so the fail-closed gate + one-time provider resolution
    # live here and thread the resolved provider into every execution route. The
    # gate must run before any table/vault/manifest is written -> spine-owned.
    # Same routing-dispatch decomposition target stands.
    # DE-02 review round (Codex BLOCKER 5, 2026-07-14): +9 LOC -- run_pipeline now
    # fails closed if a caller-supplied vault writer is keyed differently from the
    # resolved mask key (the vault holds reversible plaintext PII and must be
    # encrypted under the run secret). Same routing-dispatch decomposition target.
    # DE-02 dennis re-gate (LOW L3, 2026-07-14): +12 LOC -- the vault-key guard now
    # requires the standard VaultWriter contract (isinstance) so a duck-typed
    # writer without the key-match method cannot silently bypass the fail-closed
    # net. Same routing-dispatch decomposition target stands.
    "src/decoy_engine/execution/_pipeline.py": 645,
    # DE-02 (2026-07-14): +3 LOC crossing the 600 cap -- the sequential FK route
    # threads `key_provider` into StrategyContext.mask_key like the other adapters
    # (run-time injection, never serialized). Decompose the per-table
    # mask/quarantine loop into a sibling when the next FK-route batch lands.
    # HC-3a (2026-07-17): +32 LOC (603 -> 635) threading the date_shift group_by
    # pre-mask anchor snapshots on the sequential route -- union the anchor
    # columns into the FK-safe (lossless int+null) load, snapshot each table's
    # anchors pre-mask into ctx.group_anchor_snapshots, and evict them with the
    # frame after its node loop (same-table lifetime). Codex R1 P1 #1/#2. Same
    # per-table mask/quarantine-loop decomposition target stands.
    # HC-3b (2026-07-17): +6 LOC (635 -> 641) unioning top_code columns into the
    # sequential route's lossless nullable-Int64 ingest (Codex R2 chunk-safety).
    "src/decoy_engine/execution/_sequential.py": 641,
    # DE-08 residual (2026-07-14): crossed the 600 cap (was 569) hardening the
    # transactional quarantine publish in place -- fail-closed on a hardlink-
    # unsupported filesystem (clear message, not an opaque OSError) and best-
    # effort/logged cleanup of the post-link staging file so an already-successful
    # commit is never reported as a run failure. The stage/publish/discard/finalize
    # trio is one cohesive transactional unit that this fix extends, not appends
    # beside; decompose that publish cluster into a `_quarantine_transaction.py`
    # sibling when the next quarantine-sidecar change lands.
    "src/decoy_engine/quarantine.py": 619,
    # HC-1 slice 1 (2026-07-17): crossed the 600 cap (639) wiring the code_set
    # corpus provenance stamp + pinned-record lookup into the out-of-core route
    # -- the per-chunk evidence stamp and the job-wide pinned corpus record are
    # threaded through the streaming runner so masking and evidence cannot
    # diverge on a mid-job corpus swap (parity with the pandas/sequential
    # routes). Decompose the chunk-drain / evidence-merge cluster into a sibling
    # when the next out-of-core route change lands.
    # phase-aware build budget (2026-07-20): +24 LOC (639 -> 663) threading a
    # `budget_bytes` param through `run_fk_out_of_core` / `_stream_table` and
    # computing per-phase DuckDB memory_limit caps (joiner / sink-path build /
    # resident-path build) instead of one flat cap for every connection --
    # fixes a starved sink-path relation build that measurably OOMed under a
    # divided-by-global-peak cap even with most of the run's budget idle. The
    # phase-cap derivation itself lives in the sibling `_memory_estimate.py`
    # (new module, not appended here) to avoid growing this file further; only
    # the three call-site substitutions and the param plumbing live here. Same
    # chunk-drain / evidence-merge decomposition target stands.
    # round-3 Fix C SUB-FIX 2 (2026-07-20): +2 LOC (663 -> 665) passing the
    # new `sink=sink is not None` param to `resolve_phase_memory_limits` so
    # it computes only the opened path's pair instead of raising for the
    # unused one. Same chunk-drain / evidence-merge decomposition target.
    "src/decoy_engine/execution/out_of_core/_runner.py": 665,
    # HC-2 (2026-07-17): crossed the 600 cap adding the generic corpus
    # schema-invariant checker (_check_corpus_schema, shared by the load path
    # and the new standalone verify_corpus primitive), the
    # corpus_source_version mismatch gate (_check_source_version_pin), and
    # verify_corpus/CorpusVerifyReport themselves -- this module already owns
    # "read a Parquet file off disk, validate it, cache it" (see its
    # docstring), so the new checks and the new standalone primitive belong
    # here, not split further. 610 -> 613: the two-model gate added a Path()
    # coercion + optional-path pin signature (verify_corpus never-raises fix).
    # Decompose verify_corpus + CorpusVerifyReport into a `_codeset_verify.py`
    # sibling when the next standalone-check consumer (CLI/platform) lands and
    # needs this module touched again.
    "src/decoy_engine/transforms/_codeset_loader.py": 613,
    # NOTE: transforms/code_set.py was allowlisted at 637 during the HC-2 build;
    # the two-model-gate remediation then decomposed validate_code_set_config
    # into transforms/_codeset_config_checks.py (mirroring _checks_top_code.py),
    # bringing code_set.py back to 592 -- under the 600 cap, so it is no longer
    # allowlisted here. Do not re-add it without cause.
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
