# OOC build-floor preflight: recalibrate the over-rejecting row-linear model

Status: plan (Opus, 2026-09-08). Fresh REDO per Codex's OOC-fix verdict: the
prior `fix/ooc-preflight-floor-recalibration` branch was deleted as obsolete
against the current model; this recalibrates from current-route measurements.
Risk R2 (a refusal gate loosens; the risk is admitting a job that then OOMs, so
the never-under-predict guarantee is the load-bearing acceptance criterion).
Not started. No build until this plan is Codex-plan-gated.

## Frame

The out-of-core route runs a memory PREFLIGHT before either FK driver
(`enforce_ooc_memory_preflight`, `_pipeline_route_exec.py:389`, strictly before
`run_fk_out_of_core`). It prices each parent table's relation-build phase with a
**strictly row-linear floor** and refuses the job when that floor exceeds the
DuckDB cap the build would receive:

```
predict_ooc_build_floor_bytes(rows) = 24 MiB + 190 B/row      (_memory_estimate.py:327)
refuse when floor(rows) > actual_duckdb_cap_bytes(budget, live) (_capacity_eval.py:269)
```

The 190 B/row slope carries the model's "never-OOM" guarantee, and it was fit to
a single binding anchor: a **real-route cloud OOM at 33.3M rows** (build OOMed at
memory_limit ~1638 MB, completed at 2457 MB; GCP n2-standard-8). That anchor is
now stale. It was measured against the **pre-Phase-4 co-resident dedup** -- the
`arg_max(struct_pack(...))` / window form that held **O(distinct-key) resident
state** (the docstring at `_relation.py:470-508`: "arg_max(struct) over 33.3M
groups OOMs at a 1638 MB limit after spilling ~7.9 GB futilely; only passes at
16 GB because ~3 GB of state happens to fit -- exactly the failure mode this fix
exists to remove").

Phase 4 replaced that with a **spillable split-dedup** (`_relation.py:466-538`):
COPY parent keys to `staged.parquet`, `max(__decoy_row_nr) GROUP BY join_key` to
`winners.parquet` (aggregate state is one int per group, fully external), then an
external hash join of winners back to staged. The winners land on disk between the
aggregate and the join, so the O(distinct-key) resident state the 190 B/row slope
prices **no longer exists**. The perf proof (`test_out_of_core_relation_dedup_memory.py::test_..._plateau`)
measures peak RSS **flat from 10M to 20M distinct keys**, bounded at ~1.6x the
DuckDB `memory_limit`, i.e. a cardinality-independent envelope of the cap, not a
row-linear resident floor. Inline measurement in the same source: "20M groups
complete at a 600 MB limit, spilling ~2.1 GB."

Consequence: the preflight refuses jobs the current builder survives. Codex's
verdict enumerated the over-rejections, each enshrined by a test:
- 20M rows, sink, 4 GiB host -> refused (`test_ooc_memory_preflight_seam.py:160`);
  builder plateaus well under budget.
- 9.2M rows, resident fan-in-1, 4 GiB host -> refused
  (`test_out_of_core_memory_estimate.py::TestCodexRound2BoundaryCases`).
- 100M rows total, 8 GiB host -> refused.
- 300k rows, 64 MiB budget -> refused ("~81 MB floor" vs ~64 MB cap,
  `test_capacity_estimate_job.py:286`); Codex notes the builder runs 300k in ~3.9s.

The gate's STRUCTURE is sound and stays: the decimal-cap comparison
(`actual_duckdb_cap_bytes`, the base-10 "NNMB" re-read), the two fan-in guards,
the warn band, and the typed hard-fail before any DuckDB runs are all correct and
independently gated. **Only the floor MODEL is wrong** -- it prices a resident
structure Phase 4 deleted. This slice re-derives that model from the current
builder's measured envelope and updates every expectation that hard-codes a
row-count -> refusal.

## The model error, stated precisely

Post-Phase-4 the build has no O(rows) non-spillable state. DuckDB's buffer manager
holds the working set to `memory_limit` and spills the remainder to
`temp_directory`; peak RSS overshoots `memory_limit` by the un-accounted
allocations (hash-agg control structures, allocator fragmentation) that do not
themselves scale with cardinality. So the real quantity the preflight must bound
is **peak RSS as a function of the memory_limit (the cap), plus a fixed
control-structure minimum -- NOT a function of parent rows**:

```
peak_rss(build)  ~=  envelope_factor * memory_limit  +  C_fixed
```

with `envelope_factor` and `C_fixed` cardinality-independent (the plateau proof is
one point on this curve: factor ~1.6 at a 350 MiB limit). The current model's
row term is spurious. The recalibration replaces `base + slope*rows` with a
model of this shape (final form chosen from measurements, section "Measurement"),
retaining the row count ONLY if the sweep shows a genuine residual slope after the
cap term is accounted for.

This also reframes what "refuse" means. Because `memory_limit` is itself derived
from the host budget (sink build: `memory_limit_for(budget, 1)` ~= budget; the
reserve is `max(0.2*ceiling, 2 GiB)`, a flat 2 GiB below a 10 GiB host), the gate
becomes "does `envelope_factor * memory_limit + C_fixed` fit under the host
ceiling, given the reserve headroom" rather than "does an O(rows) structure fit
the cap." A sink job on a 4 GiB host gets a ~2 GiB build budget and a 2 GiB
reserve; whether it fits turns on the overshoot at a 2 GiB limit, which the
measurement must establish (it is NOT safe to assume the 1.6x factor measured at
350 MiB holds at 2 GiB -- a fixed `C_fixed` would make the factor fall toward 1 as
the limit grows, the likely and favorable case).

## Non-goals

- No change to the gate STRUCTURE: decimal-cap comparison, both fan-in guards
  (`out_of_core_fanin_exceeds_budget`), the warn band (`_OOC_MEM_WARN_FRACTION`),
  the typed hard-fail-before-run, the `declared_minimum_ceiling_bytes` inverse,
  and the `evaluate_capacity`/`enforce_ooc_memory_preflight` parity all stay.
  Only `predict_ooc_build_floor_bytes` (and the constants it reads) and its
  inverse's coupling change.
- No change to the two FK drivers, the split-dedup itself, the route policy
  (`decide_route`, 2M reorder threshold), the reserve model, or the budget
  resolution. This slice touches the PREFLIGHT PREDICTION only.
- No new preflight coverage of the reorder route's separate streaming-phase
  budgets (`resolve_reorder_budgets`/`ReorderCaps`) -- the preflight prices the
  SHARED relation-build phase both routes run. If the measurement finds the
  reorder streaming phase can OOM outside the build phase the preflight models,
  that is FLAGGED as a separate finding, not fixed here (it would be its own
  slice; the current preflight never claimed to cover it).
- No relaxation of the never-under-predict rule. A recalibrated floor that
  under-predicts a REAL measured OOM of the CURRENT builder is a build-blocking
  defect, not a tuning choice.

## Measurement (measure-first; the model's constants come from data, not a guess)

The recalibration is data-driven. Before changing any constant, characterize the
current builder's build-phase peak RSS envelope. All measurement uses VmHWM from
`/proc/self/status` in an allocator-pinned fresh subprocess (the harness the
plateau proof already uses; NOT `ru_maxrss`, which survives execve and
over-reports -- the pattern fixed in the Phase-4 seam probes).

Harnesses (extend, do not rewrite):
- `scripts/build_floor_probe.py` -- isolates the relation-build floor under a
  FIXED RLIMIT_DATA so a failure is attributable to `memory_limit` alone. Sweep
  `--rows` x `--memory-limit-mib`.
- `scripts/fk_memory_probe.py --mode out_of_core` -- the full real-route peak-RSS
  harness (the one that produced the stale 33.3M cloud anchor); `--capability`
  compares routes, `--sweep` sweeps cardinality, `--mem-cap-mb`/`--rlimit-kind`
  cap memory. Use `--capability`/route selection to measure batch-join AND
  reorder separately (reorder needs deduped parent keys >= 2M to select).

Measurement matrix (local devbox, up to ~20M rows; the plateau proof already
covers 10M->20M keys):
1. **Envelope vs cap.** For rows in {1M, 5M, 10M, 20M} x memory_limit in {256,
   512, 1024, 2048 MiB}: record peak RSS and whether the build completes.
   Fit `peak_rss ~= envelope_factor * memory_limit + C_fixed`; report whether
   `C_fixed` is bounded and whether `envelope_factor` is flat or falls with the
   limit. This is the model's spine.
2. **Row-independence.** At a FIXED memory_limit, confirm peak RSS is flat across
   the row sweep (extends the existing 10M->20M plateau to the full range and to
   multiple limits). Any residual slope, if real, is measured and priced -- not
   assumed away.
3. **Key width.** Repeat a subset with a wider join key (string / composite) to
   confirm the envelope does not hide a key-width term the row model would miss.
4. **Both routes.** Measure batch-join and reorder build phases separately at a
   cardinality above the 2M reorder threshold; confirm both share the build-phase
   envelope (they both call `_build_relation`).
5. **The refusal boundaries.** Re-measure the exact enshrined over-rejection
   points that are locally reproducible (20M/2 GiB-cap sink; 9.2M/fan-in-1;
   300k/64 MiB) and record pass/fail. These measured outcomes REPLACE the
   hard-coded expectations.

Large-scale (100M) confirmation is NOT locally tractable. Two options, decided at
the VERIFY gate with Cam (do not spend without a Slack ping): (a) rely on the
measured envelope shape + a conservative margin, documenting that the model is
validated by shape to 20M and by the spill model above that; or (b) one GCP run
under the authorized 50-run bench budget to confirm 100M on an 8 GiB host matches
the model. Default to (a) unless the shape fit is ambiguous.

Discard the stale 33.3M cloud anchor as a binding point (it measured a builder
that no longer exists) but RECORD it in the docstring as historical, with the
reason it no longer binds. If the local sweep cannot reproduce ANY OOM of the
current builder (likely -- it spills rather than OOMing), the never-under-predict
guarantee rests on the measured overshoot factor plus margin, stated explicitly.

## Design

1. **Replace the floor model** (`predict_ooc_build_floor_bytes`,
   `_memory_estimate.py`). New signature must still accept what the caller has
   pre-run (`max_parent_rows`) but price the cap-relative envelope. Because the
   caller compares `floor` to `actual_duckdb_cap_bytes(budget, live)`, the cleanest
   correct form prices the floor AS the envelope over that same cap:
   `floor = ceil(envelope_factor * cap_bytes) + C_fixed` evaluated at the cap the
   table will receive, plus any measured residual row term. This requires the
   predictor to see the cap (or the evaluator to compute the envelope inline). Pick
   ONE of:
   - (preferred) move the envelope computation into `evaluate_capacity` where the
     cap is already in hand (`_capacity_eval.py:228`), and reduce
     `predict_ooc_build_floor_bytes` to the fixed `C_fixed` minimum (its role
     becomes the tiny-job/near-zero floor only); or
   - keep a `predict_...` that takes `(rows, cap_bytes)` and returns the envelope.
   The choice is settled by which keeps the two modules under their ~600 LOC caps
   (`test_module_size.py`) and keeps `declared_minimum_ceiling_bytes` invertible.
   The comparison, warn band, and hard-fail logic in `_capacity_eval.py:223-293`
   are unchanged in shape; only the `floor_bytes` value feeding them changes.
2. **Re-derive `declared_minimum_ceiling_bytes`** so its "you need ~N GB" inverse
   stays truthful against the new model. If the floor is now cap-relative, the
   inverse simplifies (the recommended ceiling is the smallest whole GiB whose
   resolved budget yields an envelope that fits) -- keep the existing round-trip
   test (`TestDeclaredMinimumCeiling`) as the correctness oracle: the returned
   ceiling, fed back through `resolve_ooc_memory_limit` then the new envelope,
   must clear the floor at every tested `(rows, incoming, sink)`.
3. **Update the docstrings** to state the Phase-4 model, cite the split-dedup as
   the reason the resident term is gone, record the retired 33.3M anchor as
   historical, and cite the new measured anchors (never re-derive established
   points -- but these points ARE being re-established, so the new docstring is
   the new record of truth).
4. **Update every hard-coded expectation** (section "Acceptance tests") to the
   measured outcome. Where a test derives its boundary from the model by binary
   search (`_max_admitted_rows` in the seam test), it auto-tracks; where a test
   hard-codes "20M on 4 GiB -> refused", flip it to the measured verdict and
   rename it to describe the real behavior.

## Acceptance tests (define behavior before build; no later contributor weakens)

The load-bearing invariant is **never-under-predict a real OOM**; the loosening is
only justified against measured completions. Tests:

1. **Row-independence of the floor (primary, replaces the row-linear pin).** The
   predicted floor for a FIXED cap is flat (within a small tolerance) across
   parent-row counts spanning {300k, 1M, 10M, 20M, 100M}. Directly kills the
   `base + 190*rows` shape. (Pure-function test, no spaCy/DuckDB.)
2. **Envelope tracks the cap.** The floor scales with `cap_bytes`
   (envelope_factor + C_fixed), matching the measured fit from the Measurement
   section within tolerance. Pin `envelope_factor` and `C_fixed` as named
   constants with the measured anchors in their docstrings.
3. **Never under-predicts a measured completion's requirement.** For each measured
   (rows, memory_limit, peak_rss) point, the model's floor at that cap is
   >= the measured peak RSS (the model bounds observed reality). This is the
   never-OOM guarantee, re-grounded on the current builder.
4. **The over-rejections are now admitted -- with a measured witness.** 20M/sink,
   9.2M/fan-in-1, 300k/64 MiB, and 100M/8 GiB each ADMIT (verdict FIT), and each
   has a companion perf/probe test showing the current builder completes that
   point within budget. No point is flipped to FIT without a completion witness.
   (100M's witness is the shape argument or the optional GCP run per Measurement.)
5. **A genuinely-too-large job is still refused.** Construct a point where the
   envelope genuinely exceeds the host ceiling (e.g. a tiny host / high fan-in
   where `envelope_factor * cap + C_fixed > ceiling`), assert INSUFFICIENT with
   the typed code and the "~N GB" message. The gate must still refuse -- proving
   the recalibration is not a blanket fail-open.
6. **Both fan-in guards unchanged.** The `(64 MiB, 67 live)` admit / `(68 live)`
   raise Codex acceptance points and the pure-joiner-leaf guard still hold
   verbatim (they gate on the cap, not the floor).
7. **Hard-fail still precedes any DuckDB run.** `enforce_ooc_memory_preflight`
   raises `out_of_core_insufficient_memory` before the runner is constructed
   (`test_pipeline_route_exec_ooc_memory_preflight.py`), unchanged.
8. **Warn band still fires and never blocks.** A point in
   `[_OOC_MEM_WARN_FRACTION*cap, cap)` warns (structured, non-blocking); the
   hard-fail bound stays the full cap.
9. **Declared-minimum round-trip.** `declared_minimum_ceiling_bytes`, fed back
   through `resolve_ooc_memory_limit` then the NEW envelope, clears the floor at
   every tested `(rows, incoming, sink)` -- the existing round-trip test, re-run
   against the new model.
10. **Evaluator/enforcer parity.** `evaluate_capacity` and
    `enforce_ooc_memory_preflight` return identical code+message on the new
    boundary points (`test_capacity_evaluator.py::TestEvaluatorParityWithMidRunGate`).
11. **Both routes measured separately.** A perf test (opt-in / benchmark-marked)
    asserts the batch-join and reorder build phases both stay within the
    recalibrated envelope at a cardinality above the 2M threshold.
12. **Fail-open on undetectable budget unchanged.** `budget_bytes is None`
    -> UNKNOWN (fail-open); non-OOC / CSV -> NOT_APPLICABLE / UNKNOWN, unchanged.

## Steps

1. Build/extend the measurement harnesses; run the matrix; record the fit
   (`envelope_factor`, `C_fixed`, any residual slope, both routes, key width).
   Write the raw numbers into the sprint-testing ledger and the constants'
   docstrings. This step GATES the constants -- do not pick numbers before it.
2. Replace `predict_ooc_build_floor_bytes` with the cap-relative envelope (chosen
   placement per Design 1), keeping `_capacity_eval.py`'s comparison/warn/hard-fail
   shape. Re-derive `declared_minimum_ceiling_bytes`.
3. Update docstrings (Phase-4 model, retired 33.3M anchor recorded as historical,
   new measured anchors) -- explain WHY the resident term is gone (split-dedup).
4. Rewrite the enshrined expectations to the measured outcomes (tests 1-5, 9-11);
   confirm the derived-boundary tests (seam binary-search) still pass by tracking.
5. Verify: ruff check + format + mypy on every changed file (direct-edit lint
   rule); run the full `execution` unit suite + the OOC perf suite + the parity
   suites; mutation grade on the new envelope logic (factor, C_fixed, comparison,
   warn/hard-fail branches, never-under boundary).
6. dennis quality gate, then Codex final gate. Merge only on Cam's go.

## Risks

- **Loosening admits a real OOM (the central risk).** Mitigated by tests 3 and 5:
  the model bounds every measured peak RSS, and a genuinely-too-large job still
  refuses. The never-under-predict rule is the acceptance bar, not a preference.
- **Envelope factor measured at 350 MiB not holding at 2 GiB.** Mitigated by
  measuring across the {256..2048 MiB} tier range explicitly (Measurement 1)
  rather than extrapolating the single plateau point.
- **100M not locally reproducible.** Mitigated by the shape argument (row-
  independence proven to 20M + the spill model) with the optional GCP confirmation
  gated on Cam; default to shape + margin.
- **Reorder route OOM outside the modeled build phase.** Out of scope but WATCHED:
  Measurement 4 measures the reorder build phase; if its separate streaming phase
  shows an unmodeled OOM risk, that is flagged as a follow-up finding, not silently
  absorbed.
- **Module LOC caps.** The placement choice in Design 1 is partly driven by
  keeping `_memory_estimate.py` / `_capacity_eval.py` under ~600 LOC
  (`test_module_size.py`); if the envelope logic pushes a module over, extract a
  small helper rather than inflating either module.
