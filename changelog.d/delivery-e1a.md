### Added (delivery config and plan_delivery, DELIV-E1a, 2026-11-12)

A pipeline config accepts `delivery: {mode: strict | best_effort}`, default `strict`, left out of the dump and the config hash while it is the default. `decoy_engine.delivery.plan_delivery(config, outcomes)` returns the delivery state of each table: under `strict` any failed table withholds the others, under `best_effort` each failed table drops itself and every table that depends on it, through `relationships` and through generate `reference` columns.
