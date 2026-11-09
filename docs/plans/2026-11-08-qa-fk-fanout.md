Status: plan

# QA-FK-FANOUT: generated reference columns keep the fitted fan-out (roadmap row 24h)

Owner: Cam. Author: claudeworkerbee (Opus). Date: 2026-11-08. Scope: engine + CLI `fit`, V1 (owner decision
2026-11-08). Revision 3.

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

Revision 2 folds in Dennis round 1 (GO-WITH-FIXES, 0 blocker, 3 high, 3 medium, 4 low).
Revision 3 folds in Codex round 1 (GO-WITH-FIXES, 3 V1, 2 V2-LOG).

| Finding | Disposition |
|---|---|
| H1 the null mask reopens `snapshot_file` (`synthesize.py:311-320` passes a digest only for pinned statistical columns; `for_column` then reads the file, `derivation.py:145-151`) | Accepted. `_generate_column` also passes `sha256:<spec.digest>` for a fitted reference column. AT-5 sets `null_probability > 0`; mutant M3b |
| H2 a DP-declared config accepts `fitted` against a non-DP snapshot, so the exact source degree histogram reaches DP output | Accepted. `check_dp_generate_contract` (`plan/_checks_dp.py:141`) refuses a fitted reference column when `global_settings.dp` is present, with `plan.dp_generate_fitted_fanout_unsupported`, the same precedent as `condition_on` and HC-5. AT-6b |
| H3 raised size pins: `synthesize.py` 662 to 672, `_compile.py` 680 to 684, `snapshot.py` 600 to 614 | Accepted, all three shrink or hold. `synthesize.py`: new prep slice E1b lands the already planned H-1 Wave B E6a move (`tests/fixtures/h1/definition_owners.json` `"E6a"`: `_derived_generate`, `_formula`, `_reference` to `generation/_synthesize_derived.py`), so the file drops to about 505 and the fitted branch lands in the moved `_reference`. A new `generation/_reference.py`, as suggested, would fail that owners test (a moved name must land in its planned file). `_compile.py`: the two calls switch to one wrapper, net 0 lines. `snapshot.py`: the edit is the keyword plus one delegated call, offset by an equal trim in the same slice so the file stays at 600 |
| M1 plan load is not covered: `_serialize.py:383` repeats the `type != "statistical"` predicate and `_generation_plan_from_dict` (`:430-446`) runs only the statistical checks | Accepted. One predicate `column_pins_snapshot(col)` in `plan/_generation.py`, used by `_snapshot_field_keys` and `_require_statistical_snapshots_pinned`. One wrapper `check_generate_snapshot_columns` (statistical checks, then fitted checks) replaces `check_statistical_columns` at all three call sites (`_compile.py:190`, `:525`, `_serialize.py:439`). AT-7 cell for a manifest whose fitted snapshot was removed |
| M2 `quality` reaches into a private `generation` module | Accepted. `generation/statistical/__init__.py` exports `add_fanout`; the lazy import names the package, not `_fanout`. CODEMAP records the one `quality` to `generation` call and why it is lazy (slice D) |
| M3 the F-audit oracle `tests/parity/_f_audit_matrix.py:89-106` would silently miss the new decline | Accepted. E2b adds a `fitted` probe dimension and reason `reference_fitted_fanout` to the matrix |
| L1 anchors | Fixed: `execution/physical/_shadow_generation.py`; `docs/strategies.md` reference section `:1268` |
| L2 digest prefix and float `K` | Fixed: the digest is passed as `sha256:<hex>`; `K` uses integer half-even rounding via `divmod` |
| L3 "stratified" is the wrong name | Fixed: systematic resampling with the offset fixed at 0.5 (deterministic quantiles) |
| L4 boundary adherence 1.0 may be unreachable; AT-C1 implied a value-free snapshot | Accepted. Allocation caps each degree at the source maximum whenever `N <= K * dmax` (section 3), so AT-1 keeps 1.0; AT-3 asserts the cap. AT-C1 now claims only that the fan-out block and stdout hold no key value; the rest of the snapshot is 24f's |
| Codex V1-1 an empty generated parent pool returned `[None] * N`, contradicting the fitted no-orphan guarantee (`synthesize.py:594-608`) | Accepted. `N == 0` still returns an empty column. For `N > 0`, fitted generation refuses an empty unique non-null parent pool with `plan.reference_parent_pool_empty`; non-fitted distributions keep today's behavior. AT-2 and AT-7 cover both sides |
| Codex V1-2 E2b made exact fitted fan-out runnable before E2a added the DP-declared-config refusal, and plan load did not invoke that refusal (`_plan_entry.py:98-108` checks receipts only when a statistical column exists; `_serialize.py:429-439` goes from config decode to snapshot checks; `_checks_dp.py:141-162` is the DP knob choke point) | Accepted. The DP refusal, its compile regression and a serialized-Plan regression move into E0, before fitted execution exists. `_generation_plan_from_dict` calls the same `check_dp_generate_contract`; no second DP predicate is added. Later slices create no unsafe intermediate head |
| Codex V1-3 the persisted fan-out decoder said `int` but did not require strict stored types, so Python bools could be accepted as counts (`bool` is accepted by ordinary integer checks; the existing stored-spec reconstruction also coerces fields at `_spec.py:125-136`) | Accepted. The decoder uses exact built-in types, refuses bools, floats and numeric strings at every count position, and never echoes a rejected value. AT-6 adds strict-type and value-free-message cells |

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
3. On success, every non-null generated child key is a key of the generated parent table (no orphans), and the
   column has exactly `row_count` rows. A fitted column with child rows but no unique non-null generated parent
   key refuses instead of emitting an all-null pseudo-FK (`generation/synthesize.py:594-608`).
4. Same master key, same label and same snapshot bytes give byte-identical output on every route that accepts
   the column; a different label gives different output.
5. Columns without `distribution: fitted` are byte-identical to today (`random`, `sequential`, `weighted`, the
   min/max repair, the streamed core and the native goldens do not move).
6. A config that asks for `fitted` but cannot get it (no fan-out in the snapshot, a DP snapshot, conflicting
   keys, an empty generated parent pool with child rows, the pinned API) fails with a typed code that says what
   to do. It never falls back to uniform draws or an all-null pseudo-FK.

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
  A config that declares `global_settings.dp` refuses `fitted` even against a non-DP snapshot, because the exact
  source degree histogram would reach DP output unprotected.
- The fidelity report's FK semantics (DI QA M3) and a cardinality score in `report fidelity`. F6.

### Failure modes

| # | Failure | Guard |
|---|---|---|
| F-1 | Child row count does not match the fitted total (`row_count` 60 against a source of 20) | allocation scales to exactly `row_count` with integer largest-remainder; AT-3 property (sum equals `row_count`) |
| F-2 | Empty parents: zero-child share lost or invented | active count `K = round(N * keys / rows)`, clamped; AT-1 shape test counts zeros; AT-2 exact QA repro |
| F-3 | More active parents asked for than exist, or more than child rows | `K = min(P, N, ...)`; AT-3 edge rows |
| F-4 | Parent pool empty, child rows > 0 | fitted generation refuses `plan.reference_parent_pool_empty`; today's `[None] * n` behavior stays byte-identical for non-fitted distributions (`synthesize.py:607-608`); AT-2, AT-7 |
| F-5 | DP snapshot, or a snapshot fitted without `--fanout` | refusal `plan.reference_fanout_missing` at validate and run; AT-6 |
| F-5b | DP-declared config with a plain `--fanout` snapshot | `check_dp_generate_contract` refuses with `plan.dp_generate_fitted_fanout_unsupported`; AT-6b |
| F-6 | Hand-edited or truncated fan-out block | decoder validates totals; refusal `plan.reference_fanout_invalid`; AT-6 |
| F-7 | `fitted` combined with `weights` or `min_per_parent`/`max_per_parent` | refusal `plan.reference_fanout_conflict`; AT-6 |
| F-8 | Generation reopens `snapshot_file` from disk (seed derivation reads it, `generators/derivation.py:142-151`), in the draw or in the null mask | the fitted branch passes the pinned digest and returns before the existing `for_column` call; `_generate_column` passes the same digest to `_apply_null_probability`; AT-5 deletes the file after compile with `null_probability > 0` |
| F-8b | A loaded Plan (`plan_from_yaml`) drops a fitted snapshot and fails late | the shared `column_pins_snapshot` predicate and the `check_generate_snapshot_columns` wrapper run at load; AT-7 |
| F-9 | `fitted` silently becomes uniform (unknown `distribution` values fall back to random today, `synthesize.py:627-630`) | compile check recognizes `fitted`; the generation branch raises on a missing spec rather than falling through; AT-6, AT-7 |
| F-10 | A redacted snapshot (24f) strips or blanks the fan-out | the fan-out keys are classified value-free in the 24f registry; AT-9 |
| F-11 | Existing reference output moves | AT-8 byte-identity of the non-fitted distributions, existing parity and golden suites |
| F-12 | A hand-edited block uses bool, float or numeric-string counts, or an error echoes a rejected data value | decoder requires exact built-in `int` objects at every count position and builds messages from field names and invariant names only; AT-6 strict-type and canary cells |

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
- **Systematic resampling with a fixed offset** (Kitagawa, "Monte Carlo filter and smoother", JCGS 1996;
  Carpenter, Clifford, Fearnhead 1999): to draw K values from an empirical CDF, take the inverse CDF at
  `(i + u) / K`. With the offset `u` fixed at 0.5 it is deterministic (zero variance), and when K equals the
  source parent count it returns the source multiset exactly.
- **Largest-remainder (Hamilton) apportionment** (Balinski and Young, *Fair Representation*, 2001): scale
  integer degrees to a fixed total by flooring the exact shares and giving the leftover units to the largest
  remainders. Integer arithmetic only, so the result is the same on every platform.

`generation/statistical/_fanout.py` cites all five in its docstring and gets a `docs/methodology-registry.yaml`
entry.

## 3. Design notes

### Choke points

There are three, each one place:

1. **Format: `generation/statistical/_fanout.py`.** It holds the one writer (`fanout_stats(series)`, applied to
   a snapshot's columns by `add_fanout`) and the one reader (`fanout_spec_from_snapshot(...)`) of the fan-out
   block, so its shape is defined in one file. `quality/snapshot.py` calls `add_fanout`; the compile check and
   generation call the reader. `generation/statistical/__init__.py` exports both, so no caller imports `_fanout`.
2. **Pinning: one predicate, `plan/_generation.py::column_pins_snapshot(col)`**: true for `type: statistical`,
   and for `type: reference` with `distribution: fitted`. It replaces the `type != "statistical"` test in
   `_snapshot_field_keys` (`:93`, the read-once pass's list of paths) and in
   `_serialize._require_statistical_snapshots_pinned` (`:383`, the load-time pin check). The bytes are then
   read, hashed and embedded in the `GenerationPlan` by the existing pass (`read_and_pin_snapshots`,
   `build_generation_plan` `:261-268`). There is no second reader and no reopen. The checks run through one
   wrapper, `check_generate_snapshot_columns(config, pinned, dp_verified, failures)` in the same module: it
   calls `check_statistical_columns` and then `check_fitted_references`, and replaces the three existing
   `check_statistical_columns` calls (`_compile.py:190`, `:525`, `_serialize.py:439`), so compile and plan load
   run the same checks. It returns `check_statistical_columns`'s column-spec list unchanged for
   `build_generation_plan`, whose current input contract is the four-tuple list at `_generation.py:228-243`.
3. **Generation: `_reference`.** E1b first lands the planned H-1 Wave B E6a move, so `_reference` lives in
   `generation/_synthesize_derived.py` (moved verbatim from `synthesize.py:556-647`; `synthesize` imports it by
   name, so `monkeypatch.setattr(synthesize, "_reference", ...)` in `test_phase_f_f0_invariants.py:345-355`
   still takes effect). E2b gives it a `fitted` branch placed before the existing `GenDeriveContext.for_column`
   call (today `:610-614`). The branch delegates to `_fanout.fitted_reference_values`. The streamed core declines
   `fitted` in `classify_reference` (`_admission.py:178`), so the legacy route is the only route that runs it.
   Pinned execution calls the same `_generate_column`, but pinned compile already refuses a `snapshot_file` on
   a non-statistical column (`_pinned_compile.py:162-166`), so it never gets there. For `N > 0`, the fitted
   branch refuses an empty `ref_vals` before seed derivation; the earlier `N == 0` return remains an empty
   successful column (`synthesize.py:594-610`).

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
   `sum(degree * parents) == rows`. The reader requires a built-in `dict` block, a built-in `list` for
   `degrees`, a two-item built-in `list` for each pair, and `type(value) is int` for `rows`, `keys`, every degree
   and every parent count. Bools, floats, numeric strings, tuples and custom containers are refused rather than
   coerced. Decoder errors name only the field and failed invariant, never the rejected value; this is stricter
   than the coercing stored spec reconstruction at `_spec.py:125-136`.
- Computed as `counts = series.dropna().astype(str).value_counts(); counts.value_counts()`, sorted, ints only.
  Keys are compared as strings, matching how the snapshot already treats keys (`_categorical_stats`). The size
  is at most about `sqrt(2 * rows)` pairs.
- `compute_distribution_snapshot(df, ..., fanout_columns=())` gains the keyword. It is empty by default, so
  output is byte-identical for every existing caller. A bare string, or a name not in `df.columns`, raises
  `DistributionSnapshotError(code=reasons.quality.FANOUT_COLUMN_UNKNOWN)`. Unlike `high_cardinality_columns`,
  an unknown name is not skipped: a skipped name would surface only as a generation refusal later.
- `quality/snapshot.py` imports `add_fanout` from `decoy_engine.generation.statistical` lazily inside the
  `if fanout_columns:` branch, which is the whole edit to that file besides the keyword. `generation` already
  imports `quality` (`_spec.py` reads `DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION`), so a top-level import the other
  way would start an import cycle. This is the first `quality` to `generation` import; CODEMAP records it as a
  lazy, one-call exception (slice D). `snapshot.py` is exactly 600 lines, so E1 trims an equal number of lines
  in the same file (for example a multi-line comment) and the file stays at 600 with no census entry.

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

GEN-STRICT E1 (engine #430) closed the per-type key set in `config/_generate_options.py`, so today a reference
column refuses all three: `distribution: fitted` fails `_distribution` with `config.generate_option_value_unknown`,
and `snapshot_file` and `source_column` are registered only for `statistical`. E2b opens them in the same commit
that wires the fitted branch, never earlier, because before E2b an accepted `fitted` would fall through to random
(F-9). E2b adds `fitted` to the reference distribution choices and `snapshot_file`/`source_column` (both `_S`) to
the `reference` `TypeOptions`, with cells in `tests/unit/config/test_generate_options.py`: a fitted reference config
parses, `fitted` on `windowed_date` still refuses, and the AT-6b DP refusal fires from a config that now parses.
The option-reads sentry compares keys only, so a reader comparing against a new value can drift from the
registry; E2b's AT-7 config cell is the guard for `fitted`, and a value-level sentry is a V2-LOG follow-up.
The compile check (below) gives the keys meaning.

### Allocation (pure, integer arithmetic)

Inputs: the decoded `FanoutSpec(rows, keys, degrees, digest)`, the parent pool size `P` (unique non-null keys
of the generated parent column, insertion order, as `_reference` builds it today at `:594-605`), and the child
`row_count` `N`.

```
if N == 0: return []                   # no child rows
if P == 0: raise reference_parent_pool_empty  # no valid FK value exists (synthesize.py:607-608)
K = clamp(half_even_div(N * keys, rows), 1, min(P, N))       # active parents
base[i] = Q((2i + 1) / (2K))  for i in 0..K-1                   # inverse CDF at fixed offset 0.5
target = largest_remainder(base, N)                             # sum == N, every entry >= 1
target = cap(target, dmax)                                      # no degree above the source max when N <= K * dmax
```

- `half_even_div(a, b)`: `q, r = divmod(a, b)`; add 1 when `2r > b`, or when `2r == b` and `q` is odd. Integer
  only, so `K` is the same on every platform.
- `Q(q)` is the smallest degree `d` whose cumulative parent share is at least `q`. It is computed with
  integers: `cum_parents(d) * 2K >= (2i + 1) * keys`.
- Largest remainder: `share_i = base_i * N`. The floor is `max(1, share_i // S)` with `S = sum(base)`. Leftover
  units go to the largest `share_i % S` (ties: larger `base_i`, then higher `i`). If the floors of 1 push the
   sum over `N`, the largest entries above 1 are decremented (ties: higher `i`) until the sum is `N`. This
   terminates because the vector's minimum possible sum is `K`, and `K <= N`, so at least the excess over `N`
   is removable above the floor of one.
- Cap: `dmax` is the largest source degree. When `N <= K * dmax`, each entry above `dmax` is lowered to `dmax`
  and the removed units go one at a time to the entries below `dmax` with the largest `base_i` (ties: higher
   `i`). The redistribution terminates after exactly the number of removed units: after lowering, total
   headroom is at least that number because `N <= K * dmax`. The sum stays `N`, so no generated parent has more
   children than the largest source parent. When
  `N > K * dmax` (only when `K` was clamped to `P`), the cap cannot hold and the excess stays where largest
  remainder put it; the docs say so.
- Why `K = N * keys / rows`: it keeps the source's mean children per active parent (`rows / keys`). When the
  user keeps the source ratio of child rows to parent rows (`N / P = rows / P_src`), the active share `K / P`
  equals the source's `keys / P_src`, so the zero-child share comes out right without the parent file.
- QA check: source `{1: 5, 2: 1, 5: 1, 8: 1}`, `rows` 20, `keys` 8.
  - With `P = 10, N = 20`: `K = 8`, the quantiles give `[1, 1, 1, 1, 1, 2, 5, 8]`, the sum is 20, and two
    parents get none. That is the source exactly.
  - With `P = 30, N = 60`: `K = 24`, the quantiles give 15 ones, 3 twos, 3 fives and 3 eights (sum 60).
    The maximum is 8 and 24 of 30 parents have children.

### Assignment and row order (the only random draws)

`allocate` returns the length-`K` positive degree vector. `fitted_reference_values(pool, spec, n, rng)` returns
the child column and is the boundary that either returns exactly `n` entries from the pool or raises the empty
pool refusal (`synthesize.py:594-608`):

1. `degrees = allocate(spec, len(pool), n)` (length K, ascending).
2. `slots = degrees + [0] * (P - K)`; `rng.shuffle(slots)`. This assigns degrees to parents in pool order.
3. `stubs = [pool[j] for j, d in enumerate(slots) for _ in range(d)]`; `rng.shuffle(stubs)`. This sets the
   child row order (the configuration model's stub shuffle).

`rng` is `random.Random(GenDeriveContext.for_column(derive_key, col, fallback_seed=seed,
snapshot_content_digest=f"sha256:{spec.digest}").base_int("py"))`, the same family and sub-key the reference
draw site already uses (`_draw_sites_gen.py` `gen.reference`). `PinnedSnapshot.sha256` is bare hex, and
`strategy_config_fingerprint` expects the `sha256:<hex>` form `_generate_column` already builds
(`synthesize.py:313`), so the prefix is added once, in `FanoutSpec.seed_digest`. Both shuffles are CPython's
Fisher-Yates over `getrandbits` (the same stability basis as today's `rng.choice`). Memory is `O(P + N)`, the
same order as the legacy route, which already holds every child list.

The null mask is applied afterwards, as for every reference column (`synthesize.py:476-515`): with
`null_probability > 0` each parent loses children at random, which the docs state. The mask derives its seed
through `for_column` too, and today `_generate_column` passes a digest only for a pinned statistical column
(`synthesize.py:311-320`), so a fitted column would reopen `snapshot_file` there. E2b widens that condition: a
fitted reference column passes `FanoutSpec.seed_digest` from the `reference_fanouts` it already threads.

### Compile check

`plan/_generation.py` gains `check_fitted_references(config, pinned, failures)`, called only from the
`check_generate_snapshot_columns` wrapper (section 3, choke point 2), so compile and plan load both run it. For
each generate column with `type: reference` and `distribution: fitted`:

| Condition | Code |
|---|---|
| `snapshot_file` missing | `plan.reference_fanout_missing` |
| the file failed the read-once pass | the read failure's own code (`plan.statistical_snapshot_unreadable` or `..._schema_mismatch`), raised from `failures`, not reopened |
| `weights`, `min_per_parent` or `max_per_parent` set | `plan.reference_fanout_conflict` |
| source column absent, or its entry has no `fanout` (a snapshot fitted without `--fanout`, every DP release) | `plan.reference_fanout_missing` |
| the `fanout` block breaks a decoder invariant, or `keys == 0` | `plan.reference_fanout_invalid` |

Each message gives the next step and contains structure names only, never a rejected count or other snapshot
data value. For example, for missing: "reference column 'visits.patient_id': snapshot
'visits.snap.json' has no fan-out for column 'patient_id'. Re-run `decoy fit visits.csv --fanout patient_id`,
or use `distribution: random`. DP snapshots do not carry fan-out."

The check calls `fanout_spec_from_snapshot`, the same decoder generation uses. `plan/_checks.py` (712 lines,
over the hard max, shrink-only) is not touched.

DP: `check_dp_generate_contract` (`plan/_checks_dp.py:141-200`) is the one place that refuses anti-DP generate
knobs when `global_settings.dp` is present. Its loop skips non-statistical columns today (`:161`); E0 adds one
clause before that skip that refuses a reference column with `distribution: fitted` with the new code
`plan.dp_generate_fitted_fanout_unsupported` ("a DP pipeline cannot use the exact fitted fan-out; use
`distribution: random`"). The file is 589 lines; the clause is held to about 10 lines so it stays at or under
600. E0 lands the clause before fitted execution exists. It also calls this same function immediately after
the embedded config is decoded in `_generation_plan_from_dict`, before DP verification and snapshot-column
checks (`plan/_serialize.py:429-439`), so a hand-edited serialized Plan cannot bypass the one DP decision point.

### Generation wiring

- `_plan_entry._prepare_generation` (`:105-125`) builds `reference_fanouts: {(table, column): FanoutSpec}`.
  It reads each fitted reference column of the plan's embedded config, matches its `snapshot_file` against
  `generation.snapshots[*].source_path`, and decodes the already-pinned payload. `FanoutSpec.digest` comes from
  that matched `PinnedSnapshot.sha256`, whose shape is bare hex (`plan/_generation.py:61-73`), not from the path
  and not from a new hash or read. The result goes in `legacy_kwargs` beside `statistical_specs`.
- `synthesize._generate_tables_from_config` and `_generate_column` thread `reference_fanouts` to `_reference`,
  and `_generate_column` passes the fitted column's `seed_digest` to `_apply_null_probability` (section 3,
  Assignment).
- In `_reference` (in `_synthesize_derived.py` after E1b), after the pool is built (today `:594-608`) and
  before the `for_column` call, `distribution == "fitted"` returns `fitted_reference_values(...)`. A missing
  spec raises `StatisticalSpecError (plan.reference_fanout_missing)`: this covers a direct
   `_generate_tables_from_config` caller that skipped compile. If `n > 0` and that pool is empty, the same
   boundary raises `StatisticalSpecError (plan.reference_parent_pool_empty)`; non-fitted columns still take the
   existing `[None] * n` return (`synthesize.py:607-608`).
- `_admission.classify_reference` declines `fitted` with the reason string `reference_fitted_fanout` (admission
  reasons are strings, not registry codes, `_admission.py:170-171`). The whole plan then takes the legacy
  route (`_admission.py:10-11`). The F-audit oracle `tests/parity/_f_audit_matrix.py:89-106` re-derives
  `classify_reference` reasons and `test_phase_f_audit_no_legacy.py` claims every dispatch predicate, so E2b
  adds a `fitted` probe dimension to the matrix with that reason.

### Native and the draw-site inventory

- There is no native generation route. The Rust crate `decoy-engine-native` holds masking kernels only
  (derive, fpe, text_mask, text_redact, batch, canonicalize, arrow_ffi), and no Rust code generates reference
  or FK values.
- The Python native layer only describes reference generation: `_capabilities.py:398-406` (`_GEN_ONLY
  ["reference"]`, not partitionable), `_determinism_protocol.py:104`, `_requirements.py:265-273` ("never
  reaches this function today"), and the oracle `protocol/providers_gen.py:110-141`. A plan with a reference
  column never reaches the native path, before or after this change.
- The one native-side edit is the inventory entry `gen.reference` (`execution/native/_draw_sites_gen.py:58-77`):
  - `call_site` moves to the real line in `_synthesize_derived.py`, since `synthesize.py:624` is stale today;
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
| fitted reference, `P = 0`, `N > 0` | all nulls | refused `plan.reference_parent_pool_empty`; non-fitted output is unchanged (`synthesize.py:607-608`) |
| `fitted` without `--fanout` in the snapshot, or a DP snapshot | n/a | validate and run fail `plan.reference_fanout_missing` |
| `fitted` with `weights` or `min_per_parent` | n/a | `plan.reference_fanout_conflict` |
| `fitted` in a config with `global_settings.dp` | n/a | `plan.dp_generate_fitted_fanout_unsupported` at validate and run |
| a saved Plan whose manifest lost the fitted snapshot, loaded with `plan_from_yaml` | n/a | `plan.statistical_snapshot_not_pinned` at load |
| `fitted` through `pinned.compile_pinned` | n/a | refused `pinned_snapshot_file_not_statistical` (existing code) |
| plan with a `fitted` column and only streamed-eligible others | streamed core | legacy route (`classify_plan` reason `reference_fitted_fanout`) |

## 5. Enumerations (from the code at engine `942dae2d`, CLI `de55dab5`)

### 5.1 Routes that produce reference values

| # | Route | Entry | `fitted` after this plan |
|---|---|---|---|
| A | legacy whole-table | `_reference` (`synthesize.py:556-647` today, `_synthesize_derived.py` after E1b), from `_streamed._run_legacy` (`_streamed.py:487`) | runs (the one implementation) |
| B | V1 `ColumnGenerator._generate_reference_column` (`generators/columns.py:414-550`) | no production caller; tests only | untouched; `distribution: fitted` is not a V1 value |
| C | streamed core | `ReferenceStream` (`_column_streams.py:191-213`) via `classify_plan` | declined (`reference_fitted_fanout`), so the plan goes to A |
| D | pinned | `_pinned_compile.py:162-166`, `_pinned_execute.py:183-209` | refused at compile (existing code) |
| E | windowed `generate_rows` | `_window_admission.py:7`, `:483` | reference is Class B, already refused; unchanged |
| F | native / Rust | none (section 3) | not reachable |
| G | probe run | `execution/_probe_scale.py:118-120` scales `row_count` | allocation uses the scaled `P` and `N`; the shape is kept by the same ratio rule |

A normal `decoy run` reaches A or C through `run_generate_and_mask_steps` → `generate_tables`
(`_pipeline_generate_mask.py:118-133`, and the mixed route `_pipeline_mixed_route.py:72-78`). The unified slice
declines reference columns (`execution/physical/_shadow_generation.py:91`, `:326`).

### 5.2 Exit paths

| Path | Code | Change |
|---|---|---|
| CLI `fit` usage errors (D0 block, `fit.py:337-395`) | 1 | `--fanout` with `--epsilon`; `--fanout` naming a missing column (checked after the CSV read, beside `--parse-dates`, `:415-419`) |
| engine `compute_distribution_snapshot` bad `fanout_columns` | `DistributionSnapshotError` | new code `quality.fanout_column_unknown` |
| plan compile (validate, run) | `PlanError` with the codes in section 3 | five new plan codes |
| plan load (`plan_from_yaml`) | `PlanCompileError` | existing `plan.statistical_snapshot_not_pinned` and the fitted checks now cover fitted columns |
| generation without compile, missing spec or empty runtime parent pool | `StatisticalSpecError` | `plan.reference_fanout_missing` or `plan.reference_parent_pool_empty` |
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
| `validate` and `run` errors | five new plan codes (section 3) |
| generated child table | the fitted fan-out instead of independent draws, for `fitted` columns only |
| `fit --help`, `docs/cli-reference.md`, engine `docs/strategies.md` (reference section `:1268`) | the option and the `fitted` distribution |

## 6. Class table

| # | Class | Applies here | Structure that closes it | Sentry or property test | Mutation that turns it red |
|---|---|---|---|---|---|
| 1 | Value-bearing output | the fan-out block must hold counts only | writer emits ints from `value_counts().value_counts()`; 24f registry classifies the keys | AT-4 (no key value in the block), AT-9 | M1: write `counts.to_dict()` (key to count) instead (AT-4 red) |
| 2 | Cleanup missing on an exit path | N/A: no files or handles opened beyond the existing read-once pass | N/A | N/A | N/A |
| 3 | State and ordering | the fitted branch and the null mask must not reach `for_column`'s file read; allocation before shuffles | early return in `_reference`; the digest passed to the null mask; one stream, fixed draw order | AT-5 (file deleted after compile, `null_probability > 0`), AT-7 | M3: place the branch after the existing `for_column` call (AT-5 red); M3b: keep the null-mask digest condition statistical-only (AT-5 red) |
| 4 | Route parity and silent fallback | `fitted` on the streamed core, an unknown value falling back to random, plan load skipping the fitted checks, or a DP config taking the exact fan-out | streamed decline plus the F-audit matrix; compile check; one predicate and one wrapper at compile and load; DP contract clause; raise on missing spec | AT-6, AT-6b, AT-7, `test_phase_f_audit_no_legacy.py` | M4: drop the decline in `classify_reference` (AT-7 red: the streamed core treats `fitted` as random); M4b: call `check_statistical_columns` directly in `_serialize.py` (AT-7 load cell red); M4c: drop the DP clause (AT-6b red) |
| 5 | Referential integrity | every child key in the parent pool; exact row count | stubs built only from pool entries; largest remainder sums to `N` | AT-2, AT-3 | M5: build stubs from the snapshot's degree list without the pool (AT-3 orphan check red) |
| 6 | Untrusted producer | a hand-edited fan-out block (totals off, zero degrees, negative counts, unsorted, bool/float/string counts) | strict exact-type decoder plus invariants; value-free messages | AT-6 malformed, strict-type and canary cells | M6: skip the `sum(degree * parents) == rows` check (AT-6 red); M6b: replace `type(v) is int` with `isinstance(v, int)` (bool cell red) |
| 7 | Check then use on a path (TOCTOU) | the snapshot read at compile and again at generation | pinned bytes only; generation never opens the path | AT-5 | M7: decode from `snapshot_file` at generation (AT-5 red) |
| 8 | Weak tests and overclaimed evidence | a shape test that passes for uniform draws | AT-1 includes a heavy-tailed source on which today's `random` scores below the bar (precondition asserted) | AT-1 | M8: allocate equal degrees (AT-1 red) |
| 9 | Behavior-preserving refactor drift | non-fitted reference output, snapshot bytes without `--fanout`; the E1b move of three functions | new branch only; keyword default empty; E1b moves code verbatim and flips the owners fixture | AT-8, existing parity and golden suites, `tests/unit/test_wave_b_definition_owners.py` | M9: route `random` through the new helper (AT-8 red); M9b: leave `_reference` defined in `synthesize.py` after the flip (owners test red) |
| 10 | Frozen contract drift | `distribution-snapshot/v1` gains an optional column key; reason registry gains five codes; draw-site catalogue entry edited | additive key; registry minor plus fixture; catalogue re-pin in the same commit | AT-4, `test_reason_minor_fixtures.py`, `test_draw_sites_catalogue_pinned.py` | M10: rename `degrees` in the writer only (AT-4 round trip red) |

## 7. Acceptance tests (red first)

Engine E1 (`tests/unit/generation/statistical/test_fanout.py`, `tests/unit/quality/test_snapshot_fanout.py`):

- **AT-3** Allocation property (Hypothesis): any valid spec (1 to 40 distinct degrees up to 10,000, parents
  up to 1,000), `P` 0 to 5,000, `N` 0 to 50,000:
  - `len == K <= min(P, N)`;
  - every entry `>= 1`;
  - `sum == N`;
  - deterministic (same inputs give the same list);
  - `K == keys` and the multiset equals the source when `N == rows` and `P >= keys`;
  - `max <= dmax` whenever `N <= K * dmax`;
  - for `P > 0` and `N > 0`, `K == min(P, N, max(1, round(N * keys / rows)))` under Python's half-even
    `round` of a `Fraction`.

  Fixed rows: `N == 0`; `P == 0, N > 0` (typed refusal at `fitted_reference_values`); `N < source keys`
  (the clamp gives `K <= N`); `N` far above `rows` (`K == P`); `keys == 1`.
- **AT-4** Snapshot contract:
  - `fanout_columns=("patient_id",)` on the QA visits frame writes `{"rows": 20, "keys": 8, "degrees":
    [[1, 5], [2, 1], [5, 1], [8, 1]]}`;
  - no patient id string appears in `json.dumps(entry["fanout"])`;
  - no keyword gives bytes equal to today's output;
  - a bare string and an unknown name raise `quality.fanout_column_unknown`;
  - `load_spec`, `sample_column`, fidelity `compare` and the compat corpus loader accept a snapshot whose
    columns carry `fanout`;
  - writer then decoder round-trips;
  - `quality/snapshot.py` imports nothing from `decoy_engine.generation.statistical._fanout` (AST check: the
    lazy import names the package).

Engine E1b (no new test file): `tests/unit/test_wave_b_definition_owners.py` passes with `E6a` flipped to
`landed: true`; `test_phase_f_f0_invariants.py` (including the `synthesize._reference` monkeypatch at
`:345-355`), `TestReferenceParityV1` and `tests/native/test_determinism_goldens.py` stay green unchanged.

Engine E2b (`tests/unit/generation/test_reference_fanout.py`):

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
  - `P = 0, N > 0` raises `plan.reference_parent_pool_empty`; `P = 0, N = 0` returns an empty column. The same
    empty parent with `random`, `sequential` or `weighted` still returns `[None] * N` as today
    (`synthesize.py:607-608`).
- **AT-5** Pinned bytes: compile the plan with `null_probability: 0.1` on the fitted column, delete
  `snapshot_file`, then generate from the plan. Output (values and null pattern) equals a run with the file
  present, and no `open` of the path happens (`fileaccess` spy). A changed snapshot file between compile and
  run does not change the output.
- **AT-7** Routes and determinism:
  - `classify_plan` declines `fitted` with `reference_fitted_fanout`, so the whole plan takes the legacy route;
    the F-audit matrix carries the `fitted` probe and `test_phase_f_audit_no_legacy.py` passes.
  - Same master key and label give byte-identical tables twice; a different label differs.
  - `compile_pinned` refuses with `pinned_snapshot_file_not_statistical`.
  - A plan YAML round trip (`plan_from_yaml`) generates the same bytes.
  - A probe run (`_probe_scale`) completes.

Engine E0 (`tests/unit/plan/test_fitted_reference_checks.py`):

- **AT-6b** DP: a config with `global_settings.dp` and a fitted reference column is refused with
  `plan.dp_generate_fitted_fanout_unsupported` by compile and config-only checks before any snapshot read. A
  serialized Plan carrying the same embedded config is refused by `plan_from_yaml` before snapshot verification;
  a spy proves both paths called `check_dp_generate_contract`, the one predicate at `_checks_dp.py:141-162`.
  The same configs with `distribution: random` pass this check.

Engine E2a (`tests/unit/plan/test_fitted_reference_checks.py`):

- **AT-6** Refusals at validate (`compile_plan`) and run, each with its code and next step: no `snapshot_file`;
  unreadable file; column absent; no `fanout` key; a DP-shaped snapshot (no `fanout`); `keys == 0`; totals off;
  a zero degree; unsorted degrees; non-dict block, tuple `degrees` or pair, and bool, float and numeric-string
  values in each count position; `weights` set;
  `min_per_parent` set; `max_per_parent` set. For each malformed-value cell, a distinct data-value canary is
  absent from `str(exc)` and `exc.message`; field and column names remain allowed structure. A direct
  `_generate_tables_from_config` call without specs raises `reference_fanout_missing` (this cell lands in E2b).
- **AT-7b** Plan load: a serialized Plan whose manifest has the fitted column's snapshot removed raises
  `plan.statistical_snapshot_not_pinned` from `plan_from_yaml`; a manifest whose embedded fan-out block breaks
  an invariant raises `plan.reference_fanout_invalid` at load. An AST check finds no direct
  `check_statistical_columns` call outside `check_generate_snapshot_columns`.
- **AT-8** No drift: for `random`, `sequential` and `weighted`, with and without min/max, on the F0
  `cfg_reference` fixture, output is byte-identical to `main`. The existing suites stay green:
  `TestReferenceParityV1`, `test_phase_f_fr0_route_independence.py`, `test_phase_f_f0_invariants.py`,
  `tests/native/test_determinism_goldens.py`.
- **AT-9** With QA-FIT-REDACT merged (run in whichever slice lands second): the fan-out keys are in
  `SNAPSHOT_VALUE_FREE_STATS` and survive `distribution_snapshot_document` on a sensitive key column
  unchanged, and the 24f registry sentry (its AT-10) passes on a frame fitted with `--fanout`.

CLI C1 (`tests/e2e/test_fit_command.py`):

- **AT-C1** `decoy fit visits.csv --fanout patient_id` writes the block, prints the count-only line, and
  neither the fan-out block nor stdout holds a key value. `--json` carries `fanout_columns`. (The rest of the
  snapshot, such as a categorical key column's `top_values`, is governed by QA-FIT-REDACT, not this test.)
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
- Fixed-offset systematic quantiles rather than independent draws of degrees. They are exact at the source
  scale and deterministic elsewhere.
- Refuse `fitted` on the pinned API, on DP snapshots and in DP-declared configs rather than fall back.
- Land the planned H-1 Wave B E6a move rather than add a new module, so `synthesize.py` shrinks along the path
  that plan already fixed.
- Keep `null_probability` thinning children at random rather than reserve null rows before allocation.

## 9. Reuse inventory

| Need | Reused | Not reused, why |
|---|---|---|
| Snapshot read, hash, embed | `read_and_pin_snapshots`, `build_generation_plan`, `PinnedSnapshot` | no second reader |
| Snapshot errors and codes | `StatisticalSpecError`, `plan.statistical_snapshot_unreadable` / `_schema_mismatch`, `plan.statistical_snapshot_not_pinned` | |
| Snapshot predicate and checks at compile and load | one `column_pins_snapshot`, one `check_generate_snapshot_columns` wrapper | no copy of the `type` test in `_serialize.py` |
| DP generate refusals | `check_dp_generate_contract` | no DP test in the fitted check |
| Moving `_reference` out of `synthesize.py` | H-1 Wave B E6a, as recorded in `definition_owners.json` | no new module |
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
| fan-out writer, `add_fanout`, `FanoutSpec`, decoder, `allocate_degrees`, `fitted_reference_values` | engine `generation/statistical/_fanout.py` (new), exported by `generation/statistical/__init__.py` | about 240 | the package that turns fitted snapshots into generated columns; it has 3 modules (4 after); `quality/` already has 25 `.py` files, so a new module there would block |
| `fanout_columns` keyword and lazy `add_fanout` call | `quality/snapshot.py` | net 0 (about +5, offset by an equal trim; stays at 600, no census entry) | the one snapshot producer |
| `column_pins_snapshot`, `check_fitted_references`, `check_generate_snapshot_columns` | `plan/_generation.py` | +50 (286 to about 336) | owner of the read-once pass; `plan/_checks.py` is over the hard max and shrink-only, and `plan/` has 31 modules, so no new module |
| two call sites switch to the wrapper | `plan/_compile.py` | net 0 (stays at 680, census unchanged) | where `check_statistical_columns` is called today |
| shared predicate and the wrapper at load | `plan/_serialize.py` | about +2 (557 to about 559) | the load-time pin check |
| DP refusal clause | `plan/_checks_dp.py` | about +10 (589 to at most 600) | the one DP generate contract |
| E6a move of `_derived_generate`, `_formula`, `_reference` | `generation/_synthesize_derived.py` (new, about 175) from `generation/synthesize.py` (662 to about 505; its census entry is removed) | | the file H-1 Wave B already assigned; `generation/` has 22 direct modules (23 after) |
| threading, null-mask digest, the `fitted` branch | `generation/synthesize.py` (about +8, still under 600), `generation/_synthesize_derived.py` (about +8) | | the one reference implementation |
| `reference_fanouts` decode | `generation/_plan_entry.py` | +18 (306 to about 324) | where the statistical specs are decoded |
| streamed decline | `generation/_admission.py` | +2 | |
| draw-site entry | `execution/native/_draw_sites_gen.py` | about +4 net | the inventory |
| F-audit probe | `tests/parity/_f_audit_matrix.py` | about +6 | the oracle that re-derives admission reasons |
| reason codes | `reasons/quality/<part>.py` (+8), `reasons/plan/<part>.py` (+32; `DP_GENERATE_FITTED_FANOUT_UNSUPPORTED` beside the other DP codes in `_part2.py`) | | the files holding the neighbouring codes |
| engine tests | `tests/unit/generation/statistical/test_fanout.py`, `tests/unit/quality/test_snapshot_fanout.py`, `tests/unit/generation/test_reference_fanout.py`, `tests/unit/plan/test_fitted_reference_checks.py` | about 220, 120, 260, 160 | |
| CLI option | `cli/fit.py` | +30 (534 to about 564; 24f C1 and C2 add about 65 in other blocks) | edits in place |

## 11. Frozen contracts touched

- **`distribution-snapshot/v1`**: one optional per-column key, `fanout`, written only on request. The contract
  allows additive optional fields (`compatibility-contract.md:124-126`). Old readers ignore it (AT-4). Old files
  have no `fanout` and keep loading; asking for `fitted` on one gives a typed refusal, not a crash.
- **Reason registry**: six additions:
  - `quality.fanout_column_unknown`
  - `plan.reference_fanout_missing`
  - `plan.reference_fanout_invalid`
  - `plan.reference_fanout_conflict`
  - `plan.reference_parent_pool_empty`
  - `plan.dp_generate_fitted_fanout_unsupported`

  They take the next minor after whatever is on `main` when E0 lands (1.10.0 today; 1.11.0 if QA-FIT-REDACT E2
  lands first). The fixture is `tests/fixtures/f29/reason-manifest-v1.NN-fk-fanout.json`, and
  `consumer-contract.json` moves with it. `registry_renumber.py` adjusts both at merge. Escalation trigger:
  E0 runs the full engine suite.
- **Determinism catalogue**: the `gen.reference` entry's text changes, so `test_draw_sites_catalogue_pinned.py`
  re-pins its digests in E2b. No draw changes for existing distributions (AT-8).
- **H-1 definition owners**: E1b flips `E6a` to `landed: true` in `tests/fixtures/h1/definition_owners.json`
  and moves exactly the three names that entry lists. The parked Wave B plan (platform `plan/h1-wave-b`) then
  has one fewer slice to land; claudeorchestrator is told in the result.
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
| E0 | engine | `reasons/quality/<part>.py`, `reasons/plan/<part>.py`, `reasons/_registry.py` (version), fixture `reason-manifest-v1.NN-fk-fanout.json`, `tests/fixtures/f29/consumer-contract.json`, `docs/reasons.md`, `plan/_checks_dp.py`, `plan/_serialize.py`, `tests/unit/plan/test_fitted_reference_checks.py`, `tests/unit/test_reason_registry.py` (10) | about 150 | registry tests, AT-6b compile and serialized-Plan cells; full engine suite (registry trigger) |
| E1 | engine | `generation/statistical/_fanout.py`, `quality/snapshot.py`, `tests/sentry/test_module_size.py`, `tests/unit/generation/statistical/test_fanout.py`, `tests/unit/quality/test_snapshot_fanout.py`, `docs/methodology-registry.yaml` (6; plus `profiling/_value_redaction.py` if 24f E1 is on `main`, 7) | about 400 | AT-3, AT-4 (and AT-9 if 24f is on `main`) first; T1 = `tests/unit/quality`, `tests/unit/generation/statistical`, `tests/sentry`, `tests/integration/compat_corpus` |
| E1b | engine | `generation/synthesize.py`, `generation/_synthesize_derived.py` (new), `tests/fixtures/h1/definition_owners.json`, `tests/sentry/test_module_size.py` (4) | about 180 moved, about 10 new | behavior-preserving move, no new test; T1 = `tests/unit/test_wave_b_definition_owners.py`, `tests/parity`, `tests/native/test_determinism_goldens.py`, `tests/unit/test_v2_generation.py`, `tests/sentry` |
| E2b | engine | `generation/synthesize.py`, `generation/_synthesize_derived.py`, `generation/_plan_entry.py`, `generation/_admission.py`, `plan/_generation.py` (`column_pins_snapshot` only), `execution/native/_draw_sites_gen.py`, `tests/native/test_draw_sites_catalogue_pinned.py`, `tests/parity/_f_audit_matrix.py`, `tests/unit/generation/test_reference_fanout.py`, `config/_generate_options.py`, `tests/unit/config/test_generate_options.py`, `CHANGELOG.md` (12) | about 400 | AT-1, AT-2, AT-5, AT-6b regression, AT-7, AT-8 first; T1 = `tests/unit/generation`, `tests/unit/plan`, `tests/parity`, `tests/native`, `tests/unit/test_v2_generation.py`, `tests/sentry` |
| E2a | engine | `plan/_generation.py`, `plan/_compile.py`, `plan/_serialize.py`, `tests/unit/plan/test_fitted_reference_checks.py`, `CHANGELOG.md` (5) | about 250 | AT-6, AT-7b first; AT-6b stays as a regression; T1 = `tests/unit/plan`, `tests/unit/generation/test_reference_fanout.py`, `tests/sentry` |
| D | engine | `docs/strategies.md` (reference section), `CODEMAP.md` (the fan-out module and the lazy `quality` to `generation` call) (2) | about 60 | docs link check |
| R | engine | release request to claudeorchestrator after E0, E1, E1b, E2b, E2a and D merge | | |
| C1 | CLI | `pyproject.toml`, `uv.lock`, `cli/fit.py`, `tests/e2e/test_fit_command.py`, `docs/cli-reference.md`, `CHANGELOG.md` (6) | about 160 | AT-C1 to AT-C3; full CLI suite (pin bump) |

Order: E0, then E1, then E1b, then E2b, then E2a, then D, then R, then C1.
- E1b is independent of the fan-out code and may land any time before E2b.
- E2b depends on E1 (decoder and allocator), E1b (the moved `_reference`) and E0 (codes). It goes before E2a so
  that `main` never compiles a `fitted` column that generation would treat as random: with E2b alone, a fitted
  column with no usable spec raises `plan.reference_fanout_missing` at run (fail closed), an empty parent pool
  raises `plan.reference_parent_pool_empty`, and E0 already made both compile and serialized-Plan load call
  `check_dp_generate_contract` (`_checks_dp.py:141-162`; `_serialize.py:429-439`). E2a then adds the remaining
  fitted snapshot refusals and the fitted snapshot load path.
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
  as random without a warning (`synthesize.py:627-630`). Every engine at or after E2a refuses unknown use at
  validate. Recorded; the CLI pins its engine.
- **Keys compared as strings.** A child file with `1` and `1.0` for the same key splits it in two. This is the
  same rule the categorical snapshot uses. The parent pool is generated, not matched against source keys, so
  only the degree counts are affected, and only on mixed-format files.
- **The fan-out reveals that one parent has 8 children.** It says nothing about which parent. Small-count
  top-coding of degrees (an SDC threshold on the tail) is F8 if an owner wants it. Under a DP-declared config
  the exact histogram is refused outright (AT-6b).
- **The E6a move collides with a later Wave B slice.** The owners fixture records the target file, so whoever
  resumes Wave B finds E6a landed and skips it; no other slice moves these three names.

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
- V2-LOG: the existing statistical compile wrapper preserves a `StatisticalSpecError` as `__cause__` when it
  raises `PlanCompileError` (`plan/_checks.py:384-389`). That chain is server-admin traceback material under the
  standing bar. Do not widen this V1 slice to change the established statistical error path.
- V2-LOG: the existing statistical check imports `StatisticalSpecError` from the private `_spec` module even
  though the package exports it (`plan/_checks.py:335-337`; `generation/statistical/__init__.py:12-18`). The
  fitted code uses the public package API; clean the old import in the V2 pass.
