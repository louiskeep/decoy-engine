# 11. Relationships and subsetting

Status: plan

## What it covers

Keeping joins intact when several tables are masked together (the `relationships:`
block), orphan handling, composite and self-referencing keys, `from_parent`, and
`decoy_engine.subset` for cutting a referentially intact slice of a Parquet dataset.

## Expected behaviour

- A child key that matched a parent key in the source matches the same parent after
  masking. The mapping is the same across runs.
- Parent and child columns and the relationship all carry the same `namespace`. An
  ambiguous binding is refused at config time.
- Tables are masked parent first. The user does not order them.
- A composite key is matched and remapped as one tuple.
- A self-referencing key (`employees.manager_id` to `employees.id`) keeps its self-join.
- A null FK stays null and is not an orphan.
- `orphan_policy` is required: `preserve` (keep the orphan key as it is), `remap` (give
  it a fresh masked value), `warn` (keep it and emit one aggregated warning with the
  count) or `fail` (abort with `orphan_fk_violation`).
- A row error on a parent key removes that parent row and every child that references
  it, whatever the orphan policy.
- **Subset**: pick seed rows, then close over the FK graph downward (children of kept
  parents) and upward (parents of kept children) until nothing changes. `plan_subset`
  reports exact row counts without writing. A fan-out budget fails the run before any
  write; it never truncates. Parquet only.

## Checks

| Id | Run this | What should happen |
|---|---|---|
| REL-01 | `customers` and `orders` joined on `customer_id`, masked with `hash` under one namespace. | The inner join on masked keys has the same row count as on source keys, and pairs the same rows. |
| REL-02 | REL-01 run twice in separate processes. | Byte-identical masked keys in both tables. |
| REL-03 | Parent and child columns with different namespaces. | Refused at config or compile. The join is never silently broken. |
| REL-04 | Three-column composite key between `enrollments` and `claims`. | The composite join is preserved. A child matching on two of three columns is treated as an orphan. |
| REL-05 | `employees.manager_id` referencing `employees.id`. | The self-join gives the same manager for every employee as in the source. |
| REL-06 | Child rows with null FK. | Null in the output. Not counted as orphans under any policy. |
| REL-07 | Ten orphan child rows under each of the four policies. | `preserve`: keys unchanged. `remap`: new masked values matching no parent. `warn`: unchanged, one warning with count 10. `fail`: `runtime.orphan_fk_violation`, no output. |
| REL-08 | Three-level chain (`patients`, `visits`, `claims`) with FPE keys. | Both joins preserved. Keys keep their format. |
| REL-09 | A parent key cell that row-errors, with a child referencing it, quarantine on. | Parent row and child row are both removed and both appear in quarantine. The raw key is in no output. |
| REL-10 | Relationship naming a table or column that does not exist. | Refused at validation, not by flagging every row at run time. |
| REL-11 | `plan_subset` with a 2% sample of `customers` over a three-table graph. | Returns per-table row counts. Writes nothing. Reads no non-key column. |
| REL-12 | `run_subset` with the same seed, then check every child FK in the output. | Every non-null child key has its parent in the slice. Rerun gives the same slice. |
| REL-13 | `run_subset` with a budget below the closure size; then with a CSV source. | First fails before any Parquet is written, with no truncated output. Second is refused with the "convert to Parquet" message. |

## Open points

- `preserve` leaves the orphan's real key in the masked output. That is a real source
  value published by a masking run. Confirm V1 warns about it, since only `warn` emits
  a warning.
- A pipeline run applies a `subset:` block and masks only the closed subset rows. Check a
  run with `subset_request="skip"` says `{"applied": false}`, that a lazy source rewritten
  mid-run is refused with `runtime.lazy_source_changed`, and that the chunked entries still
  refuse with `config.subset_run_unsupported`.
- The documented when-gate limitation: a child can carry a raw key when a `when:` gate
  left a duplicate parent row unmasked. Confirm a reviewer of the output can tell this
  happened.
- `what-we-cannot-prove.md` notes FPE orphan-remap passes out-of-charset characters
  through. Add a check once the exact behaviour is confirmed.
- `from_parent` (a child column copying its parent's masked value) appears in the demo
  script but has no reference documentation.
