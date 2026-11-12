### Added (delivery reason codes, DELIV-E1b, 2026-11-12)

Four reasons in the `decoy` namespace, all pending release: `decoy/delivery_withheld` and `decoy/delivery_dependent_dropped` (decisions), `decoy/delivery_pointer_moved` (failure, retry the operation) and `decoy/delivery_rolled_back` (warning). Each `TableDelivery` from `plan_delivery` now has a `code`: the failure cause for a failed table, `decoy/delivery_withheld` or `decoy/delivery_dependent_dropped` for a withheld or dropped one, and `None` for a delivered one.
`plan_delivery` raises `PipelineConfigError` for an outcome that is not a failure reason code.
