# Polars for pandas users — quick reference

> **Status:** target. Updated as new patterns surface in Phase 3 + 4 ports.
> **Last reviewed:** 2026-05-10.
> **Audience:** contributors writing or modifying graph ops that declare `NATIVE_ENGINE = "polars"`. The relational ops in `src/decoy_engine/graph/ops/` are the main reference; this doc is the orientation for the API differences you'll hit.

The substrate switch (`plans/2026-05-10-polars-duckdb-hybrid-engine.md`) introduces Polars for relational ops. Polars' API is "different, not just renamed." This doc is the friction-point cheat sheet so you don't spend half a day re-discovering it.

## Top idioms side by side

| pandas | Polars |
|---|---|
| `df[df.col == 'x']` | `df.filter(pl.col('col') == 'x')` |
| `df[['a', 'b']]` | `df.select(['a', 'b'])` |
| `df.groupby('col').agg({'val': 'sum'})` | `df.group_by('col').agg(pl.col('val').sum())` |
| `df.merge(other, on='key')` | `df.join(other, on='key')` |
| `df.sort_values('col', ascending=False)` | `df.sort('col', descending=True)` |
| `df.drop_duplicates(['a', 'b'])` | `df.unique(subset=['a', 'b'])` |
| `df.assign(c=df.a + df.b)` | `df.with_columns((pl.col('a') + pl.col('b')).alias('c'))` |
| `df.rename(columns={'a': 'A'})` | `df.rename({'a': 'A'})` |
| `df.dropna(subset=['col'])` | `df.drop_nulls(subset=['col'])` |
| `df.fillna({'col': 0})` | `df.with_columns(pl.col('col').fill_null(0))` |
| `df.col.str.contains('x')` | `pl.col('col').str.contains('x')` |
| `df.col.str.replace('a', 'b')` | `pl.col('col').str.replace('a', 'b')` |
| `pd.to_datetime(df.col)` | `pl.col('col').str.to_datetime()` |
| `df.shape` | `df.shape` (same!) |
| `df.head(n)` | `df.head(n)` (same!) |
| `df.col.value_counts()` | `df.group_by('col').len()` |
| `df.col.isin(['a', 'b'])` | `pl.col('col').is_in(['a', 'b'])` |
| `df.col.cumsum()` | `pl.col('col').cum_sum()` |
| `df.query("x > 5 and y == 'foo'")` | See "Filter / derive" below — use SQLContext |

## Filter / derive: SQLContext vs expression DSL

The graph's `filter` and `derive` ops accept user-supplied predicate / expression strings (pandas-eval syntax in MVP). For the polars port we use `pl.SQLContext` because it accepts the same shape:

```python
sql = f"SELECT * FROM df WHERE {predicate}"
with pl.SQLContext(df=df, eager=True) as ctx:
    return ctx.execute(sql)
```

This works for `state == 'CA' and value >= 18` and the rest of the canvas's predicate-builder output. Documented divergences live in `tests/parity/SEMANTIC_DIFFERENCES.md`:

- `is` / `is not` / `in` (Python operators) don't translate. SQLContext rejects; the canvas builder doesn't emit these.
- Quoted identifiers: SQLContext requires `"col with space"` for column names with spaces; pandas-eval is laxer.
- Datetime literals: pandas-eval auto-coerces `'2025-01-01'` to a date when compared against a datetime column; SQLContext doesn't. Cast explicitly with `CAST(col AS DATE)`.

When you write a new op that needs expression evaluation, prefer SQLContext + a few translation rules over a hand-rolled parser. Less surface, more familiar to future-you.

## The `.map_elements()` footgun

When you find yourself reaching for it, **stop**. `.map_elements(callback, return_dtype=...)` falls out of the lazy planner, slows to pandas-speed, and breaks parallelization. Three patterns to use instead:

### 1. Rewrite as a Polars expression (preferred)

```python
# Footgun:
df.with_columns(
    pl.col("name").map_elements(lambda x: x.upper().strip(), return_dtype=pl.Utf8)
)

# Correct:
df.with_columns(pl.col("name").str.to_uppercase().str.strip_chars())
```

Most "I need a callback" cases collapse to a chain of `.str.*`, `.dt.*`, `pl.when(...).then(...).otherwise(...)`.

### 2. When there's no Polars equivalent: declare pandas

```python
# Footgun:
def apply(inputs, config, ctx):
    return df.with_columns(
        pl.col("encrypted").map_elements(decrypt_via_kms, return_dtype=pl.Utf8)
    )

# Correct: this op is per-row Python with a non-Polars dependency.
# In the op module declare:
#   NATIVE_ENGINE = "pandas"
# The runner converts at the boundary; you write idiomatic pandas.
```

This is exactly why `mask` / `generate` / `run_storm` stay on pandas — they call into Faker / scipy / sklearn, which has no polars equivalent.

### 3. When `.map_elements()` is genuinely OK

```python
# Acceptable when the callback is fast pure-Python AND the column is small.
# Document why in a comment so the next reader doesn't reach for the same
# hammer for the wrong nail.
df.with_columns(
    pl.col("status_code").map_elements(_translate_legacy_code, return_dtype=pl.Utf8)
    # Legacy code → human label is a 50-row dict lookup; .map_elements is fine.
)
```

**Code review checkpoint**: every PR that introduces a Polars op must justify any `.map_elements()` call in the description, or get reviewer pushback.

## Lazy vs eager mental model

### Eager (default)

```python
df = pl.read_csv('file.csv')   # loads entire file into RAM
df = df.filter(...)            # filter applied immediately
```

### Lazy (preferred for big data)

```python
df = (
    pl.scan_csv('file.csv')              # builds a query plan; doesn't load
    .filter(pl.col('country') == 'US')   # plan adds filter
    .select(['id', 'name'])              # plan adds projection
    .collect()                           # NOW the file is read; filter
                                         # and projection pushed down to
                                         # the scan
)
```

**Use `scan_*` instead of `read_*`** for any input that's "big enough to care." The lazy planner is most of the win — Polars' optimizer pushes filters and column projections down to the source so you only read what you need.

For our graph runner, the ops live inside `apply()` which receives an eager DataFrame from the runner cache. If you want lazy-mode benefits, scan inside the op and `.collect()` before returning. The runner expects an eager DataFrame at the boundary so it can cache the result as `pa.Table` via `.to_arrow()`.

## Cheat sheet for our workload

- All relational ops (filter / sort / dedupe / derive / join / group_by) declare `NATIVE_ENGINE = "polars"`.
- All mask transforms (hash / faker / fpe / etc.) stay on pandas (`NATIVE_ENGINE = "pandas"`).
- All sources / sinks (CSV / parquet / Postgres / MySQL) use DuckDB (`NATIVE_ENGINE = "duckdb"`).
- The runner converts between these via `arrow_to_engine()` / `engine_to_arrow()` in `src/decoy_engine/graph/conversion.py`.
- See `CONNECTOR_SDK_CONTRACT.md` if writing a new connector.
- See parity tests in `tests/parity/` for known semantic differences.

## When in doubt

Run the parity test for the op you're modifying:

```
pytest tests/parity/test_relational_ops_parity.py::test_<op>_parity_* -v
```

The test compares the polars output against the pandas output on the same input. If they diverge, you've either found a bug or a new entry for `SEMANTIC_DIFFERENCES.md`. Either way, surface it before merging.
