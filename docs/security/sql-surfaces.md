# SQL And Expression Injection Surfaces

Sprint 3.2 security audit. Every surface in the engine where user-supplied
config values flow into SQL strings or evaluated expressions. Each entry
notes the risk level and the planned fix sprint.

This file satisfies the Sprint 3 "Done when" criterion:
> Security notes identify remaining privileged SQL or raw fragment surfaces
> and their release status.

## Release Status Summary

| Surface | File | Risk | Status |
|---|---|---|---|
| `source.db` WHERE clause | `graph/ops/source_db.py` lines 118-120, 225 | HIGH | Fix Sprint 6 |
| `filter` predicate (Polars SQLContext) | `graph/ops/filter_op.py` line 73-76 | MEDIUM | Fix Sprint 6 |
| `if_router` predicate (Polars SQLContext) | `graph/ops/if_router.py` lines 46-49 | MEDIUM | Fix Sprint 6 |
| `derive` expression (Polars SQLContext) | `graph/ops/derive.py` line 70 | MEDIUM | Fix Sprint 6 |
| `sql_run` direct SQL | `graph/ops/sql_run.py` line 78 | INTENTIONAL | Power-user escape hatch by design |
| `target.db` table/schema names | `graph/ops/target_db.py` lines 117-121 | LOW | Fix Sprint 6 |

---

## source.db WHERE Clause (HIGH)

**File:** `src/decoy_engine/graph/ops/source_db.py`, in
`_apply_duckdb_native_scanner` (line 118-120) and `_build_select` (line 225).

The `where` config value is concatenated directly into a DuckDB SQL query
that is then executed against a real external database (via `ATTACH`).
An attacker who can write pipeline YAML can inject arbitrary SQL into
the external database connection, including `UNION SELECT`, stacked queries
(depending on the driver), or `DROP TABLE` fragments.

```python
# CURRENT (unsafe)
sql = f"SELECT * FROM {qualified}"
if where:
    sql += f" WHERE {where}"  # user config injected into external DB query
```

**Planned fix (Sprint 6):** Replace string concatenation with the DuckDB
relational API (`.filter()` or parameterized `.execute(sql, [params])`) which
does not allow SQL fragment injection. If the relational API cannot express
all WHERE predicates, enforce a parsed boolean-expression allowlist (same
approach as Sprint 6's filter/if_router fix).

---

## filter, if_router, derive — Polars SQLContext (MEDIUM)

**Files:** `filter_op.py`, `if_router.py`, `derive.py`.

These ops build SQL strings from user-controlled `predicate` / `expression`
config values and execute them via `pl.SQLContext`. The Polars `SQLContext`
operates on the pipeline's **in-memory frames only** — it has no persistent
credentials or network connections by default.

The risk is narrower than external-DB injection but real:

- Polars SQL supports `read_csv()`, `read_parquet()` as table-valued
  functions. A crafted predicate could read arbitrary on-disk files if the
  host process has read permission.
- `pandas.query(engine="python")` runs the Python eval engine; a crafted
  predicate could import modules or access process state.

These surfaces require **pipeline-config write access** to exploit — the
attacker must control the YAML saved in the pipeline. For self-hosted
deployments where the same person writes and runs pipelines this is low
operational risk. For multi-tenant or admin-runs-pipelines-for-users
deployments this is a meaningful privilege boundary.

**Planned fix (Sprint 6):** Migrate `filter` and `if_router` to the Polars
expression API (`.filter(pl.Expr)` compiled from a parsed expression tree)
instead of SQL string construction, eliminating the injection surface without
changing user-visible predicate syntax. `derive` follows the same pattern
(`.with_columns([pl.Expr])`). The pandas fallback paths use
`pd.eval(engine="numexpr")` which does not run the Python evaluator.

---

## sql_run — Intentional Power-User Escape Hatch

**File:** `src/decoy_engine/graph/ops/sql_run.py`, line 78.

This op is an intentional escape hatch that accepts raw SQL and executes
it against a DuckDB in-memory database. The op's module docstring documents
this intent. It is **not a bug**.

For multi-tenant deployments, `sql_run` nodes should require a platform-level
permission check (e.g. `require_permission("pipeline.sql_run")`) in the
preflight layer before allowing a run. This is a Sprint 2 deferred item.

---

## target.db Table and Schema Names (LOW)

**File:** `src/decoy_engine/graph/ops/target_db.py`, lines 117-121.

Table and schema names are double-quoted in the SQL string but not escaped
for double-quote injection. A config value like `my_table"DROP TABLE ` can
break the quoting boundary.

In practice, the `table` and `schema` config values come from the pipeline
YAML and go through `validate_config`; the validator checks that they are
non-empty strings but does not reject quote characters.

**Planned fix (Sprint 6):** Add a `_validate_sql_identifier(name: str)`
helper that raises `ValidationError` for any identifier containing `"`, `;`,
or other SQL metacharacters before the value enters the query string.
