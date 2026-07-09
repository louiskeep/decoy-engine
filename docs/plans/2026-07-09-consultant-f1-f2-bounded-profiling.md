# Program: Bounded-Memory Profiling + Reject-Before-Read (consultant-2026-07-09 F1/F2)

Source of goal: external architecture review `docs/engine-consultant-findings-2026-07-09.md`
(Codex/gpt-5.5), findings **consultant-2026-07-09 F1** and **F2**, independently code-verified
2026-07-09 against commit `02b18cc` on `main`.

> Disambiguation: this document addresses the findings numbered **F1/F2 in the 2026-07-09
> consultant review**. It has nothing to do with the older, already-remediated
> `docs/remediation-source.md` register (a June council review with its own F1-F17 numbering).
> Every "F1"/"F2" below means **consultant-2026-07-09 F1/F2** unless it explicitly says
> "remediation-source". Number collision is coincidental.

This is a **program** (a set of sprints), authored for Cam sign-off in the same shape as
`docs/plans/2026-07-06-100m-row-scaling-program.md` (the SC0-SC6 scaling program this extends).
Nothing here is built yet. Build tiers are assigned per the model-tiering directive. The task-ID
prefix is **SC7** because this closes the one gap the SC0-SC6 program left in the *public*
entrypoint's bounded-memory story.

## Why this program exists (the one-paragraph problem)

The SC0-SC6 program made FK masking **bounded-memory at the runner level**: `run_fk_out_of_core`
and `run_sequential` stream with peak RSS that does not scale with table cardinality, and
`decide_execution_route()` (`execution/_pipeline_routing.py:262-430`) will **reject a too-big FK
job before the read** (`fk_full_frame_oom_risk_rejected`) rather than let it OOM full-frame. But
the *public* entrypoint `run_pipeline()` still calls `profile_source(config, seed=job_seed)` at
`execution/_pipeline.py:302` **before** that route decision runs at `_pipeline.py:323-349`, and
`profile_source()` eagerly materializes every declared source into a full pandas frame
(`profile/_source.py:154-163` for files, `:176-290` for S3/GCS). So a large job submitted through
the real API can be killed by the OOM-killer *inside profiling* — before the engine ever reaches
the bounded route it built, and before the "reject before read" gate whose name promises exactly
this cannot happen. The runner-level benchmarks we are running right now (50M live, 100M next) do
**not** cover this: they call the low-level runners directly and never touch `profile_source()`.
This program removes the eager full materialization from profiling so that the *existing*
admission/reject sequence becomes genuinely pre-expensive-read, and adds the end-to-end
`run_pipeline()` memory proof the consultant flags as the missing test.

## Current landed state (verified 2026-07-09, not assumed)

- **`run_pipeline()`** (`execution/_pipeline.py:153-587`) accepts sources two ways: `config["sources"]`
  **descriptors** (paths / S3 keys / GCS objects), profiled eagerly by `profile_source()`; and a
  `sources: dict[str, pa.Table]` kwarg of **caller-loaded resident** Arrow frames, consumed by the
  masking route. A truly bounded invocation is `sources={}` + a lazy `source_loader` (the runner
  loads/evicts per table); a resident `sources` dict means the caller already holds everything in RAM.
- **The eager read is `profile_source()`, not the route.** `profile_source()` (`profile/_source.py:37-125`)
  loops `config["sources"]`, calls `_load_source()` -> `_load_file_source()` / `_load_s3_source()` /
  `_load_gcs_source()`, each of which does a flat `pd.read_csv` / `pd.read_parquet` (or fetches the
  whole S3/GCS object into a `BytesIO` first), then hands the **full frame** to `walk_dataframe`.
- **`walk_dataframe` already samples.** With the default `sample_rows=10_000`, `distinct_count` is
  already a reservoir sample over at most 10k rows (`profile/_walk.py:9-14`); only `sample_rows=None`
  is a full-column scan. So profiling *materializes the whole frame only to then sample 10k rows from
  it* — the materialization is the waste, not the sampling.
- **`TableProfile.row_count` / `ColumnProfile.row_count` are exact ints** (`profile/_types.py:85,170`)
  with two validated invariants: `null_count <= row_count` and `distinct_count <= row_count`
  (`_types.py:113-121`). This is the one field that must stay **exactly true**, not sampled.
- **The route decision keys off resident sources.** `largest_mask_table_rows()`
  (`_pipeline_routing.py:137-157`) reads `caller_sources[...].num_rows`; on the lazy path
  (`sources={}`) it returns `None`, so the SC2 size gates (`decide_execution_route`
  `out_of_core_threshold_rows` / `full_frame_reject_rows`) **never fire** and the reject-before-read
  is silently disabled for exactly the bounded-input shape that most needs it.
- **The out-of-core compat gate needs the compiled plan.** `out_of_core_admission()`
  (`_pipeline_routing.py:113-134`) reads `build_work_list(plan, registry)`; `plan = compile_plan(config,
  profile, ...)` needs the profile. Relationships/graph, by contrast, derive **purely from `config`**
  (`profile/_source.py:95-98`, `_derive_*`), so the FK graph does not need source data.
- **A cheap lazy Parquet reader already exists.** `out_of_core/_source.py:LazySource` holds only a
  `Path` and exposes `num_rows` via `pq.read_metadata(path).num_rows` (footer only, no column data),
  `schema` via the footer, and `iter_batches(batch_rows)` streaming — the exact metadata+streaming
  machinery this program needs, currently used only by the OOC runner.
- **`RELEASE_PHASE = "pre-ga"`** (`release.py:27`): hard-delete / breaking-signature changes are
  allowed (CLAUDE.md "Pre-GA = hard delete"). No back-compat shim is owed for internal seams.

## Established-methodology survey (CLAUDE.md "Use established methodology")

The design is deliberately not novel; it applies three well-worn patterns and cites them in the
implementing modules' docstrings:

- **Deferred materialization / metadata-first admission** — Spark and Dask build a logical plan and
  only materialize on an action (`.compute()` / `.collect()`), deciding partitioning from cheap
  metadata first. We apply the same split: decide the route from cheap source metadata, materialize
  (a bounded sample) only after.
- **Parquet footer metadata without reading row groups** — `pyarrow.parquet.read_metadata(path).num_rows`
  and `ParquetFile.schema_arrow` read only the file footer (the same call `LazySource` already makes).
  This is the exact pattern `LazySource` cites; the profile layer will reuse the *same* reader, not a
  second copy.
- **Bounded Arrow batch reads** — `ParquetFile.iter_batches(batch_size=...)` decodes one batch at a
  time (Arrow's own row-group/batch streaming), which is how a bounded profiling sample is drawn
  without a full-frame read.

The CSV asymmetry (below) is handled by the honest fallback the review's F2 item 3 recommends
(explicit operator override where cheap exact metadata is structurally impossible), not by guessing.

## GATE-F (Cam) — PROPOSED, awaiting sign-off

Each item is a decision that changes the sprint set. Recommendation stated; Cam confirms or overrides.

1. **The fix is "make profiling bounded," NOT "move admission before profiling."** Once
   `profile_source()` no longer fully materializes a source, the *existing* post-profile admission
   sequence (`decide_execution_route`) is already "before the expensive read": profiling touched only
   cheap footer metadata + a bounded sample, and the masking runner still streams. This avoids
   duplicating the out-of-core compat surface (which needs the compiled plan) into a second, earlier
   admission point — precisely the multi-decision-surface drift the consultant warns about in F3/F6.
   *Propose: accept — bounded profiling is the whole fix; do not build a parallel pre-plan admitter.*
2. **Row-count source of truth flips from resident `caller_sources` to cheap descriptor metadata.**
   The SC2 size gates will read the per-table `row_count` the (now-cheap) profile carries — exact from
   Parquet/fixed-width footers, estimated for CSV — so the reject-before-read fires on the lazy
   (`sources={}`) path too, closing the `largest_mask_table_rows() is None` hole. *Propose: accept.*
3. **CSV cannot give a cheap exact row count; use an O(1) byte-size estimate + require an explicit
   `execution_mode` in the ambiguous band.** A CSV has no footer. Newline-counting is O(bytes) I/O
   (a full object transfer on S3/GCS — defeats the purpose) and still not free locally, so we do NOT
   scan. We `stat()` the file, divide by the mean row width of the bounded header sample, and mark the
   result an **estimate**. The reject gate treats an estimate at/above a **conservative** CSV ceiling
   as "cannot safely full-frame" and either reroutes (if OOC-eligible) or **rejects with a message
   telling the operator to convert to Parquet or pass an explicit `execution_mode`** — never a silent
   full-frame read of a CSV we estimate is huge, and never a silent reject of one we estimate is fine.
   Rationale: an estimate can be wrong in both directions, so the coarse gate fails toward
   operator-visible choice, and the guidance ("use Parquet for large sources") aligns with an existing
   constraint — the OOC runner's `LazySource` is Parquet-only anyway. *Propose: accept.*
4. **fixed_width row count is O(1) and exact.** `rows = filesize // record_length` from the declared
   `FixedWidthLayout`; treat it as exact like Parquet, not estimated like CSV. *Propose: accept.*
5. **Bounded profiling keeps `row_count` exact and leaves null/distinct exactly as sampled as they
   already are.** `row_count` comes from cheap metadata (exact for Parquet/fixed_width, estimate flag
   for CSV). `distinct_count`/`null_count` come from the bounded sample — which, at the default
   `sample_rows=10_000`, is byte-identical in *meaning* to today (already sampled). The one behavior
   change is `sample_rows=None` (explicit full-scan) on a source too big to hold: that combination
   must degrade to a bounded scan with a loud `QualityWarning` rather than silently OOM. *Propose:
   accept; the full-scan-on-huge-source case is documented as sampled-with-warning, not a full read.*
6. **Additive to the compatibility contract; internal-seam changes are hard edits.** The `ProfileSource`
   protocol and the profile's new `row_count_exact` flag are internal; `profile_source()`'s public
   signature stays source-compatible (same return type, new optional kwarg). No frozen public-surface
   break pre-GA. *Propose: accept.*

## Design

### The one structural change: a `ProfileSource` reader protocol, bounded by default

Introduce a `ProfileSource` protocol (the review's F1 item 1 sketch, made concrete) that separates the
three things profiling actually needs — and that today are entangled in one eager `pd.read_*`:

```
class ProfileSource(Protocol):
    def row_count(self) -> RowCount: ...          # RowCount(value: int, exact: bool)
    def schema(self) -> pa.Schema: ...            # cheap for parquet/fixed_width; header read for csv
    def sample_frame(self, sample_rows: int) -> pd.DataFrame: ...   # bounded read, <= sample_rows
    def to_frame(self) -> pd.DataFrame: ...        # full eager read — small-job / opt-out fallback
```

Implementations, one per existing descriptor type, sharing **one** lazy-Parquet reader with the OOC
runner (do not duplicate `LazySource`):

| descriptor | `row_count()` | `schema()` | `sample_frame(n)` |
| --- | --- | --- | --- |
| `file`/parquet | footer `read_metadata().num_rows` (exact) | footer `schema_arrow` | `ParquetFile.iter_batches`, take first `n` |
| `file`/fixed_width | `filesize // record_length` (exact) | from `FixedWidthLayout` | `read_fixed_width` first `n` rows |
| `file`/csv | `stat().st_size // mean_row_bytes` (**estimate**) | header + sampled dtypes | `pd.read_csv(nrows=n)` |
| `s3`/parquet | footer via `pyarrow.fs.S3FileSystem` ranged read (exact) | footer | ranged `iter_batches` first `n` |
| `s3`/csv | `head_object` `ContentLength // mean_row_bytes` (**estimate**) | ranged header read | ranged `read_csv(nrows=n)` |
| `gcs`/parquet | footer via `GcsFileSystem` ranged read (exact) | footer | ranged `iter_batches` first `n` |
| `gcs`/csv | `blob.size // mean_row_bytes` (**estimate**) | ranged header read | ranged `read_csv(nrows=n)` |

`LazySource` (`out_of_core/_source.py`) is **promoted** to the shared profile/source location (e.g.
`profile/_readers.py` or `execution/_source_reader.py`) and becomes the parquet-file `ProfileSource`
implementation; the OOC runner imports it from the new home. This is the "single lazy Parquet reader"
the consultant's F1 item 1 asks for, and it removes the duplication risk between profiling and OOC.

### `profile_source()` gains a bounded default; `to_frame` is the opt-out

`profile_source(config, *, sample_rows=10_000, seed=None, residency="bounded")`:

- `residency="bounded"` (new default): for each descriptor, build its `ProfileSource`, read
  `row_count()` (cheap) + `schema()` + `sample_frame(sample_rows)` (bounded), and hand the bounded
  frame to `walk_dataframe`. The resulting `TableProfile.row_count` is set from `row_count().value`
  (the **true total**, exact or estimated), not `len(sample_frame)`; `distinct_count`/`null_count`
  stay sample-derived exactly as the `sample_rows` default already makes them. The `exact` flag rides
  onto the profile as a new `row_count_exact: bool` field (additive) so downstream admission knows
  whether it is trusting a Parquet count or a CSV estimate.
- `residency="full"`: the historical path (`to_frame()` per source) — kept for callers that pass
  `sample_rows=None` and genuinely need whole-column distincts on a source small enough to hold, and
  as the explicit escape hatch. `sample_rows=None` + `residency="bounded"` degrades to a bounded scan
  with a `QualityWarning` (GATE-F #5) rather than OOMing.

Nothing else in `profile_source`'s contract moves: same `Profile` return type, same relationship
derivation (already config-only), same seed handling.

### Route admission becomes genuinely pre-expensive-read — with no new decision surface

In `run_pipeline`, the sequence stays in the same order but the eager read is gone from step 302:

1. `profile = profile_source(config, seed=job_seed)` — now **bounded** (cheap metadata + <=10k-row
   sample), carrying an exact-or-estimated `row_count` per table.
2. `plan = compile_plan(config, profile, ...)` — unchanged.
3. `out_of_core_routing_signals(...)` / `decide_execution_route(...)` — unchanged **logic**, but the
   size signal now comes from the profile's `row_count` (via a new
   `largest_mask_table_rows_from_profile(profile, table_kinds)`) instead of only from resident
   `caller_sources`. The reconciliation is per mask table (H1): a resident table uses its `num_rows`,
   a lazy table uses its profile `row_count`, and the size signal is the max across them — so a mixed
   job with a huge lazy table plus a tiny resident one cannot under-size the gate. On a resident table
   an exact profile-count disagreement warns (does not hard-assert) and routes on the resident count;
   when `caller_sources` is empty (lazy path) the profile row-count is used — closing the `None` hole.
4. The reject-before-read raise (`fk_full_frame_oom_risk_rejected`) now fires *before any full
   materialization has happened anywhere in the lifecycle*, because profiling no longer materialized
   and the masking runner has not yet been called. That is the F2 semantic fix, achieved without a
   second admitter.

**CSV estimate handling at the gate:** `decide_execution_route` gains awareness of `row_count_exact`.
For an **estimated** (CSV) largest table whose estimate is at/above `full_frame_reject_rows` and that
is not OOC-eligible, it raises a distinct, actionable code
(`fk_full_frame_oom_risk_rejected_estimated`) whose message says "row count is a CSV size estimate
(~N rows); convert the source to Parquet for an exact count or pass an explicit `execution_mode`."
An estimated table that is OOC-eligible reroutes to streaming as usual (estimate good enough to prefer
the bounded route). A CSV comfortably below the threshold is unaffected.

### What does NOT change

- The compat gate (`check_out_of_core_compatibility`) stays the single out-of-core admission surface,
  still reading the compiled plan. We do **not** re-derive strategy admissibility from config in a
  second place (F3/F6 drift risk). The plan is now cheap to compile because the profile under it is
  cheap.
- The masking runners (`run_fk_out_of_core`, `run_sequential`, chunked) are untouched.
- `walk_dataframe`, the `Profile`/`TableProfile` shapes (beyond the additive `row_count_exact`), and
  the relationship derivation are untouched.

## Backward compatibility / blast radius

- **`profile_source()` call sites** (grep-verified): the only non-test callers are
  `execution/_pipeline.py:302` (this program owns it) and re-exports in `profile/__init__.py`. Tests:
  `tests/unit/test_v2_cloud_sources.py` (:200,:283,:309), `tests/unit/test_v2_fixed_width_source.py`
  (:341). All call `profile_source(config)` positionally; the new `residency` kwarg defaults to
  `"bounded"`, so signatures stay source-compatible. The cloud/fixed-width tests assert profile
  *contents*; where they assert whole-column `distinct_count` on a >10k-row fixture they must move to
  `residency="full"` or `sample_rows=None` — a small, mechanical test edit, flagged per file in SC7a.
- **`TableProfile` gains `row_count_exact: bool`** (additive, defaulted `True` so existing constructions
  are unaffected). Any golden profile fixture that pins the full field set gets the new key; identified
  and updated in SC7a.
- **`LazySource` import move**: `out_of_core/` imports it from the new shared home. One-line internal
  import change, pre-GA hard edit — no external contract.
- **No frozen public-surface break.** `run_pipeline` / `profile_source` public signatures stay
  source-compatible; the changes are internal seams + one additive profile field. Per `RELEASE_PHASE
  = "pre-ga"`, even the internal seam moves are allowed as hard edits with no shim.
- **Behavior change operators can observe:** a lazy-path (`sources={}`) large FK job that previously
  slipped past the size gates (because `largest_mask_table_rows()` was `None`) will now be routed to
  out-of-core or rejected before read. This is the *intended* fix, but it is a routing-behavior change
  for that shape and must be called out in release notes + validated by SC7c.

## Sprints

Task-ID prefix **SC7** (extends the SC scaling program). Engine unless marked platform.

### SC7a — `ProfileSource` protocol + bounded `profile_source()`  ·  tier: **Opus** (novel core seam)
Introduce the `ProfileSource` protocol and the per-descriptor readers; promote `LazySource` to the
shared home and repoint the OOC runner's import. Rewrite `profile_source()` to the `residency="bounded"`
default: cheap `row_count()`/`schema()` + bounded `sample_frame()`, `TableProfile.row_count` from true
metadata, new additive `row_count_exact` field. Implement the CSV byte-estimate + fixed_width O(1)
count + Parquet/cloud footer readers. Degrade `sample_rows=None` + bounded to a warned bounded scan.
Update the two cloud/fixed-width test files + any golden profile fixture for the additive field /
sampled-vs-full distincts.
- **AC:** `profile_source()` on a Parquet source **never** calls `pd.read_parquet` on the whole file
  (assert via monkeypatching `to_frame`/`pd.read_parquet` to raise — the consultant's F1 acceptance
  sketch, made concrete); `TableProfile.row_count` equals the true footer count for Parquet, `filesize
  // record_length` for fixed_width, and a flagged estimate for CSV; `row_count_exact` is `True` for
  parquet/fixed_width and `False` for csv; existing profile-content tests pass (adjusted where they
  asserted whole-column distincts on >10k fixtures); ruff/mypy clean; full regression gate green.

### SC7b — Wire the profile row-count into route admission (F2 reject-before-read)  ·  tier: **Opus** (routing correctness)
Add `largest_mask_table_rows_from_profile(profile, table_kinds)` and feed it into
`out_of_core_routing_signals` / `decide_execution_route` so the SC2 size gates fire on the lazy
(`sources={}`) path. Thread `row_count_exact` through so an **estimated** CSV largest table at/above
`full_frame_reject_rows` raises the distinct `fk_full_frame_oom_risk_rejected_estimated` code with the
"convert to Parquet or set execution_mode" message; an estimated OOC-eligible table still reroutes.
Reconcile PER MASK TABLE (not a single scalar max): for each mask table use its resident
`caller_sources[name].num_rows` when that table is resident, else its profile `row_count`
(carrying that table's `row_count_exact`), then take the max across tables. This is required for the
mixed partial-residency shape (`run_out_of_core_route` resolves *missing* tables through
`source_loader`): a scalar "any resident source -> trust the resident max" rule would let a huge lazy
child hide behind a tiny resident parent and re-open the F2 OOM hole. **Implemented behavior note
(deviates from an earlier "assert agreement" sketch):** for a RESIDENT table whose resident count
disagrees with its EXACT profile count, the build emits a `RuntimeWarning` and routes on the resident
count rather than hard-asserting. A hard assert would be wrong: a caller may legitimately pass a
resident source that differs from the on-disk descriptor the profile read (pre-filtered/transformed
input, which run_pipeline masks as given and which `profile_source` never sees), and the resident
count is authoritative for that run. The warn is scoped to resident tables only; a lazy table's
profile count is trusted without cross-check (there is no resident count to compare).
- **AC:** a lazy-path (`sources={}`, `source_loader` set) FK job whose largest Parquet source is
  >= `full_frame_reject_rows` and is *not* OOC-eligible now raises `fk_full_frame_oom_risk_rejected`
  **before** `source_loader` is ever called (assert the loader is untouched); the same job with a
  supported strategy set reroutes to `out_of_core`; a CSV-source variant at the same size raises
  `fk_full_frame_oom_risk_rejected_estimated` with the Parquet-conversion message; the resident-sources
  path still agrees with the profile count; routing decision + reason land in
  `ExecutionResult.quality_metrics["execution"]`.

### SC7c — End-to-end `run_pipeline()` bounded-memory proof  ·  tier: **Sonnet** (run) + **Opus** (interpret)
Add the missing end-to-end sentinel the consultant flags (Test Gaps #1). Two proofs, both through the
**public** `run_pipeline()`, not the low-level runners:
1. A `run_pipeline(execution_mode="out_of_core", source_loader=..., sink=...)` job whose profile path
   is monkeypatched so any resident full-frame conversion (`to_frame`/`pd.read_parquet` on the whole
   file) **raises** — proving profiling did not eagerly materialize.
2. A regression test where large Parquet-backed FK inputs complete through `run_pipeline()` under a
   memory cap (`resource.setrlimit` / the existing OOC memory-sentinel harness) that full-frame
   *profiling* would exceed — proving end-to-end boundedness, not just runner boundedness.
Plus the telemetry-honesty test from F1's third sketch: `execution.loaded_fully_in_memory` cannot be
`False` if profiling materialized all sources.
- **AC:** both `run_pipeline()` memory proofs pass under the cap; the telemetry-honesty assertion holds;
  the tests run in the `perf`-marked suite and attach to the PR; no regression in the SC0-SC6 parity /
  out-of-core sentinels.

## Dependency graph / sequencing

```
SC7a (bounded profile_source + readers) ──▶ SC7b (wire row-count into admission) ──▶ SC7c (e2e memory proof)
```
Strictly linear: SC7b needs the profile's cheap `row_count` from SC7a; SC7c proves the composed result.
SC7c's memory-cap harness reuses the SC0-SC6 out-of-core sentinel infrastructure, so no new tooling.

## Non-goals (explicit — this pass is F1/F2 only)

- **F3 routing consolidation** (one `ExecutionPlanner` owning every route; retiring
  `PLANNER_ROUTING_ENABLED`). This program deliberately does **not** add or move a decision surface;
  consolidating them is the separately-scoped F3 work.
- **F4-F9** (public stub gates, polars substrate matrix, out-of-core admission API, DB/SFTP sources,
  stale mypy overrides, module-size decomposition). Untouched here.
- **New source/target types.** DB and SFTP stay deferred; this program only adds *reader residency
  modes* to the existing file/S3/GCS descriptors.
- **A fully-lazy out-of-core input path** (feeding `LazySource` straight from a Parquet descriptor into
  `run_fk_out_of_core` with zero resident conversion). The OOC route still consumes resident /
  `source_loader`-provided Arrow tables as SC1/SC2 built it; wiring `ProfileSource` readers all the way
  into the runner is a natural follow-on but is out of scope for the F1/F2 profiling fix.
- **Exact CSV row counts.** Structurally impossible cheaply; the byte-estimate + explicit-override
  posture (GATE-F #3) is the accepted answer, not a to-do.
- **Changing masking semantics or byte output.** Every route stays byte-output-neutral; this program
  only changes when/how much source data is *read for profiling*, never how it is masked.

## Process (per sprint, house standard)

DEVELOP → SELF-CHECK (CI-gate mirror: `ruff format --check`, `sphinx -W`, no-extras env,
`pytest -m "not perf"`) → adversarial REVIEW (dennis) → REMEDIATE → docs (barry) → CI-gate →
GATE-2 (Cam) → merge. SC7c additionally runs `pytest -m perf` and attaches the end-to-end
`run_pipeline()` memory sentinel to the PR.
