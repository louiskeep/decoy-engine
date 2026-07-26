# TQ coverage audit: what to sweep, in what order

Step 3 of the Test-Quality Program (2026-07-25): decide which codebase sections
the mutation-graded oracle sweep (TQ-2 / step 4) covers, and in what order.
Priority = blast radius (a bug here corrupts output or leaks) x churn (how often
it changes) x correctness-criticality. 339 engine modules total; this ranks the
sections and names the first sweep batches.

## Already covered (TQ-0 + crown jewels, branches `tq/ri-fk-graph` + `tq/crown-jewels`)

`relationships/_graph.py` (RI model), `execution/_fk_keys.py` (RI runtime),
`determinism/_hkdf.py`, `determinism/_derive.py`, `keyprovider.py`,
`transforms/fpe.py` (crypto), `quality/dp.py`, `quality/dp_budget.py`,
`quality/dp_provenance.py` (DP). These are the 100%-mandatory families.

## Section rollup (churn = commits in 120 days)

| Section | mods | LOC | churn | Priority | Why |
|---|---|---|---|---|---|
| `execution` | 41 | 13.9k | 190 | **P0** | runtime engine core; a bug corrupts every job's output |
| `transforms` | 19 | 5.6k | 88 | **P0** | the masking transforms themselves; a bug = wrong/leaky masked data |
| `execution/_strategies` | 26 | 3.8k | 81 | **P0** | per-column masking handlers; same stakes as transforms |
| `plan` | 25 | 6.2k | 99 | **P1** | config -> plan compile correctness; gates what runs |
| `quality` | 21 | 6.9k | 89 | **P1** | DP done; snapshot / synth_report / post-checks remain |
| `execution/out_of_core` | 16 | 6.3k | 32 | **P1** | large-data FK + memory path; RI + never-OOM critical |
| `generation` + `generators` + `generation/statistical` | 11 | 3.7k | 115 | **P1** | synthetic-data generation correctness |
| `config` | 13 | 1.7k | 49 | **P2** | config validation; fail-closed but lower blast radius |
| `profile` | 9 | 2.1k | 23 | **P2** | source profiling feeds routing/planning |
| `storm` | 11 | 3.3k | 69 | **P3** | ML detector; advisory, off by default, lower correctness stakes |
| `providers_v2`, `disguises`, `connectors`, `sdk`, `expressions`, `validation*` | ~16 | ~3k | low | **P3** | narrower surface / already example-tested |

## Sweep order (step 4 batches, ~6-8 modules per wave, blast x churn within a section)

Each module runs the playbook: author oracle suite -> mutmut grade -> kill logic
survivors -> ledger. Same parallel-author + grade harness as the crown jewels.

- **Batch A (execution core):** `_pipeline.py`, `_pandas_adapter.py`,
  `_sequential.py`, `_chunked.py`, `_planner.py`, `_pipeline_route_exec.py`,
  `_adapter.py`, `_chunked_fk.py`.
- **Batch B (masking transforms):** `transforms/code_set.py`,
  `transforms/date_shift.py`, `transforms/_codeset_loader.py`,
  and the highest-churn `execution/_strategies/*` (`_top_code.py`,
  `_categorical.py`, `_nested.py`, `_code_set.py`, `_orphan.py`).
- **Batch C (plan compile):** `plan/_compile.py`, `plan/_checks.py`,
  `plan/_checks_dp.py`, `plan/_serialize.py`, `plan/_types.py`.
- **Batch D (out-of-core + generation):** `out_of_core/_runner.py`,
  `_budget.py`, `_join.py`, `_batch_join.py`, `generation/synthesize.py`,
  `generators/columns.py`, `generation/statistical/_spec.py`.
- **Batch E (quality remainder + config):** `quality/snapshot.py`,
  `synth_report.py`, `quarantine.py`, `validation_result.py`, `context.py`,
  `config/_tables.py`, `config/_global_settings.py`.
- **Batch F (P3 tail):** storm, profile, providers_v2, connectors, expressions
  as budget allows.

## Explicitly deferred / skipped (log, do not silently drop)
- `__init__.py` re-export shims (no logic to mutate).
- Modules under ~60 LOC unless correctness-critical (folded into a sibling's
  suite where they share behavior).
- `storm/model_pack/trainer.py` and ML training paths: graded only if the ML
  extra is installed; otherwise noted as uncovered.
- Anything requiring the certified DP profile is graded on the CI cert-gate,
  not the default shell (see the crown-jewel DP notes).

## Coverage ledger

| Module | authored / graded | logic-mutant score | notes | branch |
|---|---|---|---|---|
| `relationships/_graph` (pilot) | yes / yes | 100% (27 equiv) | none | `tq/ri-fk-graph` |
| `determinism/_hkdf` | yes / yes | 100% (7 equiv) | none | `tq/crown-jewels` |
| `determinism/_derive` | yes / yes | 100% (3 equiv) | none | `tq/crown-jewels` |
| `keyprovider` | yes / yes | 100% (25 equiv) | resolve_mask_key precedence gap closed | `tq/crown-jewels` |
| `transforms/fpe` | yes / yes | 100% (~42 equiv) | Luhn self-ref + Feistel KATs pinned | `tq/crown-jewels` |
| `execution/_fk_keys` | yes / yes (re-graded post-fix `7e7be68`) | 100% (32 equiv) | continue/break + dtype gaps; 1 dead branch; float/Decimal route fix re-graded, 110/142 killed, all 32 survivors equivalent | `tq/crown-jewels` |
| `quality/dp_budget` | yes / yes | 100% (18 equiv) | calibration + tolerance-masked mutants | `tq/crown-jewels` |
| `quality/dp` | yes / partial (pure layer) | pure layer 100% (18 equiv) | 441 mutants, 124 killed; pure/fail-closed request layer graded to logic-100% (3 direct tests kill delta-except + two-bin boundary + DpError.message); OpenDP mechanism (293 mutants) cert-gated, deferred to CI cert-gate | `tq/crown-jewels` |
| `quality/dp_provenance` | yes / yes | 100% (51 equiv) | re-graded: 309 mutants, 253 killed, 51 equiv (46 wording + 1 encode-case + 1 str-arg + 3 env-conditional version-guard); 36 logic killed by 17 new direct tests | `tq/crown-jewels` |

**Result: both 100%-MANDATORY families (crypto + RI/FK) fully graded to
logic-100%.** 7/8 crown jewels graded (dp_provenance re-graded to logic-100% once
its implementation functions were tested directly rather than through the
monkeypatched gate). Only `quality/dp` remains partly deferred: its OpenDP
mechanism path is cert-gated and grades on the CI cert-gate profile (its
pure/fail-closed layer is gradeable locally). No source bugs found; findings in
`tq-findings.md`.

## Step 4 (full-codebase sweep) -- IN PROGRESS
Batches A-F above. Harness that works: the MAIN LOOP owns each mutmut run as a
tracked `run_in_background` bash job (survives turns), serially; a fresh agent
does the fast classify+kill per module (no mutmut). Do NOT delegate the mutmut
run to a subagent (it outlasts the turn and loops).

**Tractability finding (2026-07-25):** the execution SUBSTRATES (Batch A:
`_pandas_adapter`, `_sequential`, `_chunked`, ...) are primarily
INTEGRATION-tested; their covering suites are slow (`tests/unit/execution` = 174s
full-run) so a broad-selection mutmut run is multi-hour per module. The
per-column STRATEGIES and TRANSFORMS have fast focused unit suites
(`test_top_code.py` = 0.5s) and grade in minutes. Grade strategies/transforms
with a FOCUSED direct-unit-test selection first (conservative: it under-counts
integration-test kills, so any test written to kill a "survivor" still adds real
focused coverage; note the scoped selection in each ledger). Substrates need
either scoped selections per module or a dedicated multi-hour background program.

**Substrate harness pathology (2026-07-26):** the first substrate attempted,
`execution/_planner.py`, exposed a hard blocker: mutmut's in-process runner
misreports genuinely-surviving mutants as ⏰ timeout on the heavy pandas/pyarrow
substrate suites (verified: a sampled "timeout" mutant SURVIVES in ~3s under
standalone pytest, exit 0). Raising `timeout_constant` to a 225s limit did not
change it, so it is a runner/suite interaction, not a tuning issue -- see
tq-findings.md #8. `_planner` is DEFERRED (not falsely scored); the Batch-A
substrate tier needs a runner that shells out to standalone pytest per mutant, or
a different mutation tool. Flagged for Cam.

**Step-4 modules graded:**
| Module | mutants / killed | logic score | branch |
|---|---|---|---|
| `execution/_strategies/_top_code` | 415 / 354 | 100% (61 equiv) | `tq/crown-jewels` |
| `execution/_strategies/_truncate` | 90 / 76 | 100% (14 equiv) | `tq/crown-jewels` |
| `execution/_strategies/_categorical` | 238 / 194 | 100% (44 equiv) | `tq/crown-jewels` |
| `execution/_strategies/_bucket_perturb` | 56 / 54 | 100% (2 equiv) | `tq/crown-jewels` |
| `execution/_strategies/_shuffle` | 54 / 51 | 100% (3 equiv) | `tq/crown-jewels` |
| `transforms/windowed_date` | 76 / 75 | 100% (1 equiv) | `tq/crown-jewels` |
| `execution/_strategies/_text_mask` | 128 / 123 | 100% (5 equiv) | `tq/crown-jewels` |
| `transforms/text_mask` (core) | 281 / 239 | 100% (42 equiv) | `tq/crown-jewels` |
| `transforms/code_set` | 342 / 296 | 100% (46 equiv) | `tq/crown-jewels` |
| `execution/_strategies/_derived` | 19 / 18 | 100% (1 equiv) | `tq/crown-jewels` |
| `execution/_strategies/_nested` | 303 / 246+37to | 100% (20 equiv) | `tq/crown-jewels` |
| `transforms/derived_aggregate` | 58 / 50 | 100% (8 equiv) | `tq/crown-jewels` |
| `transforms/bucket_perturb` (core) | 140 / 136 | 100% (4 equiv) | `tq/crown-jewels` |
| `execution/_strategies/_text_redact` | 125 / 118 | 100% (7 equiv) | `tq/crown-jewels` |
| `execution/_strategies/_orphan` (RI) | 159 / 142 | 100% (17 equiv) | `tq/crown-jewels` |
| `execution/_strategies/_hash` | 34 / 31 | 100% (3 equiv) | `tq/crown-jewels` |
| `execution/_strategies/_bucketize` | 132 / 117 | 100% (15 equiv) | `tq/crown-jewels` |
| `execution/_strategies/_redact` | 29 / 26 | 100% (3 equiv) | `tq/crown-jewels` |
| `execution/_strategies/_composite` | 122 / 108 | 100% (14 equiv) | `tq/crown-jewels` |
| `execution/_strategies/_faker` | 79 / 70 | 100% (9 equiv) | `tq/crown-jewels` |
| `execution/_strategies/_formula` | 19 / 17 | 100% (2 equiv) | `tq/crown-jewels` |
| `transforms/_fpe_checksum` | 193 / 153 | 100% (40 equiv) | `tq/crown-jewels` |
| `transforms/formula` | 51 / 40 | 100% (11 equiv) | `tq/crown-jewels` |
| `transforms/derived` | 87 / 84 | 100% (3 equiv) | `tq/crown-jewels` |
| `transforms/group_key` | 23 / 22 | 100% (1 equiv) | `tq/crown-jewels` |
| `transforms/grouped_series` | 136 / 110 | 100% (26 equiv) | `tq/crown-jewels` |
| `transforms/joint_mask` | 196 / 168 | 100% (28 equiv) | `tq/crown-jewels` |

Remaining strategy modules with clean-ish focused tests (next up): `_bucketize`,
`_composite`, `_nested`, `_categorical`, `_shuffle`, `_truncate`, `_text_mask`,
`_windowed_date`; zero-direct-test gaps to author-then-grade: `_geo_generalize`,
`_bucket_perturb`, `_derived`, `_derived_aggregate`, `_formula`. Then transforms
(`date_shift`, `code_set`), then plan, then the integration-heavy substrates.
