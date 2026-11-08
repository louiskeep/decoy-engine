Status: plan

# QA-FK-FANOUT: generated reference columns keep the fitted fan-out (roadmap row 24h)

Owner: Cam. Author: claudeworkerbee (Opus). Date: 2026-11-08. Scope: engine + CLI `fit`, V1 (owner decision
2026-11-08). Revision 1.

Rules consulted: `dev-rules/README.md`, `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`
(PLAN: choke point, Placement, Design notes, Established solutions), `feature-dev.md`, `architecture.md`,
`reuse-first.md`, `testing.md` (test depth, escalation triggers), `cross-cutting-concerns.md`,
`api-and-compatibility.md`, `code-review.md`, `workspace-and-vcs.md`, `scope-discipline.md`,
`verification.md`, `documentation.md`, `catalogs/recurring-defects.md`; the plan contract
(`30_Resources/Decoy-Plan-Contract-and-Defect-Catalog.md`), `LOCAL-BUILDER-BRIEF.md`; the standing rules
develop-correctly-registries-and-sizing and best-practice-becomes-a-sentry; the V1 bar (function break, data
loss or a non-admin value leak blocks; a second copy of a central mechanism, or a new module in a flat folder
over 25, always blocks); engine `CLAUDE.md` (cite the established method; modules aim for 600 lines, hard max
700, census above 600); engine `docs/compatibility-contract.md:116-126` (`distribution-snapshot/v1` is a
contract; new fields are additive and optional); engine `docs/reasons.md` (additions raise the registry minor,
one fixture per minor). Code read at engine `main` `942dae2d` and CLI `main` `de55dab5`.

Source: first-user distribution-integrity QA (2026-11-08, decoy-cli 0.1.0, decoy-engine 0.19.0), High 4:
"Generated FK does not reproduce skew. Source: 8/5/2/1/1/1/1/1 visits per patient. Generated (reference column,
`distribution: weighted`): max 5, 25 of 30 patients have visits. `fit` captures the parent-key counts in the
snapshot, but the reference column does not use them."

Terms: **fan-out** of a key column is how many child rows each parent key has. Its **degree histogram** is
`degree -> number of parents with that many children`, over the parents that have at least one child. **Active
parents** are parents with at least one child.

## 0. Review dispositions

None yet (revision 1).

## 1. FRAME

### Problem

A generated `type: reference` column picks a parent key per child row independently: `random` is `rng.choice`
per row, `weighted` is `rng.choices(k=1)` with user weights, `sequential` cycles (`generation/synthesize.py:617-630`
legacy; `generation/_column_streams.py:191-213` streamed core). Independent uniform draws give a binomial
fan-out: on 30 parents and 60 children, almost every parent gets 1 to 4 children and none gets 8. The source
skew is lost, and so is the share of parents with no children.

The QA note says `fit` already captures the parent-key counts. That holds only by accident: the child's key
column is categorical when it has 30 or fewer distinct values and its type is text, and then `top_values` keeps
the 20 largest counts (`quality/snapshot.py:128-139`, `:464-485`). A numeric key is binned, a key with more
than 30 distinct values is freetext with lengths only, and under QA-FIT-REDACT (row 24f) the same counts sit
beside redacted values. No reliable fan-out reaches generation today, and the snapshot has no relationship or
per-parent block (`quality/snapshot.py:143-227`).

### Guarantee (what V1 promises)

1. `decoy fit visits.csv --fanout patient_id` records the degree histogram of `patient_id` in the snapshot.
   The histogram holds counts only, never a key value.
2. A generated reference column with `distribution: fitted` and that snapshot gives each parent a child count
   drawn from the fitted histogram. When the generated parent and child row counts keep the source ratio, the
   generated degree histogram (zeros included) matches the source within the AT-1 tolerance; when they equal
   the source counts, it matches exactly.
3. Every generated child key is a key of the generated parent table (no orphans), and the column has exactly
   `row_count` rows.
4. Same master key, same label and same snapshot bytes give byte-identical output on every route that accepts
   the column; a different label gives different output.
5. Columns without `distribution: fitted` are byte-identical to today (`random`, `sequential`, `weighted`, the
   min/max repair, the streamed core and the native goldens do not move).
6. A config that asks for `fitted` but cannot get it (no fan-out in the snapshot, a DP snapshot, conflicting
   keys, the pinned API) fails at validate and at run with a typed code that says what to do. It never falls
   back to uniform draws.

### Non-goals (V1)

- Deriving the child table's `row_count` from the parent's. `row_count` stays a static per-table int
  (`config/_tables.py:397-418`); follow-up F2.
- Fitting the share of parents with no children from the parent table. The child file alone cannot see those
  parents; V1 derives the active share from the configured row counts (section 3). Follow-up F3 adds an optional
  parent count.
- Conditional fan-out (children per parent depending on a parent attribute, as SDV HMA1 does). F4.
- The streamed core and the pinned API. `fitted` declines streamed admission (the plan takes the legacy route,
  as any plan with a `statistical` column already does) and is refused by pinned compile. F1, F5.
- DP fan-out. A DP release (`fit_dp_snapshot`) carries no fan-out; `--fanout` with `--epsilon` is a usage error.
- The fidelity report's FK semantics (DI QA M3) and a cardinality score in `report fidelity`. F6.

### Failure modes

| # | Failure | Guard |
|---|---|---|
| F-1 | Child row count does not match the fitted total (`row_count` 60 against a source of 20) | allocation scales to exactly `row_count` with integer largest-remainder; AT-3 property (sum equals `row_count`) |
| F-2 | Empty parents: zero-child share lost or invented | active count `K = round(N * keys / rows)`, clamped; AT-1 shape test counts zeros; AT-2 exact QA repro |
| F-3 | More active parents asked for than exist, or more than child rows | `K = min(P, N, ...)`; AT-3 edge rows |
| F-4 | Parent pool empty, child rows > 0 | today's `[None] * n` behavior kept and stated; AT-3 row |
| F-5 | DP snapshot, or a snapshot fitted without `--fanout` | refusal `plan.reference_fanout_missing` at validate and run; AT-6 |
| F-6 | Hand-edited or truncated fan-out block | decoder validates totals; refusal `plan.reference_fanout_invalid`; AT-6 |
| F-7 | `fitted` combined with `weights` or `min_per_parent`/`max_per_parent` | refusal `plan.reference_fanout_conflict`; AT-6 |
| F-8 | Generation reopens `snapshot_file` from disk (seed derivation reads it, `generators/derivation.py:142-151`) | the fitted branch passes the pinned digest and returns before the existing `for_column` call; AT-5 deletes the file after compile |
| F-9 | `fitted` silently becomes uniform (unknown `distribution` values fall back to random today, `synthesize.py:627-630`) | compile check recognizes `fitted`; the generation branch raises on a missing spec rather than falling through; AT-6, AT-7 |
| F-10 | A redacted snapshot (24f) strips or blanks the fan-out | the fan-out keys are classified value-free in the 24f registry; AT-9 |
| F-11 | Existing reference output moves | AT-8 byte-identity of the non-fitted distributions, existing parity and golden suites |

## 2. Established solutions

- **SDV HMA1** (Patki, Wedge, Veeramachaneni, "The Synthetic Data Vault", IEEE DSAA 2016, section 4): a
  parent row carries the number of its children as a modelled column, and child rows are generated per parent
  for that count. Our V1 is the unconditional form: the child count per parent is drawn from the fitted
  marginal of child counts, not conditioned on parent attributes (F4).
- **SDMetrics CardinalityShapeSimilarity** (SDV docs, multi-table metrics): score = 1 minus the
  Kolmogorov-Smirnov statistic between real and synthetic distributions of children per parent, zeros
  included. AT-1 uses this score with a tolerance. **CardinalityBoundaryAdherence** (share of parents inside
  the real min/max child count) is AT-1's second check.
- **Degree-sequence sampling and the configuration model** (Newman, *Networks*, 2nd ed., 2018, section 12.1;
  for synthetic relational data, arXiv:2312.15187 generates a per-parent degree first and attaches children
  to it): fix each parent's degree, list each parent key `degree` times (the "stubs"), shuffle the list. Every
  child gets a real parent and every parent gets exactly its degree.
- **Stratified inverse-transform sampling** (systematic resampling: Kitagawa, "Monte Carlo filter and
  smoother", JCGS 1996; Carpenter, Clifford, Fearnhead 1999): to draw K values from an empirical CDF, take the
  quantiles at `(i + 0.5) / K`. It has the lowest variance of the standard resampling schemes, and when K
  equals the source parent count it returns the source multiset exactly.
- **Largest-remainder (Hamilton) apportionment** (Balinski and Young, *Fair Representation*, 2001): scale
  integer degrees to a fixed total by flooring the exact shares and giving the leftover units to the largest
  remainders. Integer arithmetic only, so the result is the same on every platform.

`generation/statistical/_fanout.py` cites all five in its docstring and gets a `docs/methodology-registry.yaml`
entry.

## 3. Design notes

### Choke points

There are three, each one place:

1. **Format: `generation/statistical/_fanout.py`.** It holds the one writer (`fanout_stats(series)`) and the one
   reader (`fanout_spec_from_snapshot(...)`) of the fan-out block, so its shape is defined in one file.
   `quality/snapshot.py` calls the writer; the compile check and generation call the reader.
2. **Pinning: `plan/_generation.py::_snapshot_field_keys`.** This is the read-once pass's list of snapshot
   paths (`:85-100`). Its predicate widens from `type == "statistical"` to "statistical, or reference with
   `distribution: fitted`". The bytes are then read, hashed and embedded in the `GenerationPlan` by the existing
   pass (`read_and_pin_snapshots`, `build_generation_plan` `:261-268`). There is no second reader and no reopen.
3. **Generation: `synthesize._reference`** (`generation/synthesize.py:556`). It gains a `fitted` branch placed
   before the existing `GenDeriveContext.for_column` call (`:610-614`). The branch delegates to
   `_fanout.fitted_reference_values`. The streamed core declines `fitted` in `classify_reference`
   (`_admission.py:178`), so the legacy route is the only route that runs it. Pinned execution calls the same
   `_generate_column`, but pinned compile already refuses a `snapshot_file` on a non-statistical column
   (`_pinned_compile.py:162-166`), so it never gets there.

### The fan-out block (fit side)

An optional per-column key, written only for columns named in `fanout_columns`:

```json
"columns": {
  "patient_id": {
    "dtype": "object", "kind": "categorical", "null_count": 0, "...": "...",
    "fanout": {"rows": 20, "keys": 8, "degrees": [[1, 5], [2, 1], [5, 1], [8, 1]]}
  }
}
```

- `rows` is the non-null row count and `keys` the number of distinct non-null keys. `degrees` lists
  `[degree, parents]` pairs in ascending degree, with no zero degrees. Invariants: `sum(parents) == keys` and
  `sum(degree * parents) == rows`.
- Computed as `counts = series.dropna().astype(str).value_counts(); counts.value_counts()`, sorted, ints only.
  Keys are compared as strings, matching how the snapshot already treats keys (`_categorical_stats`). The size
  is at most about `sqrt(2 * rows)` pairs.
- `compute_distribution_snapshot(df, ..., fanout_columns=())` gains the keyword. It is empty by default, so
  output is byte-identical for every existing caller. A bare string, or a name not in `df.columns`, raises
  `DistributionSnapshotError(code=reasons.quality.FANOUT_COLUMN_UNKNOWN)`. Unlike `high_cardinality_columns`,
  an unknown name is not skipped: a skipped name would surface only as a generation refusal later.
- `quality/snapshot.py` imports the writer lazily inside the `if fanout_columns:` branch. `generation` already
  imports `quality` (`_spec.py` reads `DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION`), so a top-level import the other
  way would start an import cycle.

A column-level key, not a top-level block: the fan-out describes one column, any kind (a numeric id gets one
too), and every per-column reader already looks a column up by name.

### Config surface

```yaml
- name: patient_id
  type: reference
  reference_table: patients
  reference_column: patient_id
  distribution: fitted
  snapshot_file: visits.snap.json
  source_column: patient_id      # optional, defaults to name; same key statistical columns use
```

`GenerateColumnConfig` is `extra="allow"` (`config/_tables.py:254`), so these keys pass schema validation with
no edit. The compile check (below) gives them meaning.

### Allocation (pure, integer arithmetic)

Inputs: the decoded `FanoutSpec(rows, keys, degrees, digest)`, the parent pool size `P` (unique non-null keys
of the generated parent column, insertion order, as `_reference` builds it today at `:594-605`), and the child
`row_count` `N`.

```
if N == 0: return []                   # no child rows
if P == 0: return [None] * N           # today's empty-pool behavior (synthesize.py:607-608), unchanged
K = clamp(round_half_even(N * keys / rows), 1, min(P, N))     # active parents
base[i] = Q((2i + 1) / (2K))  for i in 0..K-1                   # stratified quantiles of the degree CDF
target = largest_remainder(base, N)                             # sum == N, every entry >= 1
```

- `Q(q)` is the smallest degree `d` whose cumulative parent share is at least `q`. It is computed with
  integers: `cum_parents(d) * 2K >= (2i + 1) * keys`.
- Largest remainder: `share_i = base_i * N`. The floor is `max(1, share_i // S)` with `S = sum(base)`. Leftover
  units go to the largest `share_i % S` (ties: larger `base_i`, then higher `i`). If the floors of 1 push the
  sum over `N`, the largest entries above 1 are decremented (ties: higher `i`) until the sum is `N`.
- Why `K = N * keys / rows`: it keeps the source's mean children per active parent (`rows / keys`). When the
  user keeps the source ratio of child rows to parent rows (`N / P = rows / P_src`), the active share `K / P`
  equals the source's `keys / P_src`, so the zero-child share comes out right without the parent file.
- QA check: source `{1: 5, 2: 1, 5: 1, 8: 1}`, `rows` 20, `keys` 8.
  - With `P = 10, N = 20`: `K = 8`, the quantiles give `[1, 1, 1, 1, 1, 2, 5, 8]`, the sum is 20, and two
    parents get none. That is the source exactly.
  - With `P = 30, N = 60`: `K = 24`, the quantiles give 15 ones, 3 twos, 3 fives and 3 eights (sum 60).
    The maximum is 8 and 24 of 30 parents have children.

### Assignment and row order (the only random draws)

`fitted_reference_values(pool, spec, n, rng)`:

1. `degrees = allocate(spec, len(pool), n)` (length K, ascending).
2. `slots = degrees + [0] * (P - K)`; `rng.shuffle(slots)`. This assigns degrees to parents in pool order.
3. `stubs = [pool[j] for j, d in enumerate(slots) for _ in range(d)]`; `rng.shuffle(stubs)`. This sets the
   child row order (the configuration model's stub shuffle).

`rng` is `random.Random(GenDeriveContext.for_column(derive_key, col, fallback_seed=seed,
snapshot_content_digest=spec.digest).base_int("py"))`, the same family and sub-key the reference draw site
already uses (`_draw_sites_gen.py` `gen.reference`), with the pinned digest so nothing is reopened. Both
shuffles are CPython's Fisher-Yates over `getrandbits` (the same stability basis as today's `rng.choice`).
Memory is `O(P + N)`, the same order as the legacy route, which already holds every child list. The null mask
is applied afterwards, as for every reference column (`synthesize.py:476-515`): with `null_probability > 0`
each parent loses children at random, which the docs state.

### Compile check

`plan/_generation.py` gains `check_fitted_references(config, pinned, failures)`, called from
`plan/_compile.py` next to `check_statistical_columns` at both sites (`:190`, `:525`). For each generate column
with `type: reference` and `distribution: fitted`:

| Condition | Code |
|---|---|
| `snapshot_file` missing | `plan.reference_fanout_missing` |
| the file failed the read-once pass | the read failure's own code (`plan.statistical_snapshot_unreadable` or `..._schema_mismatch`), raised from `failures`, not reopened |
| `weights`, `min_per_parent` or `max_per_parent` set | `plan.reference_fanout_conflict` |
| source column absent, or its entry has no `fanout` (a snapshot fitted without `--fanout`, every DP release) | `plan.reference_fanout_missing` |
| the `fanout` block breaks a decoder invariant, or `keys == 0` | `plan.reference_fanout_invalid` |

Each message gives the next step. For example, for missing: "reference column 'visits.patient_id': snapshot
'visits.snap.json' has no fan-out for column 'patient_id'. Re-run `decoy fit visits.csv --fanout patient_id`,
or use `distribution: random`. DP snapshots do not carry fan-out."

The check calls `fanout_spec_from_snapshot`, the same decoder generation uses. `plan/_checks.py` (712 lines,
over the hard max, shrink-only) is not touched.

### Generation wiring

- `_plan_entry._prepare_generation` (`:105-125`) builds `reference_fanouts: {(table, column): FanoutSpec}`.
  It reads each fitted reference column of the plan's embedded config, matches its `snapshot_file` against
  `generation.snapshots[*].source_path`, and decodes the already-pinned payload. The result goes in
  `legacy_kwargs` beside `statistical_specs`.
- `synthesize._generate_tables_from_config` and `_generate_column` thread `reference_fanouts` to `_reference`.
- In `_reference`, after the pool is built (`:594-608`) and before the `for_column` call, `distribution ==
  "fitted"` returns `fitted_reference_values(...)`. A missing spec raises `StatisticalSpecError
  (plan.reference_fanout_missing)`: this covers a direct `_generate_tables_from_config` caller that skipped
  compile.
- `_admission.classify_reference` declines `fitted` with the reason string `reference_fitted_fanout` (admission
  reasons are strings, not registry codes, `_admission.py:170-171`). The whole plan then takes the legacy
  route (`_admission.py:10-11`).

### Native and the draw-site inventory

- There is no native generation route. The Rust crate `decoy-engine-native` holds masking kernels only
  (derive, fpe, text_mask, text_redact, batch, canonicalize, arrow_ffi), and no Rust code generates reference
  or FK values.
- The Python native layer only describes reference generation: `_capabilities.py:398-406` (`_GEN_ONLY
  ["reference"]`, not partitionable), `_determinism_protocol.py:104`, `_requirements.py:265-273` ("never
  reaches this function today"), and the oracle `protocol/providers_gen.py:110-141`. A plan with a reference
  column never reaches the native path, before or after this change.
- The one native-side edit is the inventory entry `gen.reference` (`execution/native/_draw_sites_gen.py:58-77`):
  - `call_site` moves to the real line, since `synthesize.py:624` is stale today;
  - "sorted parent pool" becomes "insertion-order parent pool" (stale since v2);
  - `call_shape` and `notes` gain the fitted shape (two `rng.shuffle` calls on the same stream);
  - `config_fingerprint_source` names the pinned snapshot digest.
- `tests/native/test_draw_sites_catalogue_pinned.py` re-pins its digests in the same commit. The coverage guard
  (`tests/native/test_draw_site_inventory_coverage.py`) keeps `GEN_KIND_TO_SITE["reference"]` mapped to one
  site.
- When a native generation route is built, it must take this draw site as specified. That is recorded as F7,
  not built here.

## 4. Behavior

| Input | Today | After |
|---|---|---|
| `decoy fit visits.csv` (no `--fanout`) | snapshot as today | byte-identical |
| `decoy fit visits.csv --fanout patient_id` | n/a (unknown option) | `columns.patient_id.fanout` written; stdout line `fan-out kept for patient_id: 8 keys, up to 8 rows per key`; exit 0 |
| `--fanout nosuch` | n/a | usage error, exit 1, before the snapshot is computed |
| `--fanout patient_id --epsilon 1 ...` | n/a | usage error, exit 1, CSV not read |
| reference `distribution: random`/`sequential`/`weighted`, with or without min/max | uniform or weighted draws, repair | byte-identical |
| reference `distribution: fitted`, `P = 10`, `N = 20`, QA snapshot | n/a (unknown value became random) | degrees `8/5/2/1/1/1/1/1/0/0` exactly; no orphans |
| same, `P = 30`, `N = 60` | random: max about 5, about 25 of 30 active | max 8, 24 of 30 active, histogram scaled from the source |
| `fitted` without `--fanout` in the snapshot, or a DP snapshot | n/a | validate and run fail `plan.reference_fanout_missing` |
| `fitted` with `weights` or `min_per_parent` | n/a | `plan.reference_fanout_conflict` |
| `fitted` through `pinned.compile_pinned` | n/a | refused `pinned_snapshot_file_not_statistical` (existing code) |
| plan with a `fitted` column and only streamed-eligible others | streamed core | legacy route (`classify_plan` reason `reference_fitted_fanout`) |

## 5. Enumerations (from the code at engine `942dae2d`, CLI `de55dab5`)

### 5.1 Routes that produce reference values

| # | Route | Entry | `fitted` after this plan |
|---|---|---|---|
| A | legacy whole-table | `synthesize._reference` `:556-647`, from `_streamed._run_legacy` (`_streamed.py:487`) | runs (the one implementation) |
| B | V1 `ColumnGenerator._generate_reference_column` (`generators/columns.py:414-550`) | no production caller; tests only | untouched; `distribution: fitted` is not a V1 value |
| C | streamed core | `ReferenceStream` (`_column_streams.py:191-213`) via `classify_plan` | declined (`reference_fitted_fanout`), so the plan goes to A |
| D | pinned | `_pinned_compile.py:162-166`, `_pinned_execute.py:183-209` | refused at compile (existing code) |
| E | windowed `generate_rows` | `_window_admission.py:7`, `:483` | reference is Class B, already refused; unchanged |
| F | native / Rust | none (section 3) | not reachable |
| G | probe run | `execution/_probe_scale.py:118-120` scales `row_count` | allocation uses the scaled `P` and `N`; the shape is kept by the same ratio rule |

A normal `decoy run` reaches A or C through `run_generate_and_mask_steps` → `generate_tables`
(`_pipeline_generate_mask.py:118-133`, and the mixed route `_pipeline_mixed_route.py:72-78`). The unified slice
declines reference columns (`physical/_shadow_generation.py:91`, `:326`).

### 5.2 Exit paths

| Path | Code | Change |
|---|---|---|
| CLI `fit` usage errors (D0 block, `fit.py:337-395`) | 1 | `--fanout` with `--epsilon`; `--fanout` naming a missing column (checked after the CSV read, beside `--parse-dates`, `:415-419`) |
| engine `compute_distribution_snapshot` bad `fanout_columns` | `DistributionSnapshotError` | new code `quality.fanout_column_unknown` |
| plan compile (validate, run) | `PlanError` with the codes in section 3 | three new plan codes |
| generation without compile, missing spec | `StatisticalSpecError` | `plan.reference_fanout_missing` |
| success | 0 | new stdout line, JSON key |

### 5.3 FK shapes

- **Simple FK** (`visits.patient_id -> patients.patient_id`): the case above.
- **Two FKs in one child** (`visits.patient_id`, `visits.provider_id`): each column is fitted and allocated on
  its own, with its own draw stream. The joint pattern (which patient sees which provider) is not kept. That is
  the same as today and is recorded as F4.
- **Chain** (`patients -> visits -> claims`): each level is fitted from its own child file; generation order is
  the existing topological sort (`synthesize._topo_sort` `:201`). A middle table's generated key pool feeds
  the next level as today.
- **Composite keys**: a reference column names one parent column (`reference_column`), so composite FKs are not
  expressible today. No change.
- **Self-reference**: rejected as a cycle by `_reference_graph_valid` (`config/_pipeline.py:273-277`). No
  change.
- **Child of a masked parent**: rejected (`config/_pipeline.py:222-227`). No change.

### 5.4 Fields (every key the fan-out adds to a snapshot)

| Scope | Key | Type | Value-free | Reader |
|---|---|---|---|---|
| column | `fanout` | object, optional | yes (container) | `fanout_spec_from_snapshot` |
| fanout | `rows` | int >= 0 | yes (a count) | decoder |
| fanout | `keys` | int >= 0 | yes (a count) | decoder |
| fanout | `degrees` | list of `[int >= 1, int >= 1]`, ascending, unique degrees | yes (counts of counts) | decoder |

Existing readers that receive a column entry and ignore unknown keys: `_spec.load_spec` (reads `kind`, `dtype`,
`stats`), `_sample.sample_column`, `quality/fidelity.py`, and the DP carrier check (DP shape only). AT-4 pins
that each still loads a snapshot that carries `fanout`.

### 5.5 Output surfaces

| Surface | Change |
|---|---|
| snapshot file (`fit --output`) | optional `columns.<c>.fanout` when `--fanout` is given |
| `fit` human stdout | one line per fan-out column (counts only) |
| `fit --json` | `fanout_columns: [...]` |
| `validate` and `run` errors | three new plan codes (section 3) |
| generated child table | the fitted fan-out instead of independent draws, for `fitted` columns only |
| `fit --help`, `docs/cli-reference.md`, engine `docs/strategies.md` (reference section `:1278`) | the option and the `fitted` distribution |

## 6. Class table

| # | Class | Applies here | Structure that closes it | Sentry or property test | Mutation that turns it red |
|---|---|---|---|---|---|
| 1 | Value-bearing output | the fan-out block must hold counts only | writer emits ints from `value_counts().value_counts()`; 24f registry classifies the keys | AT-4 (no key value in the block), AT-9 | M1: write `counts.to_dict()` (key to count) instead (AT-4 red) |
| 2 | Cleanup missing on an exit path | N/A: no files or handles opened beyond the existing read-once pass | N/A | N/A | N/A |
| 3 | State and ordering | the fitted branch must run before `for_column` reads `snapshot_file`; allocation before shuffles | early return in `_reference`; one stream, fixed draw order | AT-5 (file deleted after compile), AT-7 | M3: place the branch after the existing `for_column` call (AT-5 red) |
| 4 | Route parity and silent fallback | `fitted` on the streamed core, or an unknown value falling back to random | streamed decline; compile check; raise on missing spec | AT-6, AT-7 | M4: drop the decline in `classify_reference` (AT-7 red: the streamed core treats `fitted` as random) |
| 5 | Referential integrity | every child key in the parent pool; exact row count | stubs built only from pool entries; largest remainder sums to `N` | AT-2, AT-3 | M5: build stubs from the snapshot's degree list without the pool (AT-3 orphan check red) |
| 6 | Untrusted producer | a hand-edited fan-out block (totals off, zero degrees, negative counts, unsorted) | decoder invariants | AT-6 malformed cells | M6: skip the `sum(degree * parents) == rows` check (AT-6 red) |
| 7 | Check then use on a path (TOCTOU) | the snapshot read at compile and again at generation | pinned bytes only; generation never opens the path | AT-5 | M7: decode from `snapshot_file` at generation (AT-5 red) |
| 8 | Weak tests and overclaimed evidence | a shape test that passes for uniform draws | AT-1 includes a heavy-tailed source on which today's `random` scores below the bar (precondition asserted) | AT-1 | M8: allocate equal degrees (AT-1 red) |
| 9 | Behavior-preserving refactor drift | non-fitted reference output, snapshot bytes without `--fanout` | new branch only; keyword default empty | AT-8, existing parity and golden suites | M9: route `random` through the new helper (AT-8 red) |
| 10 | Frozen contract drift | `distribution-snapshot/v1` gains an optional column key; reason registry gains four codes; draw-site catalogue entry edited | additive key; registry minor plus fixture; catalogue re-pin in the same commit | AT-4, `test_reason_minor_fixtures.py`, `test_draw_sites_catalogue_pinned.py` | M10: rename `degrees` in the writer only (AT-4 round trip red) |

## 7. Acceptance tests (red first)

Engine E1 (`tests/unit/generation/statistical/test_fanout.py`, `tests/unit/quality/test_snapshot_fanout.py`):

- **AT-3** Allocation property (Hypothesis): any valid spec (1 to 40 distinct degrees up to 10,000, parents
  up to 1,000), `P` 0 to 5,000, `N` 0 to 50,000:
  - `len == K <= min(P, N)`;
  - every entry `>= 1`;
  - `sum == N`;
  - deterministic (same inputs give the same list);
  - `K == keys` and the multiset equals the source when `N == rows` and `P >= keys`.

  Fixed rows: `N == 0`; `P == 0` (gives `[None] * N`); `N < K`; `N` far above `rows` (`K == P`); `keys == 1`.
- **AT-4** Snapshot contract:
  - `fanout_columns=("patient_id",)` on the QA visits frame writes `{"rows": 20, "keys": 8, "degrees":
    [[1, 5], [2, 1], [5, 1], [8, 1]]}`;
  - no patient id string appears in `json.dumps(entry["fanout"])`;
  - no keyword gives bytes equal to today's output;
  - a bare string and an unknown name raise `quality.fanout_column_unknown`;
  - `load_spec`, `sample_column`, fidelity `compare` and the compat corpus loader accept a snapshot whose
    columns carry `fanout`;
  - writer then decoder round-trips.

Engine E2 (`tests/unit/generation/test_reference_fanout.py`):

- **AT-1** Shape (CardinalityShapeSimilarity with a tolerance). Source: 1,000 parents whose child counts follow
  a heavy tail (20% zero, a geometric body, 10 parents with 40 or more), fitted with `--fanout`.
  - Generated with `P = 1000`, `N = rows`: the degree histogram with zeros equals the source (score 1.0).
  - Generated with `P = 1500`, `N = 1.5 * rows`: score `1 - KS >= 0.95`, and boundary adherence (share of
    generated parents with a child count inside the source min/max, zeros included) is 1.0.
  - Precondition: the same config with `distribution: random` scores below 0.8.
- **AT-2** QA repro:
  - `P = 10, N = 20`: sorted degrees are `[8, 5, 2, 1, 1, 1, 1, 1, 0, 0]`.
  - `P = 30, N = 60`: max 8, 24 active parents.
  - Every child key is in the parent table (no orphans), and the column has exactly `N` rows.
- **AT-5** Pinned bytes: compile the plan, delete `snapshot_file`, then generate from the plan. Output equals a
  run with the file present, and no `open` of the path happens (`fileaccess` spy). A changed snapshot file
  between compile and run does not change the output.
- **AT-6** Refusals at validate (`compile_plan`) and run, each with its code and next step: no `snapshot_file`;
  unreadable file; column absent; no `fanout` key; a DP-shaped snapshot (no `fanout`); `keys == 0`; totals off;
  a zero degree; unsorted degrees; `weights` set; `min_per_parent` set; `max_per_parent` set. A direct
  `_generate_tables_from_config` call without specs raises `reference_fanout_missing`.
- **AT-7** Routes and determinism:
  - `classify_plan` declines `fitted` with `reference_fitted_fanout`, so the whole plan takes the legacy route.
  - Same master key and label give byte-identical tables twice; a different label differs.
  - `compile_pinned` refuses with `pinned_snapshot_file_not_statistical`.
  - A plan YAML round trip (`plan_from_yaml`) generates the same bytes.
  - A probe run (`_probe_scale`) completes.
- **AT-8** No drift: for `random`, `sequential` and `weighted`, with and without min/max, on the F0
  `cfg_reference` fixture, output is byte-identical to `main`. The existing suites stay green:
  `TestReferenceParityV1`, `test_phase_f_fr0_route_independence.py`, `test_phase_f_f0_invariants.py`,
  `tests/native/test_determinism_goldens.py`.
- **AT-9** With QA-FIT-REDACT merged (run in whichever slice lands second): the fan-out keys are in
  `SNAPSHOT_VALUE_FREE_STATS` and survive `distribution_snapshot_document` on a sensitive key column
  unchanged, and the 24f registry sentry (its AT-10) passes on a frame fitted with `--fanout`.

CLI C1 (`tests/e2e/test_fit_command.py`):

- **AT-C1** `decoy fit visits.csv --fanout patient_id` writes the block, prints the count-only line, and holds
  no key value in stdout. `--json` carries `fanout_columns`.
- **AT-C2** `--fanout nosuch` exits 1 naming the column. `--fanout patient_id --epsilon 1 --delta 1e-6
  --dp-text x` with a nonexistent CSV exits 1 (usage) and does not read the CSV.
- **AT-C3** End to end: fit, then `decoy validate` and `decoy run` on a two-table generate config with
  `distribution: fitted`. The generated `visits` keep the source skew (max degree and active count as in AT-2),
  with no orphans.

## 8. Owner questions

None. Recommended options taken without asking (V1 bar):

- Opt-in `--fanout` rather than fitting every column. Snapshot bytes stay unchanged for existing users, and
  the cost is paid only where it is wanted.
- Derive the zero-child share from the configured row counts rather than read the parent file (F3 if needed).
- Stratified quantiles rather than independent draws of degrees. They are exact at the source scale and have
  lower variance elsewhere.
- Refuse `fitted` on the pinned API and on DP snapshots rather than fall back.
- Keep `null_probability` thinning children at random rather than reserve null rows before allocation.

## 9. Reuse inventory

| Need | Reused | Not reused, why |
|---|---|---|
| Snapshot read, hash, embed | `read_and_pin_snapshots`, `build_generation_plan`, `PinnedSnapshot` | no second reader |
| Snapshot errors and codes | `StatisticalSpecError`, `plan.statistical_snapshot_unreadable` / `_schema_mismatch` | |
| Parent pool | `_reference`'s pool build (`:594-608`) | |
| Seed | `GenDeriveContext.for_column(..., snapshot_content_digest=)` and `base_int("py")` | the existing `gen.reference` family and sub-key |
| Null mask | `_apply_null_probability` | |
| Streamed fallback | `classify_reference` decline → legacy | no new route |
| Pinned refusal | `PINNED_SNAPSHOT_FILE_NOT_STATISTICAL` | no new code |
| Min/max repair | not reused | `fitted` replaces per-parent bounds; combining is refused |
| V1 `ColumnGenerator` reference | not reused | dead in production; sorted pool differs from v2 |

## 10. Placement

| Item | Where | Size | Why there |
|---|---|---|---|
| fan-out writer, `FanoutSpec`, decoder, `allocate_degrees`, `fitted_reference_values` | engine `generation/statistical/_fanout.py` (new) | about 220 | the package that turns fitted snapshots into generated columns; it has 3 modules (4 after); `quality/` already has 25 `.py` files, so a new module there would block |
| `fanout_columns` keyword and lazy writer call | `quality/snapshot.py` | +14 (600 to about 614) | the one snapshot producer; census entry at the exact count with reason "per-column stat of the one producer; `quality/` has no room for a split" |
| pinning predicate, `check_fitted_references` | `plan/_generation.py` | +40 (286 to about 326) | owner of the read-once pass; `plan/_checks.py` is over the hard max and shrink-only, and `plan/` has 31 modules, so no new module |
| two call sites | `plan/_compile.py` | +4 (680 to 684, census bump, under 700) | where `check_statistical_columns` is called |
| `reference_fanouts` decode | `generation/_plan_entry.py` | +18 (306 to about 324) | where the statistical specs are decoded |
| threading and the `fitted` branch | `generation/synthesize.py` | +10 (662 to about 672, census bump, under 700) | the one reference implementation |
| streamed decline | `generation/_admission.py` | +2 | |
| draw-site entry | `execution/native/_draw_sites_gen.py` | about +4 net | the inventory |
| reason codes | `reasons/quality/<part>.py` (+8), `reasons/plan/<part>.py` (+24) | | the files holding the neighbouring codes |
| engine tests | `tests/unit/generation/statistical/test_fanout.py`, `tests/unit/quality/test_snapshot_fanout.py`, `tests/unit/generation/test_reference_fanout.py` | about 220, 120, 320 | |
| CLI option | `cli/fit.py` | +30 (534 to about 564; 24f C1 and C2 add about 65 in other blocks) | edits in place |

## 11. Frozen contracts touched

- **`distribution-snapshot/v1`**: one optional per-column key, `fanout`, written only on request. The contract
  allows additive optional fields (`compatibility-contract.md:124-126`). Old readers ignore it (AT-4). Old files
  have no `fanout` and keep loading; asking for `fitted` on one gives a typed refusal, not a crash.
- **Reason registry**: four additions:
  - `quality.fanout_column_unknown`
  - `plan.reference_fanout_missing`
  - `plan.reference_fanout_invalid`
  - `plan.reference_fanout_conflict`

  They take the next minor after whatever is on `main` when E0 lands (1.10.0 today; 1.11.0 if QA-FIT-REDACT E2
  lands first). The fixture is `tests/fixtures/f29/reason-manifest-v1.NN-fk-fanout.json`, and
  `consumer-contract.json` moves with it. `registry_renumber.py` adjusts both at merge. Escalation trigger:
  E0 runs the full engine suite.
- **Determinism catalogue**: the `gen.reference` entry's text changes, so `test_draw_sites_catalogue_pinned.py`
  re-pins its digests in E2. No draw changes for existing distributions (AT-8).
- **Engine API**: `compute_distribution_snapshot` gains the keyword `fanout_columns`; `distribution: fitted` is
  new config. Nothing removed.
- **QA-FIT-REDACT**: its snapshot registry must classify the four fan-out keys as value-free. If 24f E1 merges
  first, E1 here adds the rows. If this lands first, 24f's AT-10 forces the rows when it lands. AT-9 checks it.
  Its runtime default-deny would otherwise blank an unclassified `fanout` on a sensitive key column, and
  generation would refuse with `reference_fanout_invalid` rather than run wrong.
- **CLI pin**: C1 bumps `decoy-engine` (escalation trigger: dependency lock; full CLI suite).

## 12. Slices

| Slice | Repo | Files (at most 10) | Lines | Tests |
|---|---|---|---|---|
| E0 | engine | `reasons/quality/<part>.py`, `reasons/plan/<part>.py`, `reasons/_registry.py` (version), fixture `reason-manifest-v1.NN-fk-fanout.json`, `tests/fixtures/f29/consumer-contract.json`, `docs/reasons.md`, `tests/unit/test_reason_registry.py` (7) | about 90 | registry tests; full engine suite (registry trigger) |
| E1 | engine | `generation/statistical/_fanout.py`, `quality/snapshot.py`, `tests/sentry/test_module_size.py`, `tests/unit/generation/statistical/test_fanout.py`, `tests/unit/quality/test_snapshot_fanout.py`, `docs/methodology-registry.yaml` (6; plus `profiling/_value_redaction.py` if 24f E1 is on `main`, 7) | about 400 | AT-3, AT-4 (and AT-9 if 24f is on `main`) first; T1 = `tests/unit/quality`, `tests/unit/generation/statistical`, `tests/sentry`, `tests/integration/compat_corpus` |
| E2 | engine | `generation/synthesize.py`, `generation/_plan_entry.py`, `generation/_admission.py`, `plan/_generation.py`, `plan/_compile.py`, `execution/native/_draw_sites_gen.py`, `tests/native/test_draw_sites_catalogue_pinned.py`, `tests/sentry/test_module_size.py`, `tests/unit/generation/test_reference_fanout.py`, `CHANGELOG.md` (10) | about 450 | AT-1, AT-2, AT-5 to AT-8 first; T1 = `tests/unit/generation`, `tests/unit/plan`, `tests/parity`, `tests/native`, `tests/unit/test_v2_generation.py`, `tests/sentry` |
| D | engine | `docs/strategies.md` (reference section), `CODEMAP.md` (2) | about 60 | docs link check |
| R | engine | release request to claudeorchestrator after E0, E1, E2 and D merge | | |
| C1 | CLI | `pyproject.toml`, `uv.lock`, `cli/fit.py`, `tests/e2e/test_fit_command.py`, `docs/cli-reference.md`, `CHANGELOG.md` (6) | about 160 | AT-C1 to AT-C3; full CLI suite (pin bump) |

Order: E0, then E1, then E2, then D, then R, then C1.
- E2 depends on E1 (decoder and allocator) and E0 (codes).
- C1 waits for R.
- QA-FIT-REDACT C1 and C2 edit other blocks of `fit.py` (the snapshot call and the DP tail); whichever lands
  second rebases. When both are in, `--fanout` is passed through `distribution_snapshot_document`, which
  forwards `fanout_columns`. That forwarding is one keyword, added in whichever plan's slice lands second.

## 13. Pre-mortem

- **A user keeps the default `distribution: random` and still sees flat fan-out.** `fitted` is opt-in so that
  existing output does not move. `docs/strategies.md` and the `fit` success line ("use `distribution: fitted`
  on the reference column") point to it. The generate template FK example is CLI-QA1's language pass.
- **The user changes the parent-to-child ratio** (30 patients, 25 visits). `K = round(25 * 8 / 20) = 10`, so
  10 of 30 parents are active, with the source shape scaled to 25 rows. The zero-child share follows the ratio
  the user chose; that is stated in the docs.
- **Large child tables.** The stub list is `O(N)` on the legacy route, which already holds every child list.
  The streamed core declines `fitted`, so memory is the legacy route's, as for any `statistical` column. F1
  admits it with a Fenwick-tree draw-without-replacement once a streaming case appears.
- **A config written for this engine runs on an older engine.** The older engine reads `distribution: fitted`
  as random without a warning (`synthesize.py:627-630`). Every engine at or after E2 refuses unknown use at
  validate. Recorded; the CLI pins its engine.
- **Keys compared as strings.** A child file with `1` and `1.0` for the same key splits it in two. This is the
  same rule the categorical snapshot uses. The parent pool is generated, not matched against source keys, so
  only the degree counts are affected, and only on mixed-format files.
- **The fan-out reveals that one parent has 8 children.** It says nothing about which parent. Small-count
  top-coding of degrees (an SDC threshold on the tail) is F8 if an owner wants it.

## 14. Follow-ups (recorded, not in V1)

- F1: admit `fitted` to the streamed core. The allocation is up front (it needs `P` and `N`, both static);
  child rows stream with draw-without-replacement from a Fenwick tree over the remaining degrees.
- F2: an optional child `row_count` derived from the parent's generated count and the fitted ratio.
- F3: `decoy fit --fanout patient_id --parents patients.csv:patient_id` to record the source parent count, so
  the zero-child share is fitted rather than implied by row counts.
- F4: conditional fan-out (HMA1 proper), and the joint of two FKs in one child.
- F5: `fitted` on the pinned API, pinning the snapshot into the pinned plan as statistical columns do.
- F6: a cardinality-shape line in `report fidelity` for reference columns (DI QA M3).
- F7: when native generation is built, it takes the `gen.reference` draw site as specified here.
- F8: SDC top-coding of rare high degrees, if an owner asks.
