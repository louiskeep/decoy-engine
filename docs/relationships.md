# Relationships and referential integrity

When you mask a parent table and a child table that share a key, the join
between them must survive. Decoy preserves referential integrity across a
multi-table (or whole-folder) mask: the same source key maps to the same masked
key on every table that uses it, so every child row still points at the right
parent row after masking.

This page describes the contract and how to declare it.

## The contract

For a foreign-key relationship between a parent column set and a child column
set:

- Every legitimate child key (one that matches a parent key in the source) is
  masked to the same value the parent key was masked to. The join is preserved.
- The mapping is deterministic: same seed (and same key, where used) yields the
  same masked keys across runs. A masked join is byte-stable, not merely
  internally consistent within one run.
- Composite keys are resolved as a unit: a multi-column FK is matched and
  remapped as one tuple, not column by column.
- Self-referencing keys are supported: a table whose FK points back at its own
  primary key (for example `employees.manager_id -> employees.id`) preserves
  the self-join.
- Null FK values pass through as null. A null is not treated as an orphan.

## Namespaces bind the keys

The mechanism that ties a parent key and a child key together is a shared
`namespace`. Both the parent column and the child column declare the same
`namespace`, and the relationship declares it too. Masking is keyed on
`(seed, namespace, source_value)`, so identical source values under the same
namespace always map to the same masked value, which is exactly what keeps the
join intact.

If two unrelated columns must NOT collide, give them different namespaces. The
engine rejects ambiguous namespace bindings at config time.

## Orphans

An orphan is a child row whose key has no matching parent. The `orphan_policy`
on the relationship decides what happens:

- `preserve`: keep the orphan's key as-is. Legitimate keys are still remapped to
  their masked parent; only the orphan rows are left at their original value.
- `remap`: route the orphan key through the parent's masking strategy, so it
  gets a fresh masked value (it will not round-trip to any real parent).
- `warn`: keep the orphan, and emit one aggregated warning reporting the orphan
  row count.
- `fail`: abort the run with an `orphan_fk_violation` error. Use this when the
  source is supposed to be clean and any orphan is a data problem.

Every relationship must name one of these four policies; the config is rejected
if a relationship omits it.

## Row errors and referential integrity

A row-level masking error (for example an uncoercible `date_shift` cell) on a
FK parent-key column is quarantined out of the parent output. A child row that
references exactly that errored key is cascade-quarantined too, regardless of
`orphan_policy`: the child's masked value is never the raw errored key, and the
child row is removed (covered) or the job fails loud (uncovered), consistently
with the parent's own disposition. This holds for cross-table FK relationships,
composite keys, multi-hop chains, and self-referencing keys alike; a
self-referencing table where one row errors and another row references it will
have both rows removed together (the failing parent and its only referrer),
which keeps the two dispositions consistent rather than leaving a dangling
self-reference.

**Accepted limitation (when-gated duplicate parent key).** When a `when` gate
leaves a parent FK-key row unmasked AND that same raw key value ALSO appears on
a different parent row that row-errored, a child referencing that key value
resolves (via the identity-map contract) to the raw value carried by the
when-gate-unmasked parent row. This is NOT a quarantine escape: the raw value
is present in the child ONLY because the user's `when` gate deliberately left
that duplicate parent row unmasked, so it is ALREADY present in the parent
output. Net-new exposure is NIL. Enforcing "cascade even on a when-gated
duplicate" would break referential integrity: the child would point to
null/quarantine while the parent row survives with the raw key, producing a
dangling reference for a row the user intentionally chose to leave unmasked.
The identity-map contract (an unmasked parent key maps to itself, and children
mirror it) is the correct behavior; this case is documented, not enforced.

## Declaring relationships

Add a `relationships` block to the config. Each entry names a parent (table plus
columns), one or more children, the `orphan_policy`, and the shared `namespace`.
The parent and child columns must also carry that `namespace` in their own
column config.

```yaml
relationships:
  - parent: {table: customers, columns: [customer_id]}
    children:
      - {table: orders, columns: [customer_id]}
    orphan_policy: preserve
    namespace: customer_identity
```

For a composite key, list every column on both sides; the tuples must be the
same length:

```yaml
relationships:
  - parent:
      table: enrollments
      columns: [member_id, plan_id, effective_date]
    children:
      - table: claims
        columns: [member_id, plan_id, effective_date]
    orphan_policy: fail
    namespace: enrollment_identity
```

The engine builds an ordering over the tables so each parent is masked before
its children, then runs them in that order. You do not order the tables
yourself; declaring the relationship is enough.

See [recipes](recipes.md) recipe (b) for a full folder-masking config, and
[determinism](determinism.md) for what "byte-stable across runs" depends on.

## Subsetting a referentially-intact slice

Full-dataset relationship preservation, above, is not always what you want.
Sometimes you need a small, referentially-intact SLICE of a multi-table
Parquet dataset instead of the whole thing, for example "2% of `customers`,
plus every `order`/`order_item` that belongs to them, nothing orphaned."
`decoy_engine.subset` (Sprint G) builds that slice as a pre-mask stage: pick a
seed, close it over the SAME `relationships` graph declared above, then write
the filtered Parquet. Masking runs unchanged on the subsetted output.

This is engine-core only in this release: there is no `decoy subset` CLI verb
yet and no platform UI for it (both are planned follow-ons). Callers use
`decoy_engine.subset.run_subset` / `plan_subset` directly, or add a `subset:`
block to `PipelineConfig` (`config/_subset.py`) to run subsetting as the
pipeline's pre-mask stage; the field is additive and optional, so existing
configs are unaffected.

### How the closure works

Seed selection (a deterministic `sample`, a `filter` predicate, or an
explicit `keys` list) picks a starting row set on one or more root tables.
From there, the closure engine walks the same FK edges the `relationships`
block declares, in two directions, to a fixpoint:

- **Downward (cascade):** a child row whose FK key matches a surviving parent
  row survives.
- **Upward (parent completeness):** a parent row whose key is referenced by a
  surviving child's non-null FK key survives too, so a child is never left
  pointing at a parent that got left out of the slice.

This is the standard graph-reachability-to-a-fixpoint pattern: semi-naive
Datalog fixpoint evaluation, equivalently a Kleene/Knaster-Tarski monotone
fixpoint over a finite powerset lattice. Termination follows from
monotonicity, not from the schema being acyclic: a row already in the
survivor set can never be re-added, so a self-reference or a mutual
A -> B -> A cycle reaches the same no-growth exit an acyclic schema does; no
special-casing for cycles is needed. Upward parent completeness is ON by
default, since silently leaving a dangling FK behind is the failure mode this
feature exists to prevent; turning it off for a given edge requires an
explicit acknowledgement (`allow_dangling=True`) because it can orphan a
child.

### Fan-out safety: dry-run first, hard-fail on budget, never truncate

A closure can fan out further than expected (a popular parent pulls in a lot
of downstream rows). Two protections:

- **Dry-run/estimate is a first-class operation, not a debug flag.** Calling
  `plan_subset` returns the exact per-table row counts the closure would
  produce, with no Parquet write and no read of any non-key column, so an
  operator sees the blast radius before committing to anything.
- **A fan-out budget hard-fails before any materialization.** The budget is
  both a total-output-row cap and a per-table cap (a multiple of the total
  seed row count). If the closure would exceed either cap, the run fails
  closed with no partial Parquet written and no truncation: truncating a
  surviving set after the fact would re-introduce the exact orphans the
  closure exists to prevent.

The subset evidence manifest (`subset-manifest.json`, written alongside the
output) records counts, edges traversed and their direction, and the budget
outcome, but never a raw key value and never a raw filter-predicate literal;
a `filter`-mode seed keeps its predicate's column and operator in the
manifest, and the literal value is redacted.

### Scope

- **Parquet only.** A non-Parquet source fails fast with a specific message
  ("convert to Parquet for subsetting"), both at config-validation time and
  at preflight; there is no degraded full-load fallback.
- **File/batch datasets only.** Database sources are deferred, not
  designed-for.
- **Manual relationship declaration only**, the same `relationships` block
  described above; there is no automatic FK/schema inference for subsetting
  (or for masking).
- **Polymorphic FKs are unsupported**: there is no clean `PlanRelationship`
  representation for a column whose parent table varies row to row.
- **Memory-bounded by the full-frame FK execution path** for this release: a
  subsetting job holds one table at a time during materialization, but a
  single table's frame is still loaded whole. A future sequential per-table
  eviction path will compose with this without a redesign once it merges.

See the CHANGELOG "Sprint G FK-aware subsetting core" entry for the full
module-by-module breakdown.
