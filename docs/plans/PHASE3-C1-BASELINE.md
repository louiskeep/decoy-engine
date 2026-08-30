Status: plan

# Phase 3 C1 baseline (Task 3.0): deterministic-faker pool_quality + JC-3 freeze

This is the Task 3.0 freeze for the deterministic C1 masking slice
(`docs/plans/2026-08-30-part1-phase3-c1-slice.md`). It fixes the
`pool_quality` metric definition, its per-tier threshold formula, and the
bounded-aggregation method BEFORE any oracle number is observed (BLOCKER 2 /
JC-2), and freezes the JC-3 performance-gate thresholds Task 3.6 will check
Task 3.1's native route against. The frozen recipe variant, the bench
harness, and the measured numbers land in later commits on this same
document, in that order, so the git history itself shows the thresholds
predate the numbers.

## Environment

- Host: single-org devbox, 12 GiB RAM, 2 GiB swap (the same box Phase 2's
  baseline was captured on; see `docs/plans/PHASE2-BASELINE.md`).
- Engine at `feat/native-phase3` off `feat/native-phase2-task2.7`.
- Harness (committed for reproducibility, next commit on this doc):
  `scripts/native-baseline/bench_c1_oracle.py`.

## Frozen deterministic C1 recipe variant

Same two-table shape as the platform's `mask-fullframe-saturate` scenario
(`decoy-platform/.claude/worktrees/streaming-qual/scripts/prod-sim/prodsim_run/scenarios/mask-fullframe-saturate/recipe.yaml`):
patients masked independently of observations, no `relationships:`. The
three faker columns each add `deterministic: true` + an explicit `namespace`
+ an explicit `pool_size` (JC-5); every hash column is unchanged from the
platform recipe. This is the ONLY change from that recipe -- the point of
JC-5 is that the deterministic variant is the same job, not a different one.

```yaml
version: 1
global_settings:
  seed: 20260830
sources:
  patients:
    type: file
    format: csv
    path: placeholder-unused.csv
  observations:
    type: file
    format: csv
    path: placeholder-unused.csv
tables:
  - name: patients
    columns:
      - name: FIRST
        strategy: faker
        provider: person_first_name
        deterministic: true
        namespace: first_name_identity
        pool_size: 10000
      - name: LAST
        strategy: faker
        provider: person_last_name
        deterministic: true
        namespace: last_name_identity
        pool_size: 10000
      - name: MAIDEN
        strategy: faker
        provider: person_last_name
        deterministic: true
        namespace: maiden_name_identity
        pool_size: 10000
      - name: SSN
        strategy: hash
        namespace: ssn_identity
      - name: DRIVERS
        strategy: hash
        namespace: drivers_identity
      - name: PASSPORT
        strategy: hash
        namespace: passport_identity
      - name: ADDRESS
        strategy: hash
        namespace: address_identity
      - name: BIRTHDATE
        strategy: hash
        namespace: birthdate_identity
      - name: DEATHDATE
        strategy: hash
        namespace: deathdate_identity
  - name: observations
    columns:
      - name: DATE
        strategy: hash
        namespace: observation_date_identity
      - name: VALUE
        strategy: hash
        namespace: observation_value_identity
targets:
  patients:
    type: file
    format: csv
    path: mask-fullframe-saturate-patients.csv
  observations:
    type: file
    format: csv
    path: mask-fullframe-saturate-observations.csv
relationships: []
namespaces: {}
```

FIRST and LAST get distinct namespaces from MAIDEN even though LAST and
MAIDEN share a provider (`person_last_name`): a namespace scopes the
POOL (`PoolBuilder`'s pool-seed derivation folds in `namespace`, so two
columns on the same provider with different namespaces build two different
pools), and LAST/MAIDEN are semantically different fields on the same
person, so this baseline gives each its own pool rather than forcing them
to share one. This mirrors the platform recipe's own convention of giving
every hash column its own namespace.

### Resolved sampler settings (per faker column)

Read off `execution/_strategies/_faker.py` and `plan/_seed_envelope.py` for
this exact config:

| Column | Provider | deterministic | cardinality_mode | pool_size | scale | locale | namespace |
|---|---|---|---|---|---|---|---|
| FIRST | person_first_name | true | reuse (default; not set) | 10,000 | 2.0 (default; unused under reuse) | None -> pool identity label `"default"`; Faker adapter resolves to `en_US` | first_name_identity |
| LAST | person_last_name | true | reuse (default; not set) | 10,000 | 2.0 (default; unused under reuse) | None -> pool identity label `"default"`; Faker adapter resolves to `en_US` | last_name_identity |
| MAIDEN | person_last_name | true | reuse (default; not set) | 10,000 | 2.0 (default; unused under reuse) | None -> pool identity label `"default"`; Faker adapter resolves to `en_US` | maiden_name_identity |

The two "default" labels above are NOT the same thing: `PoolBuilder`'s own
`effective_locale = locale or "default"` is a label folded into the pool's
identity/pool-seed derivation, while the Faker adapter separately resolves
an unset `spec.locale` to its own default of `"en_US"` for the actual
values it generates. Both are pre-existing engine behavior, not something
this baseline introduces; recorded here so Task 3.1 does not need to
rediscover it.

Neither `cardinality_mode` nor `scale` is set in the recipe; both take
their compiled defaults (`reuse`, `2.0`). `scale` is read only under
`scale_source_cardinality`, so it is inert here.

## Frozen dataset tiers

Two tiers, both over the SAME two-table shape (patients + observations; see
the recipe variant below): a small parity tier for fast, cheap checks, and a
memory tier sized to actually exercise full-frame residency without risking
the box.

- **Parity tier: 10,000 rows** (both tables; see the tier-design note below
  for why both tables share one row count).
- **Memory tier: 3,000,000 rows.**

### Tier-design note: why patients and observations share one row count

Decision 5's C1 shape does not mandate a patients:observations row-count
ratio; the two tables are independently masked, no `relationships:`. Giving
both tables the tier's row count is a deliberate simplification: it keeps
"tier" a single number, and it still stresses the hypothesis this baseline
exists to test (full-frame residency of both tables at once, plus the
per-faker-column sampler temporaries), without adding a second tunable
dimension this baseline does not need.

### Tier-size calibration (SAFETY: the box is 12 GiB and recently had an OOM
runaway; this run must never approach it)

The C1 full-frame oracle holds the whole table in memory, and the master
plan's working diagnosis puts C1's blowup around 12.7x the data size before
any staged evidence existed. Before committing to a memory-tier size, two
uninstrumented calibration points were run at 100,000 and 500,000 rows
(single rep, `stage=publication`, the full end-to-end run) to fit a peak-RSS
slope:

| Rows | Total peak RSS |
|---|---|
| 100,000 | 401 MB |
| 500,000 | 1,080 MB |

Linear fit: `~1,697 MB per 1M rows + 231 MB` fixed overhead. Projected peak
at candidate tiers:

| Candidate rows | Projected peak |
|---|---|
| 3,000,000 | ~5,323 MB (~5.2 GiB) |
| 4,000,000 | ~6,921 MB (~6.8 GiB) |

3,000,000 rows was chosen as the memory tier: its projected peak (~5.2 GiB)
leaves roughly 1.6 GiB of margin under the harness's own hard abort ceiling
(`ABORT_RSS_KB = 7,000,000` kB, ~6.8 GiB) and roughly 2.9 GiB under the 8 GiB
policy ceiling below, so ordinary run-to-run variance cannot push it into
either. 4,000,000 rows was rejected as the memory tier for exactly the
opposite reason: its projection sits inside the abort ceiling's own margin,
which risks the harness killing a real run rather than measuring it.

The harness's `_run_fresh` polls every child's VmHWM externally and kills it
the instant it crosses `ABORT_RSS_KB`, independent of this calibration --
the calibration picks a tier expected to stay well clear of that kill, it is
not the only thing preventing an OOM.

## Frozen `pool_quality` metric (Step 4 -- frozen BEFORE the Step 5 oracle run)

Population: the pooled OUTPUT values over non-null source rows of an
admitted deterministic faker column (FIRST, LAST, MAIDEN in the patients
table; the only faker columns in C1's scope).

**Distinct-source collision count.** For a faker column, group
non-null (source, masked) pairs by source. Two distinct source values
landing on the same masked output are a collision; a source value that
recurs and always maps to the SAME output is intentional deterministic
reuse, not a collision, and never enters the numerator.

```
collision_count = |distinct_sources| - |distinct_outputs_for_distinct_sources|
collision_rate  = collision_count / |distinct_sources|
```

Empty population (`|distinct_sources| == 0`, e.g. an all-null column) is
defined as rate 0, pass -- never silently omitted from the report.

**Pool-duplicate count.** For the same column's built pool:

```
pool_duplicate_count = pool_size - |distinct_pool_values|
pool_duplicate_rate  = pool_duplicate_count / pool_size
```

**UNIQUE-feasibility.** Applies only to a UNIQUE-cardinality admitted
column. C1's scope is `reuse` only (JC-5), so this check is N/A for every
column here, recorded explicitly as `"N/A (reuse-only C1 scope)"` rather
than silently passed.

**Per-tier threshold.** Collision rate rises with distinct-source count
relative to pool size, so a single threshold across tiers would either be
vacuous at one tier or impossible at another. Each gated tier (parity,
memory) gets its OWN threshold, per faker column, per metric:

```
threshold(tier, column, metric) = oracle_observed_rate(tier, column, metric) + m
m = 0.02   # fixed absolute margin; the one numeric knob this freeze commits to
```

Step 6 (next commit, after the oracle run) asserts the oracle itself passes
its own per-tier threshold at every tier and every column -- by
construction, since the threshold IS the oracle's observed rate plus a
non-negative margin, this can never fail unless a threshold were computed
against the wrong tier/column, which the assertion also catches.

**Bounded aggregation.** The distinct-source collision measurement runs
over a population that scales with row count, so it MUST NOT hold an
`O(distinct sources)` Python-side structure (a plain `set()` would). The
frozen method: write (source, masked) pairs to Parquet, then run a DuckDB
`GROUP BY source` under an explicit `memory_limit` and `temp_directory`
pragma, so the aggregation spills to disk rather than growing RAM with the
tier:

```sql
WITH per_source AS (
    SELECT source, ANY_VALUE(masked) AS out_val,
           COUNT(DISTINCT masked) AS n_distinct_masked
    FROM read_parquet('pairs_<COLUMN>.parquet')
    WHERE source IS NOT NULL
    GROUP BY source
)
SELECT COUNT(*) AS distinct_sources,
       COUNT(DISTINCT out_val) AS distinct_outputs_for_distinct_sources,
       SUM(CASE WHEN n_distinct_masked > 1 THEN 1 ELSE 0 END) AS non_deterministic_sources
FROM per_source
```

`non_deterministic_sources` is a quality-control check on the measurement
itself (a nonzero value would mean the same source mapped to more than one
output, which the deterministic sampler must never do), not part of the
`pool_quality` metric.

The pool-duplicate count needs no such bound: `pool_size` is a small,
explicit config knob (10,000 in this recipe), never a function of row
count, so a direct rebuild-and-count over the pool's own values is already
`O(pool_size)`.

This aggregation runs OUT OF BAND, in a process separate from (and after)
the measured `publication`-stage process that wrote the pair files, so its
own memory use is never attributed to the masking route being measured.

**Fixed seed.** `FIXED_SEED = 20260830`; `FIXED_MASK_KEY` is a fixed 32-byte
value. Both are frozen in the harness for reproducibility of this exact
measurement.

## Frozen JC-3 performance gate (Step 7)

Executable thresholds Task 3.6 checks Task 3.1's native route against, using
this baseline's oracle numbers as the comparison point once Step 5 fills
them in.

- **Tier sizes**: parity 10,000 rows; memory 3,000,000 rows (shared with
  `pool_quality` above).
- **Chunk size**: 50,000 rows (`chunk_size_rows`, matching the Phase 2
  convention `--batch-rows 50000`; Task 3.1's native/streaming route reads
  this, not the oracle baseline itself, which runs unchunked full-frame by
  pin).
- **Warmup**: 1 discarded rep per tier before timed reps (Phase 2
  convention).
- **Repetition count**: 5 timed reps at the parity tier; 3 timed reps at the
  memory tier (reduced for the large tier's wall-clock cost; still >= 2, so
  median + IQR are both defined).
- **Reported statistic**: median wall time; **variance policy**: IQR
  (`Q3 - Q1` over the timed reps).
- **Absolute peak-RSS ceiling**: 8,192 MB, HARD. Policy-fixed from the
  SAFETY constraint (a 12 GiB box that recently had an OOM runaway), not
  derived from any measurement -- independent of what Step 5 observes.
- **Flatness bound** (applies to Task 3.1's native route, not this oracle --
  the oracle's own full-frame RSS is EXPECTED to scale with rows; that
  scaling is exactly the problem the native route exists to remove): native
  peak RSS at the memory tier <= 1.5x its value at the parity tier (mirrors
  the Phase 2 Task 2.0 precedent ratio). HARD.
- **Wall non-regression ratio**: native wall <= 1.25x the oracle's median
  wall, at every tier. HARD, not a warning (per the plan's JC-3 framing).

## Step 6 (after the oracle run): oracle self-check

Recorded in the "measured results" commit: assert the pinned oracle passes
its OWN per-tier `pool_quality` threshold, at every tier and every faker
column. This is a sanity check on the threshold formula, not on the oracle
-- since the threshold is defined as the oracle's own observed rate plus a
non-negative margin, a failure here would mean the threshold was computed
against the wrong tier or column, not that the oracle exceeded its own
bar.

## Measured results (Step 5) + oracle self-check (Step 6)

Pinned oracle (`substrate="pandas"`, `execution_mode="full_frame"`,
`auto_chunk=False`), fixed seed 20260830, fixed 32-byte mask key. RSS is
external `VmHWM`, staged via fresh-process prefix runs (each stage's delta is
the difference of consecutive prefix high-water marks). Raw:
`/tmp/c1_final_parity.json`, `/tmp/c1_final_memory.json`.

### Parity tier (10,000 rows, 5 reps + 1 warmup)

- Wall: median 2.944 s, IQR 0.015 s.
- Throughput: hash 87,501 rows/s/col; faker (selection) 15,031 rows/s/col.
- Staged peak RSS (KB/1000 = MB): input_load 117.9, +pool_build 80.0,
  +selection 5.6, +publication 20.2; total 223.7 MB.

### Memory tier (3,000,000 rows, 3 reps + 1 warmup)

- Wall: median 398.79 s, IQR 15.21 s.
- Throughput: hash 81,330 rows/s/col; faker (selection) 91,565 rows/s/col.
- Staged peak RSS (KB/1000 = MB): input_load 689.9, +pool_build 82.5,
  +selection 1,313.7, +publication 3,668.4; total 5,754.5 MB (5.75 GiB),
  peak observed across reps ~5.75 GiB (well under the 6.8 GiB harness abort
  and 8 GiB policy ceilings; matches the ~5.2 GiB calibration projection plus
  run variance).

The staged attribution is the evidence for the plan's corrected rationale:
the oracle's memory is NOT the pool build (a flat +82 MB at both tiers, the
pool is bounded), it is the full-frame residency plus the whole-column
sampler temporaries. At 3M rows the selection stage alone adds +1.31 GiB
(the sampler's `source.tolist()` / output list / sampled Series) and
publication adds +3.67 GiB (the full masked output held before write). Both
scale with row count; that scaling is exactly what the Task 3.1 native route
removes by streaming in 50,000-row chunks.

### pool_quality (both tiers)

The synthetic source generator caps distinct source values per faker column
(1,000 FIRST, 1,200 LAST, 360 MAIDEN) independent of row count, so the
collision and pool-duplicate rates are IDENTICAL at both tiers. The per-tier
threshold mechanism still applies; it just happens the two tiers coincide
here.

| Column | distinct_sources | collision_rate | pool_duplicate_rate | non_deterministic_sources |
|---|---|---|---|---|
| FIRST | 1,000 | 0.6430 | 0.9338 | 0 |
| LAST | 1,200 | 0.5617 | 0.9013 | 0 |
| MAIDEN | 360 | 0.2944 | 0.9017 | 0 |

The rates are high because these providers have a small output vocabulary
(`person_first_name` yields ~662 distinct values in a 10,000-slot pool, so
distinct sources necessarily share fake names). This is inherent to masking a
high-cardinality column with a low-cardinality fake vocabulary, not a defect:
`pool_quality` gates the native route at the oracle's OWN rate plus a margin,
so the native route must REPRODUCE this behavior, not achieve low collisions.
`non_deterministic_sources` is 0 for every column at both tiers: the
deterministic sampler never maps one source value to two outputs.

### Frozen per-tier thresholds (oracle rate + m, m = 0.02)

Same at both tiers (rates coincide, above). Task 3.6 checks Task 3.1's native
route against these:

| Column | collision_rate <= | pool_duplicate_rate <= |
|---|---|---|
| FIRST | 0.6630 | 0.9538 |
| LAST | 0.5817 | 0.9213 |
| MAIDEN | 0.3144 | 0.9217 |

UNIQUE-feasibility is N/A (reuse-only C1 scope) for every column, recorded
explicitly.

### Step 6: oracle self-check (PASS)

The pinned oracle passes its own per-tier `pool_quality` threshold at every
tier and every faker column, by construction: each threshold is the oracle's
observed rate plus a non-negative margin, so `observed <= observed + 0.02`
holds trivially. The measurement QC (`non_deterministic_sources == 0`) also
passes for all three columns at both tiers, confirming the deterministic
selection is single-valued per source. No threshold was computed against the
wrong tier or column.

### JC-3 baseline anchor (for Task 3.6)

The native route's wall non-regression bound is `<= 1.25x` the oracle median:
`<= 3.68 s` at the parity tier, `<= 498.5 s` at the memory tier. Its absolute
peak-RSS ceiling is 8,192 MB HARD, and its flatness bound is native memory-tier
peak `<= 1.5x` its parity-tier peak. The oracle's own 25.7x RSS growth
(223.7 MB -> 5,754.5 MB across the 300x row increase) is the baseline the
native route must flatten.
