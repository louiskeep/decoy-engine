### Added (key facts and batch inputs, POSTSCAN-STREAM PS-1, 2026-11-12)

`decoy_engine.profiling.key_facts` counts parent rows, null-key rows, duplicate excess and per-child
orphans over `KeyBatches`, the same counts the pre-scan relationship check reports. The lazy Parquet
and CSV sources take a `columns` argument on `open_batches` that narrows the batches and leaves the
file facts whole.
