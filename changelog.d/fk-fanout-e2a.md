### Added (fitted reference snapshot checks, QA-FK-FANOUT E2a, 2026-11-09)

A `reference` column with `distribution: fitted` now has its `snapshot_file` read once and pinned
into the Plan, like a `statistical` column. Compile, `run_config_only_checks` and `plan_from_yaml`
refuse a fitted column with no `snapshot_file`, an unreadable snapshot, a snapshot with no fan-out
for the source column (including every DP snapshot), an invalid fan-out block, or `weights`,
`min_per_parent` or `max_per_parent` set. A loaded Plan whose manifest dropped the fitted snapshot
raises `plan.statistical_snapshot_not_pinned`. Messages name fields only, never a stored value. One
wrapper, `check_generate_snapshot_columns`, now runs the statistical and fitted checks at all three
sites.
