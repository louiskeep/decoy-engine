# 01. Config and validation

Status: plan

## What it covers

The pipeline config (`version: 2` YAML or dict) and the one place it is validated:
`PipelineConfig.model_validate(...)`, plus `parse_pipeline_config` and the non-raising
`validate_config`. Everything downstream trusts a validated config and does not
re-validate it.

## Expected behaviour

- The schema is strict. An unknown key, a wrong type or a missing required field is
  refused at validation, before any source is read.
- Validation never changes its input. The caller's dict is the same object, with the
  same contents, after the call.
- Errors name the location (config keys, table, column, list index) and a code. They
  never repeat the submitted value.
- `validate_config` reports problems as a `ValidationResult` and does not raise.
- Whatever a run would refuse on config grounds, validation refuses too (roadmap row
  24b, QA-GATE).
- A default that is left unset is dropped from the dump, so adding an optional field to
  the schema does not move the plan hash of configs that do not use it.

## Checks

| Id | Run this | What should happen |
|---|---|---|
| CFG-01 | Validate the README quickstart config (one CSV source, `faker` and `redact` columns). | Passes. `model_dump()` returns a dict that `run_pipeline` accepts unchanged. |
| CFG-02 | Add an unknown top-level key (`tabels:`) and an unknown column key (`stratgy:`). | Both refused at validation. Each error names the key's location. No source file is opened. |
| CFG-03 | Give a column a `strategy` that does not exist. | Refused at validation with a registered code. The message names the table and column. |
| CFG-04 | Put a recognisable string (`SECRET-VALUE-123`) in a field of the wrong type, then validate. | Refused. The string appears nowhere in the error text, the exception chain or the logs. |
| CFG-05 | Validate a config, then compare the input dict with a deep copy taken before the call. | Identical. Validation did not mutate it. |
| CFG-06 | Call `validate_config` on a valid config, then on a config with three independent field errors. | Returns `None` for the valid config. For the other it raises `PipelineValidationError`; the message counts three problems and lists each by field path, with no values. |
| CFG-07 | Declare a relationship with no `orphan_policy`. | Refused. The policy has no default. |
| CFG-08 | Set a keyed strategy (`hash`, `fpe`, `date_shift`) with no `namespace`. | Refused before any row is masked. `decoy validate` and `decoy run` agree. |
| CFG-09 | Set `quarantine.enabled: true` with an empty `output_path`, then with an unknown trigger name. | Both refused at validation. |
| CFG-10 | Set a pipeline `name:` that is not a string, then one that fails the name rule. | Refused with `config.pipeline_name_invalid`. |
| CFG-11 | Dump a validated config, validate the dump again, dump again. | The two dumps are byte-identical. `pipeline_config_hash` gives the same hash for both. |
| CFG-12 | Write an optional field out at its default value, and compare the plan hash with the same config that omits it. | Same hash. Comparison is by value, not by what was written. |

## Open points

- A raw dict handed straight to `run_pipeline` skips `PipelineConfig`. The docs say some
  checks still run at compile (`when:` grammar, declared columns) and some do not
  ("a config that skips validation keeps today's behaviour"). List exactly which checks
  a raw dict bypasses, and decide whether that is acceptable for V1.
- `quality-policy` config ignores unknown top-level keys, so a mistyped key silently
  disables a check (see section 13). That is the opposite of the strict rule here.
