# GEN-API 1c-E2: expose the GenerationPlan from the pinned compile

Rules consulted: 00-universal, development-loop, feature-dev, testing

Status: plan (Opus, 2026-10-23). Engine slice unblocking the platform GEN-API 1c adapter. Follows the
shipped 1c-E (`classify_window_from_generation_plan`) + A1 (`reference_date`) prereqs in 0.14.0.

## Problem

The platform 1c adapter (per Cam's architecture decision, rides the pinned subsystem for a reproducible
fingerprint) needs TWO artifacts from the SAME compile of a generation config:

- a `PinnedPlan` (from `compile_pinned_plan`) for the seed-independent fingerprint and `execute_pinned`, and
- a `GenerationPlan` for per-table window admission via the shipped
  `decoy_engine.generation.window.classify_window_from_generation_plan(generation_plan, table, ...)`.

But `compile_pinned_plan` (generation/_pinned_compile.py:190-288) computes a `GenerationPlan` internally
(`gen = plan.generation`, from `compile_plan(config, _empty_profile(reference), version)`) and then
DISCARDS it, returning only the `PinnedPlan`. No public entry returns both. The two existing window
classifiers accept a full `Plan` (`classify_window`) or a `GenerationPlan` (`classify_window_from_
generation_plan`) - neither accepts a `PinnedPlan`. So today the platform would have to compile twice (and
replicate the private `_empty_profile`/version/`snapshot_content_guard` setup), risking admission and
execution disagreeing. This slice removes that hazard at the source.

## Design (minimal, non-breaking)

Refactor the body of `compile_pinned_plan` into a shared helper that performs the single compile and
returns BOTH artifacts, then expose a public entry for callers that need both:

- New frozen result `PinnedCompilation` (attrs: `pinned: PinnedPlan`, `generation: GenerationPlan`).
- New public `compile_pinned_plan_with_generation(config, reference_date=None) -> PinnedCompilation`: does
  exactly what `compile_pinned_plan` does today (same reference_date resolution, `refuse_live_tokens`,
  `reject_unpinnable`, `snapshot_content_guard`, the single `compile_plan(...)`), and returns the
  `PinnedPlan` it builds AND the `gen` (`plan.generation`) it built that PinnedPlan from.
- `compile_pinned_plan(config, reference_date=None) -> PinnedPlan` becomes a thin wrapper:
  `return compile_pinned_plan_with_generation(config, reference_date).pinned`. Its signature, behavior,
  and every `PinnedModeError` refusal are byte-for-byte unchanged (shared path), so all existing callers
  and tests are unaffected.
- Export `PinnedCompilation` + `compile_pinned_plan_with_generation` from `decoy_engine.generation.pinned`
  `__all__`.

No new compile, no duplication of the private setup, no change to `PinnedPlan`, `fingerprint`,
`execute_pinned`, or the window classifiers.

## The consistency guarantee (the crux - do not get this subtly wrong)

The returned `generation` MUST be the exact `plan.generation` object that the returned `pinned` was built
from in the SAME `compile_pinned_plan_with_generation` call. It must NOT be recomputed by a second
`compile_plan`. This is the whole point: a window admitted from `generation` is then executed via
`execute_pinned(pinned, seed)`, and the two must describe the same tables/columns/row-counts. The plan's
acceptance tests assert this equivalence explicitly.

## Acceptance tests (new tests/unit/generation/test_pinned_with_generation.py)

1. Equivalence: for a generation config, `compile_pinned_plan_with_generation(config, ref).pinned` has the
   same `fingerprint(...)` and `.to_dict()` as `compile_pinned_plan(config, ref)` (the wrapper did not
   change the pinned output).
2. Generation usable: the returned `.generation` is accepted by `classify_window_from_generation_plan(gen,
   table, window_end=2**63)` for each table, returning a `WindowAdmission` (no uncoded TypeError).
3. Same-compile consistency: the tables/columns/row_counts the `.generation` admits match the `.pinned`
   tables (same set of table names + row counts), so admission and `execute_pinned` agree. Prove it is one
   compile: e.g. a config with a compile-time default-fill shows the SAME resolved form in both artifacts.
4. Refusal parity: every `PinnedModeError` refusal `compile_pinned_plan` raises today
   (reference_date missing/invalid, not-generation-only, unpinnable token/type, statistical-digest-missing)
   is raised identically by `compile_pinned_plan_with_generation` (shared path) - parametrized.
5. Wrapper regression: existing `compile_pinned_plan` tests still pass unchanged (run the existing module).
6. Export: both new names import from `decoy_engine.generation.pinned`.

## Non-goals / deferred

- The platform adapter itself (next slice, after the 0.14.1 release + pin bump).
- Statistical-artifact digest binding into the platform approval digest (P3) - platform V1 refuses
  statistical-reference generation jobs; unchanged here.
- No change to the window classifiers or `execute_pinned`.

## Gate + release

Build fresh, TDD (red-first), STOP-not-weaken. Codex plan-gate (pinned subsystem is reproducibility-
critical) -> Sonnet build -> dennis -> Codex final -> merge engine main (PR squash). Then cut engine 0.14.1
(version stamp over the merged main tip) and the platform pin bump, which unblock the platform 1c adapter.
