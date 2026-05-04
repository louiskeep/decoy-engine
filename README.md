# forge-engine

The shared Python data engine used by both the CLI and the platform.

`pip install forge-engine`

## What lives here

- Data masking pipeline (`Pipeline`, `PipelineConfig`)
- Masking transforms — faker, hash, redact, map, shuffle, date-shift, formula, passthrough
- Synthetic data generation (`DataGenerator`)
- Connectors — CSV, fixed-width, database
- Referential integrity management
- Public API contract (`__init__.py.__all__`)

## What does NOT live here

- CLI commands → `forge`
- Web platform → `forge-platform`
- Marketing site → `forge-web`

## Public API

```python
from forge_engine import (
    Pipeline, PipelineConfig,
    DataGenerator,
    MaskRegistry, ConnectorRegistry,
    ExecutionContext, Logger,
)
```

Everything in `forge_engine.internal` is private and may change between minor versions.

## Dev setup

```bash
pip install -e .
pytest tests/
```
