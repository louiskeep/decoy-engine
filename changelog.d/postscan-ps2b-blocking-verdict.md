### Added (one blocking verdict and key baseline status, POSTSCAN-STREAM PS-2b, 2026-11-12)

`decoy_engine.verification.postmask` gains `post_scan_verdict`, the one decision on whether a
post-scan report blocks, with the reason codes in a fixed order and the warnings that do not
block. It also gains `PostScanBlockedError`, `severity_counts` and the one value-free block
location. Each declared key result now says how its source side was counted (`baseline`:
`measured`, `no_source`, `key_column_missing`, `key_type_mismatch`, `error` or `not_run`), so a
zero from an unmeasured source is never read as a measured zero; source counts are `null` unless
they were counted. New reason code, `post_scan`: `post_scan/finding_blocked`.
`decoy_engine.reasons.reason_ref_payload` is the one serializer for a reason identity. The key
loader refuses a batch wider than its declared type with a value-free error, and the post-scan
runner lets a coded engine error through unchanged. Nothing calls the verdict yet.
