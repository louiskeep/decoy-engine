# CONFIG-EXPLICIT E2a build record: declared-columns module, codes, codemod modes

Status: record

Branch `feat/config-explicit-e2a`, base `48346650` (E1 head, unmerged). Handoff
`p05-config-explicit-build-handoff.md` r8, Part E2a. No route calls the new module, so run behavior
is unchanged.

## What changed

- `profile/_declared_columns.py`: `declared_columns`, `source_columns`, `declaration_findings`,
  `check_columns_declared`, `DeclarationRequest` (four constructors), `PreparedDeclaredSources` and
  `prepare_declared_sources`. `check_columns_declared` is the one place that turns a finding into a
  `source_refusal`; the probe and the capability methods call it through the module global.
- Registry 1.11.0: `source_file.undeclared_column`, `source_file.undeclared_table` (owner
  `PipelineValidationError`), frozen fixture `reason-manifest-v1.11-config-explicit-e2a.json`,
  regenerated consumer contract (only `registry_version` lines move).
- Codemod modes `declare-columns` and `source-schema`, the `declare_columns_probe.py` pytest plugin and
  `tests/_declared_schema.py`.

## Registry

`REGISTRY_VERSION` moves to 1.11.0, which was free on the base.

## Codemod

Python dict literals only. A YAML fixture that needs a declaration prints `hand` (not implemented in
E2a). The `Literal` of `DeclarationFinding.kind` holds `table` and `column`; E2b-r widens it.

## Named limits

- `PreparedDeclaredSources.checked_chunks` checks every chunk against the declared set. It does not keep
  the `for_chunks` schema's names separately, because a name outside the declared set is already refused.
- The AT-E2a-7 corpus has no `coherent_with` case. It does hold an empty-strategy entry and an
  unlisted scalar FK child: `compile_plan` accepts both, and `declared_columns` and
  `known_output_columns` agree on them.

## Not verified

Full suite (the orchestrator runs the T3 on the stack top). macOS arm64 run.
