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
import pyarrow.csv as pacsv

from decoy_engine import PipelineConfig, run_pipeline

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

# 1. Validate once at the choke-point. Downstream code does not re-validate.
config = PipelineConfig.model_validate(config_dict).model_dump()

# 2. Load the in-memory source tables (keyed by table name).
sources = {"people": pacsv.read_csv("people.csv")}

# 3. Run the whole pipeline: profile, compile, wire relationships, execute.
result = run_pipeline(config, sources, engine_version="0.1.0")

# result.outputs["people"] is the masked pyarrow.Table.
masked = result.outputs["people"]
```

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

Every detector match carries a semantic `domain` (`IDENTITY`, `FINANCIAL`, `HEALTH`,
`CONTACT`, `LOCATION`, or `OTHER`) so you can group or filter findings without
memorizing detector ids. The mapping lives in `decoy_engine.storm.domains`:

```python
from decoy_engine.storm.domains import domain_for

domain_for("ssn")    # Domain.IDENTITY
domain_for("email")  # Domain.CONTACT
```

The same `domain` appears on each `FieldStats` and `DetectorMatch` in the profile.

## (e) Use Decoy in CI

Two patterns. First, validate every pipeline config in the repo so a broken
config fails the build before any run:

```yaml
# .github/workflows/decoy.yml (excerpt)
- run: pip install decoy-engine decoy-cli
- run: |
    for f in pipelines/*.yaml; do
      decoy validate "$f"
    done
```

Second, run a mask and gate on the exit code (and optionally on the structured
JSON result):

```
decoy run pipeline.yaml --json --quiet
```

`run` exits non-zero on failure, so a plain `decoy run pipeline.yaml` already
gates the job. `--json` emits a structured result on stdout (progress goes to
stderr) for a CI step that wants to assert on the report.
