# 18. Evidence, reasons and compatibility

## What it covers

What a run reports about itself and what the engine promises to keep stable:
`ExecutionResult` and its `quality_metrics`, the reason-code registry
(`decoy_engine.reasons`, `reason_from_exception`), progress events, the
`decoy-run-result/v1` record, the public API (`decoy_engine.__all__`), and the
pre-GA / GA switch (`RELEASE_PHASE`).

## Expected behaviour

- Every failure the engine raises on purpose carries a registered `(namespace, code)`.
  Codes are append-only: none disappears or changes meaning.
- `reason_from_exception(exc)` never raises. An exception it does not recognise becomes
  `decoy.unexpected_error` with retry advice `unknown`. It never reads the message text.
- Each reason carries a retry class (`never`, `configuration_repair`,
  `environment_repair`, `retry_operation`, `restart_attempt`, `unknown`) and an outcome
  category. The engine reports the category; the CLI owns exit codes.
- A reason record holds approved identifiers only: no path, connection string or value.
- Progress events are value-free and the same shape on every route: a resolve phase, a
  route event, per-table phases with row counts, rate samples and a terminal event.
  Output bytes never depend on whether a sink is attached.
- Evidence under `quality_metrics` holds counts and identifiers, never values.
- Only names in `decoy_engine.__all__` are public. Anything under `internal/` is not.
- `RELEASE_PHASE` is the one switch between pre-GA (breaking changes are hard deletes)
  and GA (the compatibility contract binds).

## Checks

| Id | Run this | What should happen |
|---|---|---|
| EVID-01 | Trigger ten different deliberate failures from sections 01 to 17 and pass each to `reason_from_exception`. | Each returns a record with a registered namespace and code, not `decoy.unexpected_error`. |
| EVID-02 | Pass a plain `ValueError("boom 123-45-6789")` to `reason_from_exception`. | `decoy.unexpected_error`, retry class `unknown`. The message text is not in the record. |
| EVID-03 | For the ten failures in EVID-01, read `retry_class` and `outcome`. | Config mistakes are `configuration_repair`. A missing secret is `configuration_repair`. Findings are `never`. Record any that look wrong. |
| EVID-04 | Compare the current registry against the oldest pinned reason manifest fixture. | Every code in the old manifest still exists with the same meaning, or is a tombstone with a replacement link. |
| EVID-05 | Attach a `CallbackSink` to a chunked run, an out-of-core run and a full-frame run. | Each emits a resolve phase, one route event, table phases and one terminal event. Sequence numbers increase. Row counts add up to the table sizes. |
| EVID-06 | Run the same job with and without a progress sink. | Byte-identical outputs. |
| EVID-07 | Serialise every progress event and the whole `quality_metrics` of a run over data with marker values, then search for the markers. | None present. |
| EVID-08 | A run that fails: read the terminal progress event. | It is an `error` event carrying the run's first causal reason. An `ok` event carries none. |
| EVID-09 | Import every name in `decoy_engine.__all__`; then check the README's API table against it. | Every name imports. Every symbol the README shows exists (`profile_columns`, `validate_config`, `run_mask_chunked` and the rest). |
| EVID-10 | Read `decoy_engine.RELEASE_PHASE` and `is_pre_ga()`. | Consistent with each other and with what the release notes say about the current phase. |
| EVID-11 | Build a `RunResult` for a succeeded, a failed and a findings-only run and derive the outcome. | `derive_outcome` gives the right status for each. The record validates against `decoy-run-result/v1` and holds no path outside the allowed relative form. |

## Open points

- The README's install section says `pip install decoy-engine`, the engine `pyproject`
  declares Apache-2.0, and the CLI's `docs/install.md` says the engine and CLI ship
  under BUSL-1.1 from a signed bundle with pip arriving later. One of these is stale.
  Settle it before QA signs off anything about install or licence.
- The README quickstart passes `engine_version="0.1.0"` while the package is at 0.22.0.
  Confirm what `engine_version` is meant to be and fix the example.
- `examples/runnable_demo.py` imports from private modules
  (`decoy_engine.config._pipeline`). The shipped example should use the public API.
- The engine docs say there are seventeen mask strategies; the generated capability
  matrix lists 24. `tokenize`, `top_code`, `group_key`, `grouped_series` and
  `windowed_date` are missing from the strategy catalog.
- The compatibility contract's frozen surfaces (artifact formats, vault, determinism,
  API) only bind at GA. Decide which of them V1 QA treats as already frozen.
