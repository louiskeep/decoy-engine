# Recipes

Five end-to-end recipes. Each one is runnable as written. The CLI paths use
the `decoy` command (the `decoy-cli` package); the in-process library path in
recipe (a) shows the same work done from Python.

For the config shapes used below, see the bundled templates shipped with the
CLI (`decoy templates list`) and the [strategies](strategies.md) catalog.

## (a) Mask one CSV

### With the CLI

`pipeline.yaml`:

```yaml
version: 1
global_settings:
  seed: 42
sources:
  people:
    type: file
    format: csv
    path: ./people.csv
tables:
  - name: people
    columns:
      - name: email
        strategy: faker
        provider: person_email
      - name: ssn
        strategy: redact
targets:
  people:
    type: file
    format: csv
    path: ./people_masked.csv
```

```
decoy run pipeline.yaml
```

### From Python (in-process)

The engine is plan-first: validate the config once at the choke-point, profile
the source, compile a frozen plan, and run it on the default execution adapter.

```python
import pyarrow as pa
import pyarrow.csv as pacsv

from decoy_engine import (
    PipelineConfig,
    compile_plan,
    select_execution_adapter,
)
from decoy_engine.profile import profile_source

config_dict = {
    "version": 1,
    "global_settings": {"seed": 42},
    "sources": {
        "people": {"type": "file", "format": "csv", "path": "./people.csv"},
    },
    "tables": [
        {
            "name": "people",
            "columns": [
                {"name": "email", "strategy": "faker", "provider": "person_email"},
                {"name": "ssn", "strategy": "redact"},
            ],
        }
    ],
}

# 1. Validate once. Downstream code does not re-validate.
config = PipelineConfig.model_validate(config_dict).model_dump()

# 2. Profile the declared sources. profile_source reads config["sources"]
#    (file/s3/gcs descriptors) itself.
profile = profile_source(config, seed=42)

# 3. Compile a frozen plan.
plan = compile_plan(config, profile, decoy_engine_version="0.1.0")

# 4. Load the in-memory source tables and run the plan.
sources = {"people": pacsv.read_csv("people.csv")}
result = select_execution_adapter().run(plan, sources)

# result.outputs["people"] is the masked pyarrow.Table.
masked = result.outputs["people"]
```

<!-- VERIFY: the in-process call sequence above. Confirmed by reading:
profile_source(config, *, sample_rows=, seed=) reads config["sources"] file
descriptors (profile/_source.py); compile_plan(config, profile, *,
decoy_engine_version=) (plan/_compile.py); ExecutionResult.outputs is the
table dict and ExecutionResult.output is the single-table accessor
(execution/_adapter.py). NOT verified by execution: whether
select_execution_adapter().run(plan, sources) needs the registry /
relationship_graph / namespace_registry kwargs that the golden E2E _run helper
passes explicitly (tests/integration/golden/test_execution_e2e.py uses
PandasExecutionAdapter().run(plan, sources, registry=..., relationship_graph=...,
namespace_registry=...)). The README quickstart calls .run(plan, sources) with
no extra kwargs, so the defaults likely cover the single-table no-FK case;
please run this snippet to confirm before publishing, and add the kwargs if the
adapter requires them. The README at decoy-engine/README.md also calls
profile_source(sources) with a dict of arrow tables and reads result.tables -
both are inconsistent with the code (profile_source takes a config dict;
the field is .outputs). Flagged separately to Dennis. -->

The CLI is the recommended path for most callers; the library path is for
embedding the engine inside your own tool.

## (b) Mask a folder, preserving foreign keys

When a parent table and a child table share a key, Decoy keeps the join intact:
the same source key maps to the same masked key on both sides. Declare the
relationship in the config and mask both tables in one run.

```yaml
version: 1
global_settings:
  seed: 7
sources:
  customers:
    type: file
    format: csv
    path: ./customers.csv
  orders:
    type: file
    format: csv
    path: ./orders.csv
tables:
  - name: customers
    columns:
      - name: customer_id
        strategy: faker
        provider: person_email
        namespace: customer_identity
      - name: email
        strategy: faker
        provider: person_email
  - name: orders
    columns:
      - name: customer_id
        strategy: faker
        provider: person_email
        namespace: customer_identity
relationships:
  - parent: {table: customers, columns: [customer_id]}
    children:
      - {table: orders, columns: [customer_id]}
    orphan_policy: preserve
    namespace: customer_identity
targets:
  customers:
    type: file
    format: csv
    path: ./out/customers.csv
  orders:
    type: file
    format: csv
    path: ./out/orders.csv
```

```
decoy run pipeline.yaml
```

The `relationships` block plus a shared `namespace` is what binds the parent
and child keys. `orphan_policy` controls what happens to child rows whose key
has no parent (`preserve`, `remap`, `warn`, `fail`). See
[relationships](relationships.md) for the full contract.

<!-- VERIFY: the exact YAML key names in the `relationships` block
(`parent` / `children` / `orphan_policy` / `namespace`). These mirror the
config dicts in tests/integration/golden/test_execution_e2e.py
(_orphan_fk_config), which build the model_dump()-ed form. Confirm the
surface YAML field names match by running `decoy validate` on this file. -->

## (c) Generate a synthetic table

Generation needs no input file: it builds rows from a column spec. Set
`mode: generate` (or pass `--mode generate` on the command line).

```yaml
version: 1
global_settings:
  seed: 42
sources: {}
tables:
  - name: employees
    row_count: 1000
    generate_columns:
      - name: employee_id
        type: sequence
        start: 1000
        step: 1
      - name: first_name
        type: faker
        faker_type: first_name
      - name: department
        type: categorical
        categories: ["Engineering", "Marketing", "Sales", "HR", "Finance"]
        weights: [0.3, 0.2, 0.3, 0.1, 0.1]
targets:
  employees:
    type: file
    format: csv
    path: ./employees.csv
```

```
decoy run pipeline.yaml --mode generate
```

Column `type` values: `sequence`, `faker`, `categorical`, `reference`,
`formula`, `distribution`. See [strategies](strategies.md) for what each does.

## (d) Detect PII (storm)

`storm` profiles a dataset for PII, format signals, and re-identification risk
without masking anything. The canonical command is `storm analyze`.

```
decoy storm analyze data.csv
decoy storm analyze data.csv --json --out profile.json
```

Useful flags: `--rows N` caps the rows scanned; `--strategy` selects the
sampling strategy. The result is a JSON-serializable profile (the same
`StormProfile` the library's `run_storm` returns). `storm scan` is a
deprecated alias for `storm analyze` and still works.

From Python:

```python
import pandas as pd
from decoy_engine import run_storm

df = pd.read_csv("data.csv")
profile = run_storm(df, "data.csv")
# profile is a JSON-serializable StormProfile.
```

## (e) Use Decoy in CI

Two patterns. First, validate every pipeline config in the repo so a broken
config fails the build before any run:

```yaml
# .github/workflows/decoy.yml (excerpt)
- run: pip install decoy-engine decoy-cli
- run: decoy validate pipelines/*.yaml
```

Second, run a mask and gate on the exit code (and optionally on the structured
JSON result):

```
decoy run pipeline.yaml --json --quiet
```

`run` exits non-zero on failure, so a plain `decoy run pipeline.yaml` already
gates the job. `--json` emits a structured result on stdout (progress goes to
stderr) for a CI step that wants to assert on the report.

<!-- VERIFY: that `decoy validate pipelines/*.yaml` accepts a glob / multiple
path arguments. The validate command takes a single `config: Path` argument
(decoy/src/decoy/cli/validate.py); shell glob expansion to multiple paths may
need a loop instead. Confirm whether validate accepts multiple files or
whether CI should iterate. -->
