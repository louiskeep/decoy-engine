---
Status: plan
---

# Unified-slice route activation (Phase 4 / roadmap A2)

> Codex plan-gate: GO (round 3, 2026-09-20). Cam mechanism ack: given (2026-09-20).
> Supersedes the parked `2026-09-18-route-activation.md`.

## Frame

The unified-slice lane (Task 4.5) is built, certified (D9 cert green, engine #164),
and gated behind `unified_slice_enabled`, which defaults `False` in `run_pipeline`
(`_pipeline.py:174`). Nothing uses it by default today.

Two things keep it off for real platform jobs:

1. **The global default is `False`.** No caller opts in, so even the CLI and library
   callers never take the lane.
2. **The platform worker always attaches a sink.** `_isolated_worker._run` builds
   `ParquetTransactionalSink(staging_output_dir)` unconditionally and passes it to
   `run_pipeline` (`_isolated_worker.py:225`), because the worker does not know the
   route in advance and a sink is a no-op for the full-frame route. But
   `cheap_admission` declines on `sink is not None` (`_unified_slice_admission.py`),
   so every platform job is turned away regardless of the flag.

Cam has acked (2026-09-20) extending admission so the worker's inert sink stops
disqualifying full-frame jobs, then flipping the default. This plan does exactly
that and nothing more.

## The change

### Edit 1 -- admission: stop declining on an inert sink

In `cheap_admission` (`src/decoy_engine/execution/_unified_slice_admission.py`),
split the combined decline:

```python
# before
if sink is not None or source_loader is not None:
    return None

# after
if source_loader is not None:
    return None
# `sink` is intentionally NOT a decline: cheap_admission's first check already
# established route == "full_frame" (not chunked, not native). On the full-frame
# route the sink is never consumed -- the legacy full-frame route ignores it
# (`_isolated_worker.py:225`'s own comment; `_finalize_outputs` reads
# result.outputs), and the admitted path returns outputs in-memory identically
# (this module returns via `_execute_admitted`, which is never passed `sink`).
# The sink's fate is decided by ROUTE, not by which full-frame implementation
# runs, so admitting a full-frame job that happens to carry a sink is
# behavior-preserving. A streaming/OOC job -- the only route that writes the
# sink -- is already declined above by the route/chunked/native checks.
```

`source_loader` stays a hard decline: it signals lazy/relationship loading (a
different output contract, `_isolated_worker._load_sources` with `lazy=True`),
outside this slice's single-resident-table scope.

### Edit 2 -- flip the default

`src/decoy_engine/execution/_pipeline.py:174`: `unified_slice_enabled: bool = False`
becomes `= True`. Update the docstring at `_pipeline.py:253` (the "both default
False" line) to record the new default and that admission remains the safety gate.

Admission is the real gate. Even with the flag on globally, only the certified
subset takes the lane: a single non-FK Parquet mask table, pandas substrate,
full-frame route, exact schema match, no `when:`/vault/validators/quarantine/
STORM/relationships/transforms, resident types inside the reviewed per-strategy
domain. Everything else falls through to the unchanged legacy route.

## Why it is safe

- **Only the unified lane's own admission domain lets a job through -- not a
  universal upstream diversion.** The precise routing fact: sequential and OOC
  branches return in `run_pipeline` before unified admission is reached; a chunked
  job is declined by the `route_chunked` check; any remaining `route == "full_frame"`
  job is then subject to the unified lane's independent, conservative
  resident-contract admission (single non-FK Parquet mask table, pandas substrate,
  exact schema, reviewed per-strategy type domain, etc.). OOC routing is
  relationship-oriented, so a large non-FK job can still be full-frame -- it is the
  admission domain, not upstream routing, that keeps such a job on the correct
  lane. The streaming/OOC route (the only one that writes a sink) is never reached
  by an admitted job.
- **The sink is inert on the full-frame route.** The full-frame legacy route does
  not write the sink; the admitted path returns `ExecutionResult(outputs=...)`
  in-memory and is never handed the sink (`_execute_admitted` has no `sink`
  parameter; `sink` is passed only to `cheap_admission`). So sink fate is identical
  across both full-frame implementations. Confirmed by Codex plan-gate:
  `run_pipeline` passes the sink only to the sequential/OOC branches, both of which
  return before unified admission.
- **Output contract matches the D9-certified parity -- with one permitted
  difference.** The worker consumes `result.outputs`, `quality_metrics`, and
  `table_kinds` via `_finalize_outputs`. The admitted lane produces byte-identical
  outputs, schema (`check_metadata=True`), `warnings`, `row_errors`, and
  `table_kinds`, and `quality_metrics` equal to legacy AFTER removing the one
  `QUALITY_METRICS_KEY` activation-evidence leaf the lane intentionally adds (the
  D9 contract, `_assert_quality_metrics_parity` in `test_unified_slice_parity.py`).
  `timings` / `boundary_conversion_ms` under `quality_metrics["execution"]` may
  differ and are not part of the parity contract.

## Behavior and failure modes

- Admitted platform job (golden shape, with sink): now runs the unified lane,
  returns identical outputs in-memory plus the `QUALITY_METRICS_KEY` activation
  leaf, sink untouched, worker stages outputs through `_finalize_outputs` exactly
  as before (never through sink `commit`). Faster wall + bounded RSS (per cert).
- Non-admitted job (anything outside the subset): unchanged legacy route.
- Streaming/OOC job (with sink, route != full_frame): declined at the first
  admission check; sink written by the streaming route as before.
- Flag explicitly `False` (inertness suite, opt-out callers): fully inert, no
  `execution.physical` import, exactly as today.

## Acceptance tests (write/adjust before impl)

In `tests/physical/test_unified_slice_admission.py`:

1. **Flip `test_cheap_admission_declines_sink_present` (line 106) →
   `test_cheap_admission_admits_inert_sink_on_full_frame`**: a golden-shape
   full-frame candidate carrying a non-None sink is now ADMITTED
   (`_cheap_ok(..., sink=object()) is not None`).
2. **New `test_cheap_admission_still_declines_sink_on_non_full_frame`**: sink
   present AND `route != "full_frame"` (or chunked, or native) still declines --
   proving the route check, not the sink, is what guards streaming.
3. Keep `test_cheap_admission_declines_source_loader_present` (line 113) green --
   `source_loader` still declines independently of `sink`.

In `tests/physical/test_unified_slice_parity.py` (reuse the existing
`_assert_full_parity` / `_assert_quality_metrics_parity` contract -- do NOT
hand-assert byte-identical `quality_metrics`, which is wrong because the lane adds
the `QUALITY_METRICS_KEY` leaf and `timings`/`boundary_conversion_ms` legitimately
differ):

4. **Worker-shaped parity (the certification extension -- HIGH).** D9's existing
   parity arms use NO sink, so sink-present is a genuinely new admitted domain, not
   a reachability of an already-certified one. Add a real spawned-`_isolated_worker`
   test: a single-table **Parquet** config in the admitted strategy/type domain,
   run once with `unified_slice_enabled` **omitted** (exercising the new default)
   and once with an explicit `unified_slice_enabled=False`; assert exact
   staged-Parquet schema + value parity between the two, matching `table_kinds`,
   `quality_metrics` parity under the normalized contract (drop `QUALITY_METRICS_KEY`;
   ignore `timings`/`boundary_conversion_ms`), and that the on-run's activation leaf
   is present and covers every node. (The current `test_unified_slice_isolated_worker.py`
   is CSV and asserts the OLD `sink is None`-required decline; leave it as the
   non-admissible-shape guard, and add this Parquet sibling as the admitted case.)

In `tests/physical/test_unified_slice_inertness.py`:

5. **Repurpose `test_default_omitted_flag_is_also_inert` (line ~95) →
   `test_default_omitted_flag_now_activates`.** With the default flipped, an omitted
   flag now runs `cheap_admission` and admits the golden shape; assert activation
   (the poison must be removed and replaced by an admitted-path assertion). KEEP the
   explicit-`False` inertness tests (`test_flag_off_never_calls_*`, and the
   fresh-subprocess `execution.physical`-never-imported probe) unchanged -- flag-off
   inertness is still contractual.
6. **New sink-inertness-by-method test.** Run an admitted golden-shape job through
   `run_pipeline` with `unified_slice_enabled=True` and a **spy** sink (a
   `TransactionalSink` subclass recording calls, or monkeypatch
   `ParquetTransactionalSink.write` / `write_batches` / `commit` / `abort`); assert
   every one of those call counts is zero AND `result.outputs` carries the admitted
   table resident. (Directory-emptiness alone is insufficient evidence -- assert
   method non-use.)
7. **New streaming-still-uses-sink guard (route-safety, MEDIUM).** Poison
   `_execute_admitted`, then run a real streamed/OOC route that carries a sink, and
   assert the sink is written/committed via the legacy streaming route (the poison
   never fires). Proves an admitted lane never intercepts a sink-writing job.

Default-flip guard:

8. Run the full engine suite after Edit 2. The dedicated unified-slice files set
   the flag explicitly, EXCEPT the omitted-default test repurposed in (5). Any
   incidental `run_pipeline` caller that now hits the admitted path must still pass
   (parity guarantees identical outputs); investigate -- do not silently re-pin --
   any test that changes.

## Default-flip release preconditions (folded from the superseded 2026-09-18 plan)

Flipping the library default is a public behavioral compatibility change for every
`run_pipeline` caller of an admitted shape (SDK/embedding callers included) -- that
IS "production default." Three preconditions, all hard:

- **P1 (BLOCKER) -- D9 recert at the candidate commit.** The flip commit itself must
  carry a GREEN `d9_certified: true` from `bench_compare.py --require-cert` on the
  reference host (GCE n2-standard-8) under the 1.25x 1M budget, preserved as an
  artifact. The cert arms set the flag explicitly, so the number is unchanged by the
  flip -- this is a confirmation run on the exact merge artifact, not a re-derivation.
  One GCP run (budget 30, 1 used). Merge precondition.
- **P2 (HIGH) -- AST omitted-flag caller sweep (not grep).** Flipping the default
  silently changes every `run_pipeline` / isolated-worker `**kwargs` caller that
  omitted the flag AND can satisfy admission. Build an AST inventory of direct calls,
  wrappers/partials/mocks, and isolated-run kwargs across `src/`, `tests/`,
  `scripts/`. Classify each omitted-flag call by whether it can satisfy admission.
  Then: legacy-oracle tests that mean to test the legacy path get an explicit
  `unified_slice_enabled=False` (preserves intent); default-contract tests update to
  prove an omitted flag ACTIVATES an admitted shape while explicit `False` stays
  inert (the `test_default_omitted_flag_is_also_inert` inversion in acceptance test 5
  is one instance of this class, not the whole set).
- **P3 (MEDIUM) -- whole-dict `quality_metrics` consumer inventory.** The additive
  default-on delta is the single `quality_metrics[QUALITY_METRICS_KEY]`
  (`"unified_slice_activation"`) leaf. (`["execution"]` already exists on legacy
  full-frame results -- a parity field, not new; `["execution_plan"]` is conditional
  on `explain_plan`, not default-on.) The activation leaf is manifest-safe free-form
  provenance, not a determinism/byte-golden change, but newly-admitted omitted-flag
  callers now emit it. Find every consumer asserting WHOLE-dict `quality_metrics` equality on
  an admitted shape and update it (keyed assertions are unaffected). Test both the
  default-on telemetry delta and explicit-`False` stability (no keys added).

## Docs to update (default-off is documented in several places)

- `CHANGELOG.md` (line 208 "default OFF") -- new entry: unified route activated as
  default; admission is the gate; sink-present full-frame now admitted; note the
  library-default behavioral change under compatibility.
- `CODEMAP.md` (lines 53, 56, 148 -- "default-off", "Task 4.6 caller activation") --
  correct to activated-default; note the activation slice done.
- `src/decoy_engine/execution/_unified_slice.py:1` -- module docstring opens
  "DEFAULT-OFF"; update it. Sweep for the hyphenated/uppercase forms
  (`DEFAULT-OFF`, `default OFF`) as well as `default.*[Ff]alse`, across `src/`,
  `docs/`, and `*.md`.
- `_pipeline.py` docstring (line 253 "both default False").
- Mark `docs/plans/2026-09-18-route-activation.md` `Status: superseded` (pointer to
  this plan). Do NOT claim CLI behavior: this library repo has no CLI; the CLI and
  platform live in separate repos and are out of scope for this slice.
- Roadmap A2 + `RECENTLY-SHIPPED.md` (platform repo) on merge.

## Blast radius and rollback

- Two-line behavioral change plus the test/doc updates above. Rollback = revert the
  default to `False` (instant global kill-switch) and/or restore the combined sink
  decline.
- No new dependency and no config-schema change, but this IS a public
  library-default behavioral change: an admitted-shape `run_pipeline` caller that
  omits the flag now takes the unified lane and emits the activation
  `quality_metrics` keys. Covered by P2/P3 above and the CHANGELOG compatibility
  note; not "internal-only."
- The platform side needs no change to benefit: the worker already calls
  `run_pipeline` without pinning the flag, so the new default reaches it. (Phase
  3.3/3.4 platform work is a separate, later slice for explicit enablement +
  bounded release; not required for this activation.)

## Out of scope

- Task 4.7 (delete superseded routing) -- follows this, separate slice.
- Widening the admitted domain (more strategies, CSV sources, `when:` gates, FK) --
  each is a separately-certified slice, never bundled here.
- Any telemetry/reason-code threading for declines.

## Gate sequence

FRAME (done) → PLAN (this revision) → Codex plan-gate → build on own branch (Opus;
delicate: admission-safety edit + default flip + the P2 sweep + the acceptance
matrix) → SELF-CHECK (ruff/mypy on the diff) → VERIFY (full engine suite green;
coverage+mutation on the changed admission unit) → dennis review → Codex FINAL gate
→ **P1 D9 recert GREEN on the candidate commit** → Cam ack (already given) → merge.
P2 and P3 land inside the build (they are test/inventory work); P1 is the last
pre-merge gate.

cam

https://claude.ai/code/session_016uret1DdCHsDFC9ZeZ4XsH
