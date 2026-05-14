# decoy-engine

The shared Python data engine used by both the CLI and the platform.

`pip install decoy-engine`

## What lives here

- Data masking pipeline (`Masker`)
- Masking transforms — faker, hash, redact, map, shuffle, date-shift, formula, passthrough
- Synthetic data generation (`DataGenerator`)
- Graph-mode pipeline runner (`run_graph` / `validate_graph` / `preview_graph`)
- Dataset analysis — STORM (`run_storm`) and FORECAST (`recommend`)
- Keyed deterministic masking (`make_key_resolver`)
- Connectors — CSV, fixed-width, database; plus the public Connector SDK
- Referential integrity management
- Public API contract (`__init__.py.__all__`)

## What does NOT live here

- CLI commands → `decoy`
- Web platform → `decoy-platform`
- Marketing site → `decoy-web`

## Architecture

Start with [`docs/product-flow.md`](docs/product-flow.md) for the developer-oriented product flow: STORM, FORECAST, graph execution, masking, generation, runtime context, examples, and diagrams.

Then see [`docs/architecture.md`](docs/architecture.md) for the internal component map (transforms, pipeline graph, generators, execution context) and where to keep reading.

## Public API

```python
from decoy_engine import (
    # Pipelines
    Masker,
    DataGenerator,

    # Execution context (caller-provided runtime)
    ExecutionContext, Logger, TelemetryClient,
    make_key_resolver,                    # keyed deterministic masking

    # Schema + license
    SchemaInspector, LicenseVerifier,

    # Validation
    validate_config,

    # Graph-mode pipelines
    validate_graph, run_graph, preview_graph,
    RunResult, PreviewResult,

    # Dataset analysis (STORM)
    run_storm,
    StormProfile, FieldStats, DetectorMatch, SentinelFlag,

    # Recommendations (FORECAST)
    recommend,
    ForecastReport, DisguiseRecommendation, FieldRecommendation, RiskFlag,

    # Faker provider registration
    register_faker_provider, unregister_faker_provider,

    # Exceptions
    DecoyError, ConfigError, PipelineValidationError,
    ConnectorError, ConnectorAuthError,
    LicenseError, LicenseExpiredError,
)
```

`ForgeError` is a deprecated alias for `DecoyError`, kept for one minor version.

The Connector SDK (`FileSource`, `FileSink`, `ConnectorConfig`, capability
constants, `TransientError` / `PermanentError`, etc.) is also re-exported
at the top level for convenience. Connector authors should import from
`decoy_engine.sdk` for the full surface — see
[`CONNECTOR_SDK_CONTRACT.md`](CONNECTOR_SDK_CONTRACT.md).

Everything in `decoy_engine.internal` is private and may change between minor versions.

## Dev setup

```bash
pip install -e .
pytest tests/
```

## License

Apache License 2.0 — see [LICENSE](LICENSE). Third-party notices in [NOTICE](NOTICE). Use of the "Decoy" name and marks is governed by [TRADEMARKS.md](TRADEMARKS.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Contributions require a DCO sign-off (`git commit -s`). Security issues: see [SECURITY.md](SECURITY.md).
