# SQL And Expression Injection Surfaces

Sprint 3.2 security audit. Sprint 6 remediation.

Every surface in the engine where user-supplied config values flow into SQL
strings or evaluated expressions. Each entry notes the risk level and current
status.

## Release Status Summary

| Surface | File | Risk | Status |
|---|---|---|---|
| `source.db` WHERE clause (native scanner) | `graph/ops/source_db.py` | HIGH | **Fixed Sprint 6** -- DuckDB relational API |
| `source.db` WHERE clause (SQLAlchemy fallback) | `graph/ops/source_db.py` | MEDIUM | **Accepted** -- SQL-literate config field; identifier names validated |
| `filter` predicate | `graph/ops/filter_op.py` | MEDIUM | **Fixed Sprint 6** -- pl.sql_expr() |
| `if_router` predicate | `graph/ops/if_router.py` | MEDIUM | **Fixed Sprint 6** -- pl.sql_expr() |
| `derive` expression | `graph/ops/derive.py` | MEDIUM | **Fixed Sprint 6** -- pl.sql_expr() |
| `sql_run` direct SQL | `graph/ops/sql_run.py` | INTENTIONAL | Power-user escape hatch by design |
| `target.db` table/schema names | `graph/ops/target_db.py` | LOW | **Fixed Sprint 6** -- identifier validation |
| `source.db` table/schema names | `graph/ops/source_db.py` | LOW | **Fixed Sprint 6** -- identifier validation |

---

## source.db WHERE Clause -- Native Scanner Path (FIXED Sprint 6)

**File:** `src/decoy_engine/graph/ops/source_db.py`, `_apply_duckdb_native_scanner`.

**Previous state:** The `where` config value was concatenated into a full DuckDB
SQL string executed against an external database via `ATTACH`. A crafted WHERE
value could inject UNION SELECT or other fragments.

**Sprint 6 fix:** The native-scanner path now uses the DuckDB relational API:

```python
rel = con.sql(f"SELECT * FROM {qualified}")  # validated identifier only
if where:
    rel = rel.filter(where)  # relational filter, not a full SQL statement
if row_limit:
    rel = rel.limit(int(row_limit))
return rel.arrow()
```

`rel.filter()` parses `where` as a filter expression against the relation's
columns. The expression cannot introduce UNION, stacked queries, or new table
references that the relational context does not already expose.

Table and schema identifiers are validated by `_validate_sql_identifier()`
before entering the SQL string.

---

## source.db WHERE Clause -- SQLAlchemy Fallback (ACCEPTED)

**File:** `src/decoy_engine/graph/ops/source_db.py`, `_build_select`.

The SQLAlchemy fallback is used only for non-SQLite/Postgres dialects (MSSQL,
Oracle, MySQL). For these paths, `where` is still concatenated into SQL.

**Accepted risk:** The `where` field is treated as a SQL-literate config
field at the same trust level as `table` and `schema`. Self-hosted
deployments where the operator writes the pipeline YAML are the target
scenario. Multi-tenant deployments should gate access to `source.db` nodes
at the preflight layer (tracked separately).

**Identifier protection:** Table and schema names in `_build_select` are
double-quoted and validated by `_validate_sql_identifier()` so identifier
injection through table/schema is blocked. The WHERE clause itself is not
validated; it is documented as SQL-literate input.

---

## filter, if_router, derive -- Polars Expression API (FIXED Sprint 6)

**Files:** `filter_op.py`, `if_router.py`, `derive.py`.

**Previous state:** These ops built SQL strings from user-controlled config
values and executed them via `pl.SQLContext`. The Polars SQLContext operates
on in-memory frames only, but it accepts full SQL statements, which meant a
crafted predicate could invoke Polars SQL table-valued functions such as
`read_csv()` or `read_parquet()`, potentially reading arbitrary on-disk files.

**Sprint 6 fix:** All three ops now use `pl.sql_expr()` instead:

- `filter`: `df.filter(pl.sql_expr(predicate))`
- `if_router`: `df.filter(pl.sql_expr(predicate))` and `df.filter(~pl.sql_expr(predicate))`
- `derive`: `df.with_columns(pl.sql_expr(expression).alias(column))`

`pl.sql_expr()` parses a SQL expression string into a Polars `Expr` object and
evaluates it in the scope of the frame's existing columns only. It cannot
construct full SQL statements, reference external tables, or invoke
table-valued functions. No `# noqa: S608` suppressions remain in these files.

The pandas fallback paths (`df.query(engine="python")` and `df.eval()`) are
unchanged; they are secondary paths in pandas-mode graphs.

---

## sql_run -- Intentional Power-User Escape Hatch

**File:** `src/decoy_engine/graph/ops/sql_run.py`.

This op accepts raw SQL and executes it against a DuckDB in-memory database.
This is intentional and documented in the op's module docstring.

For multi-tenant deployments, `sql_run` nodes should require a platform-level
permission check (e.g. `require_permission("pipeline.sql_run")`) in the
preflight layer before allowing a run. This is a deferred item tracked in the
platform roadmap.

---

## target.db And source.db Table / Schema Names (FIXED Sprint 6)

**Files:** `graph/ops/target_db.py`, `graph/ops/source_db.py`.

**Previous state:** Table and schema names were double-quoted in SQL strings but
not validated. A value like `my_table"DROP TABLE` could break the quoting
boundary.

**Sprint 6 fix:** `_validate_sql_identifier(name, path)` is called on `table`
and `schema` in `validate_config()` for both ops. The validator enforces:

```
^[A-Za-z_][A-Za-z0-9_$]*$
```

This rejects double quotes, semicolons, spaces, dots, and other SQL
metacharacters. The restriction is conservative -- table names with hyphens
or non-ASCII characters are not supported in Release 1.0. If a customer
needs such identifiers, this constraint should be revisited with an explicit
escape-and-validate strategy.

`_validate_sql_identifier` lives in `source_db.py` and is imported by
`target_db.py` so both ops use the same rule.
