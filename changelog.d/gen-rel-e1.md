Breaking: a generated `reference` column with infeasible or conflicting per-parent bounds, `null_probability` beside a bound, mismatched `weighted` weights, or an empty parent under `orphan_policy: fail` is now refused instead of generated; a bounded reference whose repair runs produces different values once.

### Changed (per-parent bounds are exact and an empty parent follows its orphan policy, GEN-REL E1, 2026-11-12)

A generated `reference` column now gets the parent and child shape its config asks for or is
refused with a coded reason.

- `min_per_parent` and `max_per_parent` are exact. The repair no longer lets a parent pass
  `max_per_parent` on a feasible request, and an infeasible request is refused instead of
  repaired on a best-effort basis. Validate refuses `min` above `max`
  (`config/generate_per_parent_bounds_conflict`), a bound beside `null_probability`
  (`config/generate_per_parent_null_conflict`), a bound beside `distribution: fitted`
  (`config/generate_fitted_reference_bounds_conflict`) and a maximum the parent `row_count`
  cannot carry (`config/generate_per_parent_infeasible`). The run re-checks both bounds on the
  real distinct parent keys (`runtime/generate_per_parent_infeasible`), refuses a bound on a
  fitted column that skipped validation (`runtime/generate_fitted_reference_bounds_conflict`)
  and checks the repaired counts (`runtime/reference_bounds_unsatisfied`).
- An empty parent with child rows follows the `orphan_policy` of the relationship that covers
  the column: `fail` stops the run (`runtime/reference_parent_pool_empty`), `warn` keeps the null
  column and raises one warning (`warning/reference_parent_pool_empty`), and `preserve`, `remap`
  or no relationship keep the null column silently, as before. A fitted column keeps its
  existing refusal under every policy.
- `distribution: weighted` with a weights list that does not match the real parent key count is
  refused at run (`runtime/reference_weights_length_mismatch`) instead of falling back to
  uniform draws.
- The rules are one check on the in-memory, streamed, whole-table and pinned routes. A pinned
  plan gains the optional `reference_policies` field; a plan without it reads as before, and a
  plan with a covering relationship gets a new fingerprint while its generated values stay the
  same.
- Relationship policy errors (`orphan_fk_policy_invalid`, `orphan_fk_policy_duplicate`,
  `orphan_policy_conflict`) name the tables and columns only; a submitted policy value is no
  longer echoed. `decoy_engine.relationships` exports `normalize_orphan_policy_lookup`.
- A capacity probe keeps per-parent bounds and loosens them to its scaled row counts.

Output changes once, before GA, for a bounded reference whose repair fill runs, on the v2 route
and the V1 `ColumnGenerator` route. Every other config keeps its bytes.
