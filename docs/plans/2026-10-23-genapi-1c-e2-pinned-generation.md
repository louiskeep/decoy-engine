# GEN-API 1c-E2: expose the GenerationPlan from the pinned compile

Rules consulted: 00-universal, development-loop, feature-dev, testing

Status: plan rev2 (Opus, 2026-10-23). Folds the Codex plan-gate round-1 findings (2 HIGH + 2 MEDIUM + 1 LOW,
all acceptance-criteria hardening; the design approach was confirmed sound). Engine slice unblocking the
platform GEN-API 1c adapter. Follows the shipped 1c-E (`classify_window_from_generation_plan`) + A1
(`reference_date`) prereqs in 0.14.0.

## Problem

The platform 1c adapter (per Cam's architecture decision, rides the pinned subsystem for a reproducible
fingerprint) needs TWO artifacts from the SAME compile of a generation config:

- a `PinnedPlan` (from `compile_pinned_plan`) for the seed-independent fingerprint and `execute_pinned`, and
- a `GenerationPlan` for per-table window admission via the shipped
  `decoy_engine.generation.window.classify_window_from_generation_plan(generation_plan, table, ...)`.

`compile_pinned_plan` (generation/_pinned_compile.py:190-288) computes a `GenerationPlan` internally
(`gen = plan.generation`, from a single `compile_plan(config, _empty_profile(reference), version)`), builds
the `PinnedPlan` from it, and DISCARDS `gen`. No public entry returns both. The two existing window
classifiers accept a full `Plan` or a `GenerationPlan` - neither accepts a `PinnedPlan`. So today the platform
would have to compile twice and replicate the private `_empty_profile`/version/`snapshot_content_guard`
setup, risking admission and execution disagreeing. This slice removes that hazard at the source.

## Design (minimal, non-breaking)

Refactor the existing `compile_pinned_plan` body into a shared helper that performs the single compile and
returns BOTH artifacts, then expose a public entry for callers that need both:

- New frozen result `PinnedCompilation` (frozen dataclass; attrs `pinned: PinnedPlan`,
  `generation: GenerationPlan`).
- New public `compile_pinned_plan_with_generation(config, reference_date=None) -> PinnedCompilation`.
- `compile_pinned_plan(config, reference_date=None) -> PinnedPlan` becomes a thin wrapper:
  `return compile_pinned_plan_with_generation(config, reference_date).pinned`. Shared path => its signature,
  behavior, and every `PinnedModeError` are byte-for-byte unchanged.
- Export `PinnedCompilation` + `compile_pinned_plan_with_generation` from `decoy_engine.generation.pinned`
  `__all__`.

### Exact sequence to PRESERVE in the shared helper (do not reorder; Codex round-1 MEDIUM-4)

The current function's order is load-bearing and MUST be kept verbatim in the helper:
1. `refuse_live_tokens(config, "config")` (config token scan).
2. reference_date resolution: positional arg else `config["global_settings"]["reference_date"]`; None ->
   `PinnedModeError(pinned_reference_date_invalid)`; then `normalize_reference_date(reference_date)`.
3. `reject_unpinnable(config)` (initial rejection, pre-compile).
4. guarded compile: set `snapshot_content_guard` to `refuse_live_tokens(parsed, "snapshot")`, call the single
   `compile_plan(config, _empty_profile(reference), decoy_engine_version=str(decoy_engine.__version__))`
   inside `try`, reset the guard in `finally` (restored after BOTH success and failure).
5. `gen = plan.generation`; if None -> `PinnedModeError(pinned_not_generation_only)`.
6. `resolved = json.loads(gen.config_json)`; `reject_unpinnable(resolved)` (the POST-compile recheck on the
   resolved/default-filled config - explicitly preserved).
7. build the `PinnedStatistical`/`PinnedTable`/`PinnedColumn` projection (incl. the
   `pinned_statistical_digest_missing` raise) and the `PinnedPlan`.
8. return `PinnedCompilation(pinned=<the built PinnedPlan>, generation=gen)` - the SAME `gen` object from
   step 5, never a second compile.

No new compile, no duplication of the private setup, no change to `PinnedPlan`, `fingerprint`,
`execute_pinned`, or the window classifiers.

## Consistency guarantee (the crux)

The returned `generation` MUST be the identical in-memory `plan.generation` object that the returned `pinned`
was built from in the same call (`result.generation is <captured plan>.generation`), never a re-compile or a
copy. A window admitted from `generation` is then executed via `execute_pinned(pinned, seed)`; the two must
describe the same tables, ordered columns, row counts, dependencies, and statistical digests. The acceptance
tests PROVE this by object identity + a one-call spy + an actual `execute_pinned` comparison (not by trusting
a returned `WindowAdmission`).

## Bundle contract (Codex round-1 LOW-5)

`PinnedCompilation` is a frozen, in-process pair whose provenance guarantee ("both from one compile") applies
to the object a compiler call returns; it is not a serialization or wire type. Fingerprinting and
serialization stay on `.pinned` (`fingerprint(result.pinned)`, `result.pinned.to_dict()`). The bundle does
NOT imply: that `.generation` is seed-independent in the way the fingerprint is, nor that window-generated
bytes equal `execute_pinned` bytes. Exposing the already-public-as-classifier-input `GenerationPlan` here does
not promote every planner type to public.

## Acceptance tests (new tests/unit/generation/test_pinned_with_generation.py)

Fixtures: a multi-table generation config with (a) >=2 tables with UNEQUAL row counts, (b) at least one table
whose `generate_columns` are NOT in alphabetical order, (c) a cross-table `reference` dependency, (d) a
compile-time default-fill (a column whose resolved config gains a default), (e) a zero-row table, and - in a
dedicated fixture - a statistical column (snapshot_file) for the digest-substitution path.

1. ONE COMPILE + OBJECT IDENTITY (round-1 HIGH-1): monkeypatch/wrap the real `compile_plan` to record calls
   and capture the returned `plan`. Assert `compile_plan` is called EXACTLY ONCE, and
   `result.generation is captured_plan.generation`. This must FAIL if the impl compiles twice or returns a
   copied/substituted generation.
2. PINNED-vs-GENERATION PROJECTION AGREEMENT (round-1 HIGH-2): decode `result.generation.config_json` and
   assert it matches `result.pinned` on: table names (same set), per-table row counts, per-table ordered
   column names, each column's resolved config, dependencies, and - for the statistical fixture - that the
   pinned column's `snapshot_file` is the `sha256:<digest>` form while generation carries the corresponding
   snapshot digest (explicitly cover the path->digest substitution).
3. EXECUTION AGREEMENT (round-1 HIGH-2): call `execute_pinned(result.pinned, seed)` for a fixed seed and
   assert the output table names, per-table ordered column names, and row counts equal what `result.generation`
   admits; include the zero-row table.
4. ADMISSION VERDICTS (round-1 HIGH-2): for each table call
   `classify_window_from_generation_plan(result.generation, table, window_end=2**63)`; assert the expected
   Class A / Class B verdict and reason per fixture column, AND that a refusable column raises the coded
   admission error (not a bare TypeError).
5. WRAPPER DELEGATION + CHARACTERIZATION (round-1 MEDIUM-3): assert `compile_pinned_plan(config, ref)` equals
   `compile_pinned_plan_with_generation(config, ref).pinned` (delegation), AND pin the pinned output against
   PRE-REFACTOR characterization values under a FIXED `runtime_identity` (monkeypatched): a known
   `fingerprint(...)` and `.to_dict()` for the fixture. Exercise reference_date OMITTED (config fallback),
   POSITIONAL, and KEYWORD, explicit-arg-beats-config precedence, and seed independence (fingerprint equal
   across two seeds at execute time is already covered elsewhere - here assert the PinnedPlan is seed-free).
6. REFUSAL PARITY + SEQUENCE (round-1 MEDIUM-4): parametrize every `PinnedModeError` the current function
   raises - `pinned_reference_date_invalid` (missing AND naive/bad), `pinned_not_generation_only`,
   `pinned_live_token` at BOTH the config scan and the snapshot guard, unpinnable type, and
   `pinned_statistical_digest_missing` driven through a controlled `compile_plan` result (the existing
   malformed-plan execution test does not cover that branch). Assert exact (code, message) and that
   `compile_pinned_plan_with_generation` and `compile_pinned_plan` raise identically. Assert the
   `snapshot_content_guard` ContextVar is RESTORED to its prior value after BOTH a successful compile and a
   compile that raises (guard lifecycle).
7. EXPORTS: both new names import from `decoy_engine.generation.pinned`.
8. EXISTING-SUITE REGRESSION: run the existing `tests/unit/generation/test_pinned_generation.py` AND
   `test_generation_admission_bridge.py` unchanged (the latter already covers fallback/precedence).

## Non-goals / deferred

- The platform adapter itself (next slice, after the 0.14.1 release + pin bump).
- Statistical-artifact digest binding into the platform approval digest (P3): platform V1 refuses
  statistical-reference generation jobs; unchanged here.
- No change to the window classifiers, `execute_pinned`, or `PinnedPlan`. Successful pinned compilation does
  NOT imply Class A admission.

## Gate + release

Build fresh, TDD (red-first), STOP-not-weaken. Codex plan-gate rev2 (pinned subsystem is reproducibility-
critical) -> Sonnet build -> dennis -> Codex final -> merge engine main (PR squash). Then cut engine 0.14.1
(version stamp over the merged main tip) and the platform pin bump, which unblock the platform 1c adapter.
