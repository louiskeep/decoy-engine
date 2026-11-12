### Added (declared key checks and five post-scan reason codes, POSTSCAN-STREAM PS-2a, 2026-11-12)

`decoy_engine.verification.postmask` gains `check_keys`, which checks every declared relationship
key on a run's output against the same facts over its inputs, and `keys_report`, which turns the
results into a value-free list in which each reason code is a `{"namespace", "code"}` mapping. A key blocks only on what the run introduced. A child key shared by
several relationships is checked once against the union of its parents. New reason codes, all
`post_scan`: `post_scan/primary_key_duplicates_introduced`, `post_scan/primary_key_nulls_introduced`,
`post_scan/foreign_key_orphans_introduced`, `post_scan/key_column_missing` and `post_scan/key_type_mismatch`. `decoy_engine.profiling` also
exports `passes_through`, the one rule that says whether a scan failure reaches the caller
unchanged; pre-scan and the key checks both use it. Nothing calls the checks yet.
