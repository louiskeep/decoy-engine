# decoy-engine

The shared Python data engine used by both the CLI and the platform.

`pip install decoy-engine`

## What lives here

- Data masking pipeline (`Masker`)
- Masking transforms — faker, hash, redact, map, shuffle, date-shift, formula, passthrough
- Synthetic data generation (`DataGenerator`)
- Connectors — CSV, fixed-width, database
- Referential integrity management
- Public API contract (`__init__.py.__all__`)

## What does NOT live here

- CLI commands → `decoy`
- Web platform → `decoy-platform`
- Marketing site → `decoy-web`

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for the internal component map (transforms, pipeline graph, generators, execution context) and where to start reading.

## Public API

```python
from decoy_engine import (
    Masker,
    DataGenerator,
    ExecutionContext, Logger, TelemetryClient,
    SchemaInspector, LicenseVerifier,
    validate_config,
    DecoyError, ConfigError, PipelineValidationError,
    ConnectorError, ConnectorAuthError,
    LicenseError, LicenseExpiredError,
)
```

`ForgeError` is a deprecated alias for `DecoyError`, kept for one minor version.

Everything in `decoy_engine.internal` is private and may change between minor versions.

## Dev setup

```bash
pip install -e .
pytest tests/
```
