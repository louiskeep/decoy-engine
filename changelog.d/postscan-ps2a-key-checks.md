### Added (declared key checks and five post-scan reason codes, POSTSCAN-STREAM PS-2a, 2026-11-12)

`decoy_engine.verification.postmask` gains `check_keys`, which checks every declared relationship
key on a run's output against the same facts over its inputs, and `keys_report`, which turns the
results into a value-free list. A key blocks only on what the run introduced. A child key shared by
several relationships is checked once against the union of its parents. New reason codes, all
`post_scan`: `primary_key_duplicates_introduced`, `primary_key_nulls_introduced`,
`foreign_key_orphans_introduced`, `key_column_missing` and `key_type_mismatch`. Nothing calls the
checks yet.
