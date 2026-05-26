# Performance fixture suite (PERF.BASE.2)

Reproducible synthetic data at three scales for benchmarking the engine
against itself across substrate changes. Same column set + shape in
every tier so per-strategy timing comparisons are apples-to-apples.

## Tiers

| Tier | Rows | Columns | ~Parquet size | Committed | Purpose |
|---|---|---|---|---|---|
| small | 1,000 | 11 | ~80 KB | yes | Per-call overhead probe |
| medium | 100,000 | 30 | ~20 MB | yes | Steady-state throughput |
| large | 10,000,000 | 50 | ~5-10 GB | **no** | Scale ceiling + memory pressure |

The committed fixtures live at `tests/perf_fixtures/<tier>/data.parquet`
with a companion `fixture.yaml` (schema + recorded sha256). The large
tier is gitignored; regenerate it locally when you need it.

## Regeneration

```bash
# One tier:
python scripts/gen_perf_fixtures.py small
python scripts/gen_perf_fixtures.py medium
python scripts/gen_perf_fixtures.py large   # ~5-10 GB, several minutes

# All committed tiers (small + medium), overwriting in place:
python scripts/gen_perf_fixtures.py all --force
```

Determinism contract: same engine version + same tier name => byte-identical
Parquet. Verified by `test_fixture_reproducibility.py`. Cross-version
drift is permitted; bump the recorded sha256 in `fixture.yaml` when
Faker or pyarrow shifts on the engine side.

Regenerating a committed fixture is a deliberate act: the test
`test_parquet_matches_recorded_sha256` will fail until you commit both
the new Parquet and the updated `fixture.yaml` together.

## Running the perf tests

The fixture validation tests are tagged `@pytest.mark.perf` so they
stay out of the default `pytest tests/` run.

```bash
pytest tests/perf_fixtures/ -m perf
```

`@pytest.mark.perf` is registered in `pyproject.toml` alongside the
existing `@pytest.mark.benchmark` marker.

## Strategy intensity mix

Each tier ships strategy-tagged columns covering all three perf bands.
Benchmarks under `tests/benchmark/transforms/` consume these columns to
generate per-strategy timing tables; the strategy_band tags in
`schema.COMMON_COLUMNS` document which column is meant for which band.

| Band | Column | Strategy it exercises |
|---|---|---|
| cheap | `customer_id` | passthrough |
| cheap | `ssn` | redact |
| cheap | `account_balance` | truncate |
| medium | `full_name` | faker |
| medium | `dob` | date_shift |
| medium | `score` | bucketize |
| expensive | `email` | fpe |
| expensive | `transaction_amount` | formula |
| expensive | `zip` | reference / FK |

PERF.BASE.3 (baseline measurement) reads these tiers + tags to build
the 36-cell matrix (12 strategies x 3 tiers).

## Caveats

- **All values are synthetic.** Faker output only; zero real customer
  data. Safe to commit; safe to publish.
- **Large tier is not byte-stable across machines** (the small / medium
  tiers are). On the large tier the determinism contract is "same row
  count + same schema + same statistical distribution," not exact
  bytes; the test suite does not assert sha256 there.
- **Faker upgrades change values.** When the engine bumps Faker, the
  committed sha256 will drift -- expected, regenerate at that point.
