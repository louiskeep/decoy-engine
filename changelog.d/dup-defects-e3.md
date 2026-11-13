### Fixed (chunked when guard accepts large_string, DUP-DEFECTS E3, 2026-11-13)

A seeded categorical or non-deterministic faker column under `when:` now runs with `--chunked` on a parquet source whose text columns are `large_string`. The per-chunk guard refused them with `chunked_categorical_nondeterministic_when_not_supported` and a widening reason that does not apply to `large_string`. A new private leaf, `decoy_engine._arrow_types`, holds the `is_text` and `is_text_or_null` predicates.
