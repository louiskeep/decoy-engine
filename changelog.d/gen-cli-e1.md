### Changed (config-relative paths, generation warnings and order, GEN-CLI E1, 2026-11-10)

`compile_plan`, `run_config_only_checks`, `run_pipeline`, `run_pipeline_isolated`, `validate_plan`,
`compile_pinned_plan`, `compile_pinned_plan_with_generation`, `estimate_job`,
`estimate_job_capacity` and `capture_physical_plan_inputs` take a new keyword, `base_dir`. A
relative `snapshot_file` is read from that folder, a relative `quarantine.output_path` is
written there (a `quarantine_path` argument still wins and is used as given), and the capacity
estimators resolve `column_names_file` the way they already resolved `path`. With no `base_dir`
nothing changes: a relative path still means the working directory, or the root under a strict
file-access policy. Through `run_pipeline_isolated`, `base_dir` must be an absolute `str`. The
config dict is never rewritten, so `pipeline_config_hash`, plan bytes and every output byte are the
same from any folder. `decoy_engine.fileaccess.config_relative(locator, base_dir)` is the one join,
and replaces the private copy in `execution/capacity.py`. An unreadable relative snapshot now names
the path it tried and the rule.

The generation fidelity gate now hands a structured `QualityWarning`
(`warning.generation_fidelity_below_threshold`) to the new `on_warning` keyword on the four
`generate_tables*` entries, and the pipeline routes fold it into `ExecutionResult.warnings`. The
log line stays. The gate also warns when any one comparable column scores below
`fidelity_warn_threshold`, not only the table's overall score, so a single broken column no longer
hides in a high average. The default 0.8, the key and its bounds are unchanged. `detail` carries
`trigger` (`overall` or `column`) and `below_columns`. A second code,
`warning.seed_ignored_with_master_key`, is registered for hosts to raise (registry 1.22.0).

`decoy_engine.generation.generation_order(config)` returns each generate table with its parents,
parents first, and both generation routes build tables in that order. Ties follow table declaration
order, then first-reference order, so the order no longer depends on `PYTHONHASHSEED`. Per-table
bytes do not change. For a table with two or more parents the order of the returned tables, and so
of written targets, may differ from a run on an earlier build; the order was never part of the
byte contract. A `reference_table` that is not a generate table now raises the same `ValueError` on
both routes.

`decoy_engine.config.generate_option_catalog()` and `GenerateTypeOptions` list each generate
type's required, optional and common option names. The root `decoy_engine` namespace is unchanged.
