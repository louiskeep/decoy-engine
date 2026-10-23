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

1. **Split the boundary.** Extract the generation-payload half of `_prepare` into
   `_prepare_generation(generation: GenerationPlan, caller: str) -> _PreparedPlan` that runs the
   `generation is None`/DP guards and the `config_json` / `statistical_specs` / `snapshots` decode.
   `_prepare(plan: Plan, caller)` keeps the `isinstance` guard + `validate_plan_when(plan)` and then
   delegates to `_prepare_generation(plan.generation, caller)`. This is a behavior-preserving
   refactor: `generate_tables(Plan)` produces the identical `_PreparedPlan`.

2. **Public pinned entry.** Add `generate_tables_from_generation_plan(generation_plan: GenerationPlan,
   derive_key=None, instance_default_locale=None, *, provider_snapshot=None, budget_bytes=None)`
   (name TBD-in-review; mirror the `generate_tables` signature minus the `Plan`). It type-guards
   `GenerationPlan`, calls `_prepare_generation`, and runs the identical generation core
   (`_streamed.run_generation` / the legacy whole-table path) that `generate_tables` uses. Provide a
   streamed sibling if `generate_tables_streamed` has the same shape.

3. **Risk A -- the `when` re-check.** `validate_plan_when(plan)` validates masking `when:` over
   `seed_envelope`, which the pinned entry has no analog for. Decide in PLAN review, after confirming
   whether generate columns can carry a `when:` that needs call-time validation:
   - **3a (preferred if generate `when` is validated at compile and embedded in `config_json`):** the
     pinned entry does NOT call `validate_plan_when`; it relies on the compile-time validation already
     baked into the pinned `config_json`, and (if a config-level when validator exists) re-runs THAT
     over `config_json`. Document that the pinned payload is trusted-as-compiled for `when`.
   - **3b (fallback):** if generate output genuinely depends on a `when:` that only the seed-envelope
     validator checks, synthesize the minimal structure needed and validate it, or require the caller
     to pass it. 3b only if 3a is shown unsafe.
   The acceptance tests below pin whichever is chosen; do not silently skip validation that affects
   output.

4. Mirror the deserialize-and-reverify shape of `_generation_plan_from_dict`
   (`plan/_serialize.py:381`) -- that function already rebuilds a `GenerationPlan` and re-verifies the
   DP receipt from pinned bytes without reopening files; the pinned entry is the same contract at the
   admission boundary.

## Acceptance tests (land before/with the implementation)

In `tests/unit/execution/test_pipeline_generation_contract.py` (the home of the Plan-only contract)
and `tests/unit/test_v2_generation.py`:

1. **Byte-identity:** compile a config to a `Plan`; call `generate_tables(plan)` and
   `generate_tables_from_generation_plan(plan.generation)`; assert the per-table Arrow output is
   byte-identical (same schema, same values, same row order).
2. **No reopen:** with the snapshot source files deleted/made unreadable after compile, the pinned
   entry still generates (proves it reads only pinned bytes). Mirror
   `test_compile_plan_reads_each_snapshot_path_once`.
3. **Type guard:** passing a raw dict or a full `Plan` to the pinned entry raises `TypeError` with a
   clear message; passing a bare `GenerationPlan` to `generate_tables` still raises (the Plan-only
   contract is preserved).
4. **DP receipt:** a `GenerationPlan` whose config declares DP but carries no `dp_verification`
   raises, same as the full-Plan path.
5. **`when` behavior (per the 3a/3b decision):** a generate config with a `when:` produces identical
   output through both entries; if 3a, add a test asserting the pinned entry does not require
   `seed_envelope`.
6. **Refactor safety:** existing `generate_tables` tests stay green unchanged (the `_prepare` split is
   behavior-preserving).

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

## Open question for PLAN review (Codex plan-gate)

The 3a-vs-3b `when` decision is the one real design fork. Confirm from the code whether a generate
column's `when:` is fully validated at compile time and embedded in `config_json` (then 3a is safe and
correct), or whether any generate output depends on a `when:` validated only by the seed-envelope path
(then 3b). Everything else is a mechanical, behavior-preserving extraction.
