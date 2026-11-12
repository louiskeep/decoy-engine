### Added (post-scan gate core, POSTSCAN-STREAM PS-7, 2026-11-12)

`config.post_scan_gate_applies(config)` is the one decision whether a run needs the post-scan gate:
a declared relationship or `post_scan.integrity: true`. The gate core
(`execution/pipeline/_post_scan.py`) checks resident outputs once per run: declared keys through
`check_keys`, the column suite through `run_post_mask` when integrity is on, and the decision through
`post_scan_verdict`. A blocking verdict, or a commit reached by a required gate that never ran,
raises `PostScanBlockedError` with an empty exception chain. No route calls the gate yet.
`profiling.MEMORY_LIMIT` is now public and is the one key-scan memory cap; the gate uses the smaller
of it and the run's out-of-core budget. `verification.postmask.blocked_error` builds the error from
its verdict.
