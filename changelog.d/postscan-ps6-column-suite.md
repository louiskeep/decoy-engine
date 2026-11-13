### Added (post-scan column suite on facts, PS-6, 2026-11-13)

`run_post_mask_tables` runs the post-scan report over tables held as batches: residual PII and policy validation are computed from exact per-column counts (`residual_pii_findings`, `policy_findings`), and the declared key facts come from the caller. `run_post_mask` keeps its signature and builds the same facts from its frames, so its findings do not change. Nothing in a run calls the tables entry yet.
