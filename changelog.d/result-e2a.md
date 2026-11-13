### Added (run-result table projection, RESULT-E2a, 2026-11-12)

`decoy_engine.run_result` exports `project_tables`, `ProjectedTables`, `CollectedFacts`, `DeliveryFacts` and `TableDelivery`. `project_tables` turns a pipeline config, the progress facts, the `ExecutionResult` and the delivery facts into one value-free entry per declared table: outcome, `rows_out` with the source that gave it, row-error count and delivery state, plus the run's `delivery` summary. Nothing writes the `decoy-run-result/v1` document yet; the builder follows.
