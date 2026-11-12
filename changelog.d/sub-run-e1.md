### Added (typed subset requests and one input seam, SUB-RUN E1, 2026-11-12)

`decoy_engine.subset` gains `select_subset`, which picks the subset rows of resident Arrow tables or
lazy sources (`LazySource`, including CSV) and returns a `SubsetSelection`, plus `SubsetRequest`,
`SubsetScope`, `scope_for` and `subset_request_from_config`. `run_subset_preflight` also accepts
`pa.Table` and `LazySource` values. A table no seed reaches passes through whole. Keys keep their
real types, so a text CSV key against an integer Parquet key is still refused with
`subset.subset_relationship_type_mismatch`. `plan_subset`, `run_subset` and the `decoy subset`
output are unchanged, and no reason code is added.
