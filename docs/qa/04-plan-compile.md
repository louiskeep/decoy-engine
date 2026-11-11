# 04. Plan compile

Status: plan

## What it covers

`compile_plan(config, profile, decoy_engine_version=...)` turns a validated config and a
profile into a frozen `Plan`. Also `run_config_only_checks`, `validate_plan`,
`plan_to_yaml` / `plan_from_yaml`, `pipeline_config_hash` and `job_seed_for_config`.

## Expected behaviour

- The plan is frozen. Nothing mutates it after compile.
- Compile is where cross-field problems are caught: missing column references, cyclic
  `derived` references, unknown checksum schemes, a keyed column with no namespace, a
  vaulted column with no namespace.
- Compile reads no row data beyond the profile it is given.
- The same config and profile compile to the same plan and the same hash.
- A stored plan reloads to an equivalent plan. A plan naming an unknown format or
  catalog version never executes.
- Secrets are never written into the plan. The plan holds a reference only.
- A snapshot referenced by a generate column is read once at compile, and its bytes and
  SHA-256 are pinned. Generation never reopens the file.

## Checks

| Id | Run this | What should happen |
|---|---|---|
| PLAN-01 | Compile the same config and profile twice, in two processes. | Identical plan YAML and identical hash. |
| PLAN-02 | Try to set an attribute on a compiled `Plan`. | Raises. The plan is frozen. |
| PLAN-03 | `derived` column referencing a column that does not exist. | `PlanCompileError(derived_missing_column_ref)`. |
| PLAN-04 | Two `derived` columns that reference each other, then a three-column cycle. | `PlanCompileError(derived_cyclic_ref)` for both. |
| PLAN-05 | `fpe` with `checksum: lunh` (misspelled); `checksum: iban`; `checksum: vin` with a digits-only charset. | `fpe_checksum_unknown_scheme`, `fpe_checksum_iban_unsupported`, `fpe_checksum_charset_incompatible`. |
| PLAN-06 | `cardinality_mode: deterministic_map`. | Refused with the rename message pointing at `deterministic: true` plus a mode. |
| PLAN-07 | `allow_collisions: true` together with `cardinality_mode: unique`. | Refused with `allow_collisions_mode_conflict`. |
| PLAN-08 | Compile with a mask secret set, write the plan with `plan_to_yaml`, search the file for the secret. | The secret is absent. Only the reference is present. |
| PLAN-09 | Round-trip: `plan_to_yaml` then `plan_from_yaml`, then execute both plans on the same source. | Byte-identical outputs. |
| PLAN-10 | Compile a generate config with a statistical column, delete the snapshot file, then generate from the compiled plan. | Generation succeeds from the pinned bytes. A plan reloaded from YAML without the snapshot fails with `plan.statistical_snapshot_not_pinned`. |
| PLAN-11 | Hand-edit a stored plan to name an unknown declared-casting format. | It is refused at load or before execution. It never runs. |

## Open points

- `explain_plan` on `run_pipeline` surfaces the planner's classification. No doc states
  the exact shape of that output; QA needs it before checking it.
- Forward references in generate-mode `derived` columns are caught at evaluation, not
  at compile (the docs call this a carry-forward). Roadmap row 24k says validate should
  refuse them. Check which is true now.
