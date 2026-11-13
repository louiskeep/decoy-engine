### Changed (created columns and supplied-source profiling, CONFIG-EXPLICIT E2b-r1, 2026-11-12)

`profile_source` takes `supplied_sources`. A supplied table with no descriptor is profiled from the
supplied table, so `run_pipeline` plans every table it masks; a same-name descriptor keeps
profiling its own file. Execution routes build the declaration capability through one choke point that carries a
rule for columns the config creates itself (`derived`, `derived_aggregate`, `grouped_series`,
`windowed_date`, `group_key` and a `derive` transform), read from `SURFACE_DECLARATIONS`. The
native `group_key` batch loop no longer selects its target column, so it runs when the source
lacks the target. No reason code and no new refusal.
