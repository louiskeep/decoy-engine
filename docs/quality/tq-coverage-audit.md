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

## Coverage ledger (fill as batches land)
| Batch | modules covered | logic-mutant score range | bugs found | branch |
|---|---|---|---|---|
| crown jewels | 8 | (grading in progress) | see `tq-findings.md` | `tq/crown-jewels` |
| A-F | pending | | | |
