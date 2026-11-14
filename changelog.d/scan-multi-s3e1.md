Breaking: a namespace beginning `decoy-set-namespace-` on a column or a relationship now refuses (`config/namespace_placeholder_unset`); no shipped config uses the prefix.

### Added (detector descriptors, starter decision and scan_baseline, SCAN-MULTI S3-engine.1, 2026-11-14)

`profiling.recommendations.DETECTOR_DESCRIPTORS` holds one descriptor per detector id with the platform default and the starter choice `decoy scan` writes. `DEFAULT_STRATEGY_BY_DETECTOR` is now derived from it and its content is unchanged. `profiling._starter_inference.infer_starter_strategy` is the CLI's `_infer_strategy_for_column` at 8fba6af, ported as it was (the review copy was renamed: the retired product word became "The scan"; the decisions are ported exactly), including its read of `confidence_bucket` (a field `DetectorMatch` does not carry; it names it `confidence`) and its strict 0.1 ambiguity window. A parity fixture and its capture script sit in `tests/fixtures/scan_multi/`.

`PipelineConfig` gains the optional `scan_baseline: {path, schema}` block. `schema` is the closed tag `scan-set/v1`; unset it stays out of dumps and hashes.

The prefix `decoy-set-namespace-` is reserved for the placeholder a starter pipeline writes where a declared relationship names no namespace. `ColumnConfig.namespace` and `RelationshipConfig.namespace` refuse it with the new reason `config/namespace_placeholder_unset`, which names the field location and never the value.
