# Engine Developer Help

Short notes for working in the `decoy-engine` repo.

## Build and test

    pip install -e .
    pytest tests/unit
    pytest tests/integration/golden

Run the narrowest useful test scope (modules touched plus nearest neighbors). Do not run the full pytest suite by default; it covers benchmark and perf fixtures that take long enough to be wasteful for routine edits.

## Common tasks

- Add a public export: declare it deliberately in `src/decoy_engine/__init__.__all__`.
- Add a masking strategy: implement `StrategyHandler` under `execution/_strategies/`, wire it into the Pandas adapter dispatch (and Polars counterpart if you target both substrates), and add unit + golden coverage.
- Add a provider: register it in `providers_v2/_registry.py`. The planner closed-checks unknown providers with `code=unknown_provider`.
- Add a connector: inherit from `FileSource` / `FileSink` in `sdk.py`, declare capabilities, and ship in-tree under `connectors/` or as an external package via the `decoy.connectors` entry point.

## Where to look

See [CODEMAP.md](CODEMAP.md) for the directory map and the "Where Do I Find" pointer table. The canonical end-to-end caller shape is `tests/integration/golden/test_execution_e2e.py`.

---

Full engine dev-help guide lives in the commercial platform repo.
