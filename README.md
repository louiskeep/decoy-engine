# decoy-engine

`decoy-engine` is the data-plane library that powers Decoy masking and
synthetic-data generation. It validates a pipeline config, compiles it
into a frozen plan, and runs that plan against tabular sources (CSV /
Parquet / Arrow / pandas / Polars) to produce deterministic, masked or
synthesized output.

The engine is Apache-2.0 licensed and has no network or auth surface of
its own: it is a library you call from your own code. The companion
`decoy-cli` package is the recommended way to drive it from a shell.

## Install

```
pip install decoy-engine
```

Python 3.10, 3.11, and 3.12 are supported. Heavy dependencies (pandas,
Polars, PyArrow) are pulled in automatically.

## Quickstart

The shortest path from import to masked output: validate a config dict,
profile the source, compile a plan, and run it on a Polars-backed
execution adapter.

```python
import pandas as pd
import pyarrow as pa

from decoy_engine import (
    PipelineConfig,
    compile_plan,
    select_execution_adapter,
)
from decoy_engine.profile import profile_source

config_dict = {
    "version": 1,
    "global_settings": {"seed": 42},
    "tables": [
        {
            "name": "people",
            "columns": [
                {"name": "first_name", "strategy": "faker", "provider": "person_first_name"},
                {"name": "last_name",  "strategy": "faker", "provider": "person_last_name"},
                {"name": "email",      "strategy": "faker", "provider": "person_email"},
                {"name": "ssn",        "strategy": "redact"},
            ],
        }
    ],
}

# 1. Validate once at the choke-point. Downstream code does not re-validate.
config = PipelineConfig.model_validate(config_dict).model_dump()

# 2. Load source data and profile it.
df = pd.read_csv("people.csv")
sources = {"people": pa.Table.from_pandas(df, preserve_index=False)}
profile = profile_source(sources)

# 3. Compile a frozen plan, then run it through the default execution adapter.
plan = compile_plan(config, profile, decoy_engine_version="0.1.0")
result = select_execution_adapter().run(plan, sources)

# `result.tables["people"]` is the masked pyarrow.Table.
```

A more complete, runnable end-to-end shape lives in
`tests/integration/golden/test_execution_e2e.py` (the `_run` helper is
the canonical caller pattern).

## Public API at a glance

The full public surface lives in `decoy_engine.__all__`. The contract
pieces most callers need:

| Symbol                                                                            | Use                                                                          |
|-----------------------------------------------------------------------------------|------------------------------------------------------------------------------|
| `PipelineConfig`                                                                  | Strict pipeline-config schema. Validate once: `PipelineConfig.model_validate(yaml).model_dump()`. |
| `compile_plan(config, profile, decoy_engine_version=...)`                         | Compile a validated config + Profile into a frozen `Plan`.                   |
| `select_execution_adapter()` / `PandasExecutionAdapter` / `PolarsExecutionAdapter`| Plan-to-data execution. Polars is the default substrate.                     |
| `generate_tables(...)`                                                            | Table-from-schema synthesis for `mode: generate` configs.                    |
| `run_storm(...)`                                                                  | Source profiling: distributions, PII detectors, sentinels.                   |
| `validate_config(...)`                                                            | Validation report without raising. Returns a `ValidationResult`.             |

Anything not in `__all__`, and anything under `decoy_engine.internal/`,
is private and may change without a version bump.

## Repository layout

| Path                                       | What lives here                                                          |
|--------------------------------------------|--------------------------------------------------------------------------|
| `src/decoy_engine/config/`                 | `PipelineConfig`, source/target descriptors, relationship config.         |
| `src/decoy_engine/plan/`                   | `compile_plan` and the frozen `Plan`.                                    |
| `src/decoy_engine/execution/`              | `ExecutionAdapter` protocol, Pandas + Polars adapters, strategy handlers. |
| `src/decoy_engine/generation/`             | `generate_tables`, composite providers, value pools.                     |
| `src/decoy_engine/providers_v2/`           | Provider registry + identifier adapters (NPI, SSN, EIN, MRN, NDC).        |
| `src/decoy_engine/relationships/`          | Relationship graph, namespace registry, orphan-FK policy.                 |
| `src/decoy_engine/storm/`                  | Source profiling and PII detectors.                                       |
| `src/decoy_engine/sdk.py`                  | Public Connector SDK (`FileSource`, `FileSink`, capability flags).        |
| `src/decoy_engine/connectors/`             | In-tree file connectors (`s3.py`, `gcs.py`, `sftp.py`).                   |
| `tests/integration/golden/`                | Canonical end-to-end caller examples.                                    |

`CODEMAP.md` has the full directory map.

## Where to go next

- `CHANGELOG.md` -- release notes.
- `SECURITY.md` -- how to report a vulnerability privately.
- `CONTRIBUTING.md` -- build, test, and contribution flow.
- `CODEMAP.md` -- complete codebase navigation map.
- `tests/integration/golden/` -- canonical end-to-end caller shapes.
- `decoy-cli` (PyPI) -- the command-line driver. Recommended unless you
  are embedding the engine inside your own Python tool.

## License

Apache License 2.0. See `LICENSE` and `NOTICE`.
