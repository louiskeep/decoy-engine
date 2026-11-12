# Unique generated columns

Status: current

A generated column can promise that no two rows hold the same value. A junction table can
promise that no two rows hold the same pair of parent keys. This guide covers the two options
that do this, `unique` and `unique_together`, and how they interact with `pooled`.

Without these options a generator draws each row on its own, so repeats are normal. A faker
column repeats once its draws collide. A pooled faker column holds about 10,000 entries,
often with fewer distinct values. A junction table built from two `reference` columns can repeat a pair.

## `unique: true` on a column

Set `unique: true` on a generate column. It is accepted on four types:

| Type | How a unique column is drawn |
|---|---|
| `sequence` | Unchanged. A sequence counts by its step, so the option adds no check and changes no value. |
| `categorical` | Samples the distinct categories without replacement, in a seeded order. |
| `faker` | Draws as usual and redraws a row whose value was already used, up to 1,000 attempts per row. |
| `reference` | Samples the parent's distinct keys without replacement. `sequential` takes them in parent order. |

```yaml
tables:
  - name: parts
    row_count: 500
    generate_columns:
      - name: part_id
        type: sequence
        start: 1
        step: 1
      - name: part_code
        type: faker
        faker_type: uuid4
        unique: true
      - name: bin_label
        type: categorical
        categories: ["B01", "B02", "B03", "B04", "B05"]
        unique: true
```

This example is refused at validation: `bin_label` has 5 categories for 500 rows. See
[Domain checks](#domain-checks).

The value must be a real boolean. `unique: "true"` and `unique: 1` are refused with
`config.generate_option_type`. `unique: false` is the same as leaving it out.

### Types that refuse `unique`

Any other type (`formula`, `derived`, `statistical` and the rest) refuses `unique: true` with
`config.generate_unique_unsupported`. Their values come from a computation or a fitted
distribution, so the engine has no domain it can sample without replacement.

### Options that conflict with `unique`

These combinations are refused with `config.generate_unique_conflict`, because each one draws
with replacement or by weight:

- `pooled: true` on any type.
- `weights` on a `categorical` column.
- `distribution: weighted` or `distribution: fitted` on a `reference` column.
- `min_per_parent` or `max_per_parent` above 0 on a `reference` column.

### Nulls

A null never counts as a repeat. SQL treats nulls the same way in a `UNIQUE` constraint.

- `null_probability` still applies. The non-null values stay distinct.
- A faker value of `None` is kept and not tracked.
- A `None` entry in `categories` is a reusable null. Rows past the distinct categories get
  null, so the domain check always passes.
- A `reference` column reads the parent's distinct non-null keys. Null parent keys are skipped.

### How values are compared

Two values are the same when they compare equal. Lists, tuples and dicts are compared by
content, so a category or parent key of that shape works. A faker value that cannot be compared
at all stops the run with `runtime.uniqueness_impossible`.

## `unique_together` on a table

`unique_together` lists pairs of `reference` columns on one table. Each listed pair takes
distinct parent-key pairs across rows. Each column alone may still repeat.

```yaml
tables:
  - name: shelves
    row_count: 20
    generate_columns:
      - name: shelf_id
        type: sequence
        start: 1
        step: 1
  - name: items
    row_count: 50
    generate_columns:
      - name: item_id
        type: sequence
        start: 1
        step: 1
  - name: item_shelves
    row_count: 600
    unique_together:
      - [item_id, shelf_id]
    generate_columns:
      - name: item_id
        type: reference
        reference_table: items
        reference_column: item_id
      - name: shelf_id
        type: reference
        reference_table: shelves
        reference_column: shelf_id
```

`item_shelves` can hold up to 50 x 20 = 1,000 distinct pairs, so 600 rows fit.

The rules, each refused with `config.generate_unique_conflict` when broken (a member of the
wrong type fails earlier, at schema validation):

- Each entry names exactly two different columns of the same table.
- Both columns are `type: reference`.
- A column appears in at most one entry.
- A member sets none of `unique`, `null_probability`, `weights`, `min_per_parent` or
  `max_per_parent`.
- A member has no `determinism: fresh` and no `distribution` other than `random`.

The pair is drawn once, at the turn of whichever member is declared first. Declared column
order is otherwise unchanged.

## Domain checks

A unique column needs at least one distinct value per row. Validation checks what it can know
before any value is drawn, and the run checks the rest.

At validation, a column whose domain is too small is refused with
`config.generate_unique_domain_too_small`:

- `categorical`: the number of distinct non-null categories is below `row_count`, and no
  category is `None`.
- `reference`: the parent table's `row_count` is below this table's `row_count`.
- `unique_together`: the product of the two parents' `row_count` values is below this table's
  `row_count`.

A parent `row_count` is an upper bound on its distinct keys. A parent column can still hold
repeats or nulls, and a parent that is not a generate table with a plain integer `row_count` is
not checked at validation. So the run checks again against the real distinct keys.

At run time, a draw that cannot be honored stops with `runtime.uniqueness_impossible` (a
`GenerationError`). This happens when:

- a `reference` parent has fewer distinct non-null keys than the rows needed;
- a `unique_together` pair's real key product is below the rows needed, or is larger than
  Python's `sys.maxsize`;
- a `faker` row repeats an earlier value on all 1,000 attempts;
- a `faker` value cannot be compared.

The engine never truncates a column, wraps around to reuse values, or fills the gap with nulls.

### Faker domains are not checked up front

A faker type has no fixed list of values, so validation cannot count its domain. A faker type
with few possible values fails at run time. Close to its limit, the run is likely, not certain,
to finish: each row gets 1,000 attempts, and the last free values are found by chance. Leave
headroom. A sequence or a categorical column gives a hard guarantee instead.

### The refusal message

The message names the table, the column, the kind of draw and counts only: the rows needed,
the distinct values found, and for faker the row where retries were exhausted or an
uncomparable value appeared. It never quotes a
generated or parent value. If the parents of `item_shelves` held only 400 distinct pairs, the
run would stop with:

```text
item_shelves.(item_id, shelf_id): unique pair needs 600 distinct value(s) but only 400 exist. Lower row_count, drop unique, or use a source with more values.
```

## Pooling (`pooled`)

A faker column on one of the pool-eligible faker types pools its values automatically at
50,000 rows or more. A pool builds about 10,000 values once and samples rows from it, so a
pooled column repeats.

- `pooled: false` turns pooling off for that column.
- `pooled: true` asks for nothing extra. Pooling is still decided by the row count. On a unique
  column it is refused as a conflict.
- `unique: true` turns pooling off. A unique faker column always draws row by row.

## Determinism

A unique column with the default `determinism: stable` is deterministic per seed, like any
generated column. The same config and seed give the same values. A single unique column can
still set `determinism: fresh`; its values are then not guaranteed to be reproducible across runs. `unique_together` refuses
`fresh`.

`unique` is left out of the column's seed fingerprint, as `pooled` is. So adding `unique: true`
keeps the column's seed root:

- For `faker`, the first attempt of each row is the plain per-row draw. A row that did not
  collide keeps the value it had without `unique`. A redraw reseeds from its own derived seed.
- For `categorical` and random `reference` draws, sampling without replacement replaces the
  per-row choice, so outputs may change. A sequential `reference` keeps parent order and stops
  instead of wrapping.
- A `unique_together` pair is seeded from its first-declared member.

The draw sites are `gen.faker_unique_retry`, `gen.categorical_unique`, `gen.reference_unique`
and `gen.unique_pair` in the draw-site catalogue
(`src/decoy_engine/execution/native/_draw_sites_gen.py`).

## Routes and memory

A unique draw reads the whole column's history, so some routes do not run it:

- Streamed generation: a `unique` column (other than a `sequence`) or a `unique_together`
  member is not eligible. Eligibility is plan-wide, so the whole plan runs on the whole-table
  path. The reasons are `unique` and `unique_pair`.
- Windowed `generate_rows`: the same columns make the table Class B (not seekable), with the
  same two reasons.
- Pinned plans: a table with `unique_together` is refused with
  `plan.pinned_unique_pair_unsupported`.

The memory ledger reserves room for a unique column's seen set (`unique_set_bytes` in
`generation/_bounds.py`).

## Methods

The draws follow established methods. They are listed in the
[methodology registry](../methodology-registry.yaml).

- Faker's `fake.unique` proxy keeps a seen set and gives up after 1,000 attempts
  (`_UNIQUE_ATTEMPTS`). The engine uses the same limit but not the proxy itself. The proxy
  advances one shared state, so a row would depend on how many retries earlier rows needed.
  Here each retry has its own seed.
- Sampling without replacement uses CPython's `random.Random.sample`, as the masking pool's
  UNIQUE mode does.
- A composite key on a junction table is the SQL `UNIQUE (a, b)` constraint, Django's
  `unique_together`, and the composite keys SDV generates for bridge tables.

## Where it lives

| Piece | File |
|---|---|
| Config checks | `src/decoy_engine/config/_generate_unique.py` |
| Draws | `src/decoy_engine/generation/_unique.py` |
| Capacity rule | `unique_capacity_ok` in `src/decoy_engine/generation/pool/_capacity.py` |
| Run-time refusal | `raise_uniqueness_impossible` in `src/decoy_engine/generation/pool/_errors.py` |
| Reason codes | [Reason codes](../reasons.md) |
