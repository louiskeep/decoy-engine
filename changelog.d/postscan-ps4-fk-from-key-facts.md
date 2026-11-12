### Changed (FK findings from the key facts, POSTSCAN-STREAM PS-4, 2026-11-12)

The post-scan report's `fk_preservation` findings now come from the declared keys' facts instead of
a pandas orphan count with rate thresholds. A finding fails when the run introduced orphans, warns
when every output orphan was already in the source or the key carries only warning codes, and is an
`error` only when the key could not be counted. One introduced orphan now fails at any table size.
Composite keys fill the new `parent_columns` and `child_columns` tuples, and `source_orphan_count`
is null when the source side was not counted. `run_post_mask` takes an optional `keys=`. A key
result's `source` is now `None` whenever its baseline was not counted. The report tag stays
`post-scan-integrity/v2`.
