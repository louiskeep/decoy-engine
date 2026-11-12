Breaking: `decoy_engine.errors.CAPACITY_REASON_CODES` gains `out_of_core_faker_pool_budget_exceeded`, `out_of_core_reorder_budget_too_small`, `pool_exceeds_cache_budget` and `isolated_worker_out_of_memory`; a caller that treats the set as capacity now sees these four as capacity too.

### Changed (capacity reason-code set, RESULT-E-CAP, 2026-11-12)

The set holds seven codes. `decoy_engine.errors.CAPACITY_EXCLUSIONS` maps every other registered memory, budget, capacity or OOM code to the reason it is not a capacity verdict, including the two full-frame OOM-risk refusals. A test walks the registry so a new such code must join one of the two.
