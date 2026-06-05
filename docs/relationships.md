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

<!-- VERIFY: the surface YAML field names in the `relationships` block
(`parent` / `children` / `orphan_policy` / `namespace`, and the
`{table, columns}` shape). These mirror the model_dump()-ed config dicts in
tests/integration/golden/test_execution_e2e.py (_orphan_fk_config and the
composite-key config). Confirm the YAML-level field names against the config
schema (decoy_engine/config) or by running `decoy validate` on recipe (b). -->
