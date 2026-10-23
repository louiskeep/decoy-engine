# GEN-API 1c-E: pinned-admission bridge (engine)

Status: plan

Rules consulted: 00-universal, development-loop, feature-dev, testing

## Why

GEN-API 1c (platform) needs to run a generation job from a plan that was **pinned at approval
time** and replayed later at call time, without recompiling or reopening the source config or its
statistical snapshots. Today the engine's generation entry accepts only a full compiled `Plan`
(`src/decoy_engine/generation/_plan_entry.py:65`, the `_prepare` boundary). The pinned payload the
platform holds is the `GenerationPlan` (`plan/_types.py:406`) that already lives inside a `Plan`:
`config_json` plus the pinned `PinnedSnapshot`s and decoded `PinnedStatisticalSpec`s. There is no
engine seam that admits a bare `GenerationPlan`.

This slice adds that seam. It is pure engineering (no owner/product decision): the data the
generation core consumes is already self-contained in the pinned payload, so the bridge is internal
with no file reopen. It is the one build-ready piece of GEN-API 1c and unblocks the platform
snapshot/admission adapter; the rest of 1c stays parked on the P3 decision (external statistical
files vs the YAML digest) and the platform P1 wiring.

## What `_prepare` actually reads (verified against the code)

`_prepare(plan, caller)` does four things:
1. `isinstance(plan, Plan)` guard (`_plan_entry.py:70-74`) -- the site that rejects a bare
   `GenerationPlan`.
2. `validate_plan_when(plan)` (`:79`) -- reads `plan.seed_envelope`, which a `GenerationPlan` does
   **not** carry. This is the one non-`generation` read (Risk A).
3. `plan.generation is None` guard + DP-receipt guard (`:80-99`).
4. Decode of `plan.generation.config_json`, `statistical_specs`, `snapshots` (`:86-122`).

Steps 3 and 4 read only `plan.generation.*`. So a `GenerationPlan` is fully self-describing for the
generation core; the only coupling to the wider `Plan` is step 2.

## Goal

A public engine entry that generates tables from an already-pinned `GenerationPlan` with byte-identical
output to calling `generate_tables` on the full `Plan` that embeds the same `GenerationPlan`, and with
no recompile and no snapshot-file reopen.

## Non-goals (hard boundaries)

- **No masking path.** The masking classifier (`classify_job` / `build_work_list`,
  `execution/_planner.py:174`) and resident/out-of-core admission genuinely require
  `seed_envelope`, `relationships`, `namespaces`, `ordering` -- none of which a `GenerationPlan`
  carries. A pinned `GenerationPlan` can never drive masking. This slice touches the generation
  admission gate ONLY and leaves `classify_job` untouched.
- No change to the compile path, the serialized Plan schema, or `generate_tables(Plan)` behavior.
- No new recompile or snapshot reopen; `source_path` stays diagnostic-only.

## Design

Factor `_prepare` so the pinned-payload decode (steps 3-4 above) is reachable from a
`GenerationPlan` alone, keeping the full-`Plan` entry a thin wrapper so its behavior is provably
unchanged.

1. **Split the boundary, preserving order.** Extract the generation-payload half of `_prepare` into
   `_prepare_generation(generation: GenerationPlan | None, caller: str) -> _PreparedPlan` (param typed
   `| None` because it owns the missing-payload guard) that runs the `generation is None` guard, the
   conditional DP-receipt guard, the `config_json` decode, and the `statistical_specs` / `snapshots`
   decode. `_prepare(plan: Plan, caller)` keeps, IN THIS ORDER: the `isinstance(plan, Plan)` guard →
   `validate_plan_when(plan)` → delegate to `_prepare_generation(plan.generation, caller)`. The
   preserved order is: type guard → seed validation → missing-payload guard → config decode → DP guard
   → artifact decode. This is a behavior-preserving refactor: `generate_tables(Plan)` yields the
   identical `_PreparedPlan` and raises the identical errors in the identical order.

2. **Public pinned entry.** Add `generate_tables_from_generation_plan(generation_plan: GenerationPlan,
   derive_key=None, instance_default_locale=None, *, provider_snapshot=None, budget_bytes=None)`
   (name TBD-in-review; mirror the `generate_tables` signature minus the `Plan`). It type-guards
   `GenerationPlan` (reject a raw dict or a full `Plan` with a clear `TypeError`), calls
   `_prepare_generation`, and runs the identical generation core (`_streamed.run_generation` / the
   legacy whole-table path) that `generate_tables` uses. Add the streamed sibling definitively
   (`generate_tables_streamed` has the required shape, `_plan_entry.py:193`): a
   `generate_tables_streamed_from_generation_plan(...)` covering `sink`, `batch_rows`, and the result
   mode. **Public surface:** export BOTH new entries from `generation.synthesize` (where consumers
   import `generate_tables`, per `tests/unit/execution/test_pipeline_generation_contract.py:23`);
   defining them in `_plan_entry.py` alone does not establish the supported import path. Keep the thin
   adapters in `_plan_entry.py` and reuse `run_generation` (its module docstring explains the size
   boundary, `_plan_entry.py:4`); no new module, framework, or `internal/` relocation.

3. **The `when` re-check (resolved by plan-gate: 3a).** `validate_plan_when(plan)`
   (`_when_parser.py:259`) walks ONLY `seed_envelope.per_table[].per_column[].when`
   (`_when_parser.py:230`), and `ColumnSeed.when` is conditional **masking** (`plan/_types.py:88`).
   There is no config-level `when` validator, and the seed predicate never reaches the generation core
   (only the decoded `config` and pinned artifacts do, `_plan_entry.py:180`). So `validate_plan_when`
   is masking-seed validation that is irrelevant to generation: the pinned entry does **not** call it,
   and the full-`Plan` wrapper keeps calling it (behavior unchanged for `generate_tables(Plan)`). Do
   NOT claim generate-column `when` is compile-validated or embedded in `config_json`; this plan
   asserts nothing about generate-column `when` semantics. The acceptance tests pin exactly this: the
   full-Plan path still runs seed validation; the pinned path is independent of the seed envelope.

4. **Admission contract: trusted object, not serialized-payload verification.** The shared helper does
   NOT re-verify: `_prepare` checks DP-receipt *presence* only, then trusts the supplied
   `statistical_specs` and decodes the embedded snapshots (`_plan_entry.py:89`). This is different from
   `_generation_plan_from_dict` (`plan/_serialize.py:381`), which verifies digests, requires snapshot
   coverage, recomputes DP verification, and rebuilds specs. Therefore the new entry's contract is:
   **it accepts an already-compiled or already-verified `GenerationPlan` object** (the output of
   `compile_plan`, or of a trusted `_generation_plan_from_dict` deserialize). A caller replaying a
   **serialized** pinned payload (e.g. platform 1c reading a stored plan) MUST run it through the
   existing deserialize-and-verify path FIRST, then pass the resulting trusted object here. The shared
   `_prepare_generation` helper preserves the existing presence-check-and-trust behavior exactly; this
   slice adds NO re-verification to it (doing so would change `generate_tables(Plan)` behavior). The
   plan and the platform 1c adapter must both state this caller responsibility.

## Acceptance tests (land before/with the implementation)

Placement: put the new pinned-admission tests in a focused new module
`tests/unit/generation/test_pinned_admission.py` (do NOT enlarge the 1,651-line
`tests/unit/test_v2_generation.py`). Keep the Plan-only pipeline-contract assertions in
`tests/unit/execution/test_pipeline_generation_contract.py`.

1. **Byte-identity (resident/legacy):** compile a config to a `Plan`; call `generate_tables(plan)` and
   `generate_tables_from_generation_plan(plan.generation)`; assert per-table Arrow output is
   byte-identical (schema, values, row order) with identical `derive_key`, `instance_default_locale`,
   and `provider_snapshot`. Cover BOTH a streamed-eligible plan and a legacy-whole-table plan (a plan
   with an ineligible column), since the core branches on that.
2. **Byte-identity (streamed):** same parity for the streamed sibling, covering `sink`, `batch_rows`,
   and the result mode, against `generate_tables_streamed(plan)`.
3. **No reopen:** delete/chmod-away the snapshot source files after compile; the pinned entry still
   generates (reads only pinned bytes). Mirror `test_compile_plan_reads_each_snapshot_path_once`.
4. **Type guard + contract preservation:** a raw dict or a full `Plan` passed to a pinned entry raises
   `TypeError` with a clear message; a bare `GenerationPlan` passed to `generate_tables` still raises
   (Plan-only contract intact).
5. **DP receipt:** a `GenerationPlan` whose config declares DP with statistical columns but carries no
   `dp_verification` raises, same as the full-Plan path; AND a config declaring DP with NO statistical
   columns succeeds (the guard at `_plan_entry.py:37` deliberately permits it).
6. **`budget_bytes`:** success under budget and refusal over budget, identical to the full-Plan path.
7. **Seed-envelope independence (the 3a invariant):** the pinned entry generates correctly for a plan
   whose output does not depend on masking seed `when`, WITHOUT consulting `seed_envelope`; and the
   full-Plan path still runs `validate_plan_when` (assert an invalid seed `when` still fails the
   full-Plan entry). Do not assume any generate-column `when` semantics.
8. **Error ordering + refactor safety:** the full-Plan path raises the SAME errors in the SAME order
   as before (type guard → seed validation → missing-payload → DP), and existing `generate_tables`
   tests stay green unchanged (the `_prepare` split is behavior-preserving).

## Risks

- **A (handled above):** `validate_plan_when` / `seed_envelope` coupling. Resolve in review via 3a/3b.
- **B:** scope creep into masking. Mitigated by the non-goal; `classify_job` is out of scope.
- **C:** a second generation entry point could drift from `generate_tables`. Mitigated by routing both
  through the shared `_prepare_generation` + the identical core, and by the byte-identity test.

## Rollout

Pre-GA, hard change (no compatibility shim). Single engine slice; no Plan schema bump (the
`GenerationPlan` type is unchanged). After merge: platform 1c consumes the new entry via its P1 bridge.
Promote the behavior into `docs/reference/` and note it in the generation API surface; update the
cross-repo roadmap row for GEN-API 1c.

## PLAN-gate resolution (Codex, 2026-10-23)

Codex plan-gate resolved the `when` fork and corrected two framings; this revision folds all findings
in. Resolved:
- **`when` = 3a:** `validate_plan_when` is masking-seed validation (`_when_parser.py:230,259`;
  `ColumnSeed.when` = masking, `_types.py:88`), there is no config-level `when` validator, and the seed
  predicate never reaches the generation core. The pinned entry does not call it; the full-Plan wrapper
  keeps it. The plan asserts nothing about generate-column `when` semantics (test 7).
- **Admission contract:** the new entry accepts an already-compiled/verified `GenerationPlan` (trusted
  object); serialized replay is deserialized-and-verified by the caller first. No re-verification is
  added to the shared helper (it keeps the presence-check-and-trust behavior). See Design step 4.
- Acceptance coverage broadened (streamed + legacy parity, `budget_bytes`, DP-without-statistical,
  provider/key/locale, error ordering) and the public surface fixed to `generation.synthesize` with a
  focused new test module.
Codex affirmed: the extraction is behavior-preserving if the order is kept, the no-reopen claim holds
for valid pinned payloads, the masking non-goal is correct, and no new module/framework/`internal/`
move is warranted. Build-ready after this revision.
