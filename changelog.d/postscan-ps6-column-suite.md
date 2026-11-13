### Added (post-scan column suite on facts, PS-6, 2026-11-13)

`run_post_mask_tables` runs the post-scan report over tables held as batches: residual PII and policy validation are computed from exact per-column counts (`residual_pii_findings`, `policy_findings`), and the declared key facts come from the caller. `run_post_mask` keeps its signature and builds the same facts from its frames, so its findings do not change. Nothing in a run calls the tables entry yet.

A frame whose column names are integers now gets findings (the column named as text); the old runner reported one error for the whole check. A column that cannot be compared or converted (mixed int and str, Decimal with float, list or map cells) is isolated: the other columns and tables keep their findings, and a policy-validation error names the class only.
