# Changelog

All notable changes to the `decoy-engine` PyPI distribution land here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Engine versions are independent of `decoy-cli`; the CLI declares the
minimum engine version it was tested against via its
`decoy-engine>=X.Y` dependency pin.

## [Unreleased]

### Added

- OSS.3 packaging metadata: PyPI Trove classifiers (Python 3.10/3.11/3.12,
  Apache-2.0 license, Topic taxonomy), keywords (data-masking,
  synthetic-data, faker, mimesis, pandas, polars, etc.), and the
  `[project.urls]` block (Homepage, Repository, Documentation, Issues,
  Changelog) surfaced on the PyPI sidebar.
- This `CHANGELOG.md` itself.

## [0.1.0] - 2026-06-02

The first publishable cut of the engine. Not yet pushed to the real
PyPI index; first publish lands with OSS.7.

### Added

- **FC-1 (mixed mask + generate)**: a single PipelineConfig can now
  declare both mask-kind tables (with `columns:`) and generate-kind
  tables (with `generate_columns:`) in one config. The top-level
  `mode:` discriminator is gone; per-table kind is inferred from
  `columns` vs `generate_columns` presence. The new
  `decoy_engine.run_pipeline` entry sequences generate -> merge ->
  mask in one call and returns an `ExecutionResult` whose
  `table_kinds: dict[str, "mask" | "generate"]` carries the per-table
  classification for manifest stamping.
- **FC-2 (self-FK end-to-end verification)**: golden fixture
  `tests/fixtures/golden/self_fk/` (50-row employees table with
  manager_id self-FK + 5 root nodes + 1 orphan) plus 4 e2e cells +
  1 invariant cell + the degenerate-case `parent_col == child_col`
  cycle-rejection pin. No engine source code change; the verification
  doc's trace proved correct.
- `classify_table_kinds(config)` top-level export: returns
  `{table_name: "mask" | "generate"}` for every table in the config.
  Used by the platform's preview helper to slice mask sources + cap
  generate row_counts independently.

### Fixed (from QA review docs/qa/review-2026-06-02-fc1-mixed-mode-engine.md)

- Finding 1 (HIGH): `_topo_sort` in `generation/synthesize.py` used
  recursive Python DFS. Reference chains >~1000 generate tables hit
  the default recursion limit and crashed with `RecursionError`.
  Replaced with iterative DFS that uses an explicit (node, parent
  iterator) work stack.
- Finding 2 (HIGH): the validator at
  `config/_pipeline.py::_reference_graph_valid` admitted a
  generate-child -> mask-parent reference (the engine docstring said
  runtime resolution was V2.1), but `synthesize.py::generate_tables`
  raised a plain `ValueError` on this case at runtime, which the
  platform's typed-exception handler did not catch -> the job hung
  in `running` forever. Post-fix the validator rejects at submit time
  with a "deferred to V2.1" message.

[Unreleased]: https://github.com/louiskeep/decoy-engine/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/louiskeep/decoy-engine/releases/tag/v0.1.0
