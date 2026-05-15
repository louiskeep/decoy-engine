# decoy_engine Package Map

## One-Line Summary

Core data library used by the CLI and platform for masking, generation, graph execution, connectors, STORM, and FORECAST.

## Package Map

| Path | What Lives Here |
|---|---|
| `__init__.py` | Public exports |
| `context.py` | ExecutionContext, Logger protocol, structured event helpers |
| `validation.py` | Public validation facade |
| `exceptions.py` | Engine exception types |
| `graph/` | Graph runtime, conversion, op registry, op modules |
| `transforms/` | Masking strategies and registry |
| `generators/` | Legacy generator, column generation, relationships |
| `connectors/` | Legacy IO handlers and cloud/file connectors |
| `storm/` | Profiling, detectors, sentinels, types |
| `forecast/` | Recommendation types and recommender |
| `walks/` | Schema walk/diff/hazard logic |
| `disguises/` | Disguise YAML and loader |
| `internal/` | Private validators, helpers, memory, integrity, mappings |
| `schema/`, `license/` | Public-ish stubs; verify capability before using |

## Hot Paths

| Task | Start Here |
|---|---|
| Run graph | `graph/runner.py` |
| Add graph op | `graph/ops/` and `graph/ops/__init__.py` |
| Convert substrates | `graph/conversion.py` |
| Validate config | `validation.py`, `internal/validator.py` |
| Apply masks | `graph/ops/mask_op.py`, `transforms/registry.py` |
| Source/target files | `graph/ops/source_file.py`, `target_file.py`, `_cloud_io.py` |
| Cloud sources/targets | `graph/ops/source_s3.py`, `source_gcs.py`, `source_sftp.py`, targets |
| STORM scan | `storm/profiler.py`, `storm/detectors.py` |
| FORECAST recommend | `forecast/recommender.py` |

## Gotchas

| Gotcha | Note |
|---|---|
| Private internals | `internal/` can change; avoid external imports |
| Hidden context coupling | Some graph exports use context state; inspect runner before changing |
| Native engine declarations | Invalid declarations should become validation failures |
| Row-wise paths | Generation/formula/STORM hot paths may need benchmarks |
