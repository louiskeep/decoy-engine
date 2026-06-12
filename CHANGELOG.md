# Changelog

All notable changes to the `decoy-engine` PyPI distribution land here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Engine versions are independent of `decoy-cli`; the CLI declares the
minimum engine version it was tested against via its
`decoy-engine>=X.Y` dependency pin.

## [Unreleased]

### Added (capability gaps, 2026-06-12)

- **Multi-parent FK support** (WS5). A child column-tuple may now declare
  FK relationships to multiple parent tables (polymorphic/shared-domain
  keys). The child resolves through each parent's source->masked map in
  DECLARED CONFIG ORDER, first hit wins; a row is an orphan only when
  absent from every parent map. Per-edge orphan policies on a shared
  child tuple must agree (new error `orphan_policy_conflict`).
  BEHAVIOR CHANGE: the S2-era `multi_parent_fk_unsupported` rejection is
  gone -- configs it used to reject now compile and run.

- **NER-backed text_redact** (WS2). New opt-in `ner` key on text_redact's
  provider_config (`ner: true` or `ner: {model: ..., entities: [...]}`)
  detects person names and locations via spaCy NER -- the two categories
  the regex span catalog deliberately cannot cover -- and merges those
  spans into the same leftmost-longest overlap resolution as the regex
  detectors (`iter_spans` gains an additive `extra_spans` kwarg). New
  optional extra `decoy-engine[ner]`; the model installs separately via
  `python -m spacy download en_core_web_sm`. New compile check row 13
  (`text_redact_ner_available`) rejects an ner-enabled config when spacy
  or the model is missing on this host (checks_passed grows 12 -> 13).
  Off by default; the no-ner path is byte-identical to before.

- **`statistical` generate type** (WS3). Samples synthetic columns from a
  `distribution-snapshot/v1` artifact (the existing quality/snapshot
  schema is the fitted model): histogram inverse-CDF for numeric
  (Devroye), weighted top-k for categorical with
  `other_mode: redistribute|emit`, year-bin sampling for datetime, and
  `condition_on` declared-pair conditional sampling from the snapshot's
  joint contingency tables (synthpop-style). Categorical columns require
  the explicit `allow_real_categories: true` disclosure opt-in (snapshot
  top_values carry real source values; DP is out of scope for v1).
  Per-row seeded (chunk-safe), pure-Python sampling (bit-stable).
  New compile check `statistical_columns` (row 12) validates config +
  artifact at validate time; `checks_passed` grows 11 -> 12.

- **`decoy_engine.unmask_pipeline` detokenization API** (WS1): inverts
  fpe columns of a masked output under the same config; per-column
  reversibility report. See the fpe re-keying entry under Changed.

- **Mimesis backend adoption completed** (closes the S7 evaluation that was
  built but never run). With the `mimesis` extra installed, five person
  providers (`person_name`, `person_first_name`, `person_last_name`,
  `person_full_name`, `person_email`) now bind to MimesisAdapter, 17-55x
  faster than Faker with checks 1-6 parity green. Without the extra,
  behavior is byte-identical to before. The other 6 candidates were
  rejected with evidence (speed or length/distribution parity); see
  `docs/mimesis-adoption-2026-06-12.md`. The extra is now pinned
  `mimesis>=19.0,<20` (evaluated on 19.1.0), and a seeded CI tripwire
  re-runs gating parity for adopted providers.

### Fixed (audit remediation, 2026-06-12)

Findings from the 2026-06-11 full-codebase audit. Behavior changes are
called out explicitly.

- **STORM residual-PII oracle is now source-aware** (audit C1, Critical;
  + H6). A column whose mask silently failed (output positionally
  identical to source) previously reported `severity='info'` on
  faker/formula/categorical/reference/date_shift strategies — a real
  leak shipped green. Detector-flagged columns are now compared
  positionally against the source frames and severity escalates to
  `fail` (substitution strategies at >=50% identity, value-reuse
  strategies at full identity, unconfigured columns at >=50% on a
  high-confidence hit). BEHAVIOR CHANGE: pipelines with partially-failed
  masks or verbatim-preserved unconfigured PII columns now exit 4 at
  `decoy storm integrity`. Shuffle's detector-hit baseline moved
  warning -> info (expected outcome) with a full-identity fail backstop.
  `ResidualPIIFinding` gains additive `source_identity_rate` +
  `source_compared` fields (schema stays `storm-post-mask/v1`).
- **text_redact null preservation** (audit H1): `pd.NA`/`pd.NaT` no
  longer leak into output as the literal strings `'<NA>'`/`'NaT'`.
- **composite_custom slot mapping** (audit H2): non-alphabetical bundle
  declarations no longer write every generated value into the wrong
  column on the pool/sampler path. Duplicate bundle column names are
  rejected (`composite_custom_duplicate_columns`). First composite
  pandas<->polars parity coverage added.
- **Pool build race** (audit H3): concurrent cache misses on the same
  deterministic identity now build exactly once (per-identity locks);
  divergent pool instances can no longer break determinism under the
  platform's async runner.
- **New compile check row 11, `non_poolable_provider_with_pool_backend`**
  (audit H5): `strategy: faker` on a poolable=False provider (e.g.
  `uuid`) is rejected at plan compile instead of crashing at run.
  BEHAVIOR CHANGE: `checks_passed` grows 10 -> 11 (no-profile 7 -> 8);
  consumers asserting the exact check set must update.
- **New public API `run_config_only_checks(config)`**: the profile-free
  compile-check subset for config-only callers (`decoy validate`).
- **Disguises carry a required dated `version`** (product rule: a
  disguise is the canonical legal artifact for its regulation; derived
  templates pin the version). All 8 bundles stamped `2026-06-12`.
  BEHAVIOR CHANGE: third-party disguise YAMLs without `version` no
  longer load.
- **HIPAA Safe-Harbor item Q is now honestly covered** (audit M2):
  biometric_id name hints gained photo/face terms (photo, photo_url,
  face_id, headshot, ...) so photo path/URL columns route to redact;
  the disguise states explicitly that image FILE CONTENT is out of
  scope. Stale header comments that disagreed with the disguise's own
  field_rules were corrected.
- **Relationship graph dedupes duplicate edges** (audit M1): indegree
  and parents_of/children_of bookkeeping no longer inflate when a
  relationship is declared twice.
- **Stable dtype labels across pandas majors** (audit M5/BL-2): pandas-3
  default-inference labels (`str`, `datetime64[us]`) normalize to their
  historical values (`object`, `datetime64[ns]`) in ColumnProfile and
  distribution snapshots, so USER-HELD snapshot baseline digests minted
  under pandas 2.x remain valid. pandas is now capped `>=1.5.0,<4`.
- **numexpr fallback surfaced** (audit L1): the silent numexpr -> python
  engine fallback on extension-array dtypes is logged through the
  engine logger instead of an unmonitored RuntimeWarning.
- **Capability matrix lists all 34 providers** (audit M3/BL-9): the
  generator walks the live registry instead of the Faker-only _CATALOG.
- New Hypothesis property suite `tests/property/test_mask_invariants.py`
  (9 properties x 400 examples) pinning null-preservation, determinism,
  namespace isolation, and per-strategy structural invariants.

### Added

- **Generated engine capability matrix** (`docs/capability-matrix.md`, emitted by
  `scripts/gen_capability_matrix.py`). Reads the live registries (mask + generate
  strategies, synthetic providers, connectors + capabilities, STORM detectors,
  disguises) and writes a correct-by-construction reference. A `tests/sentry/
  test_capability_matrix.py` drift guard fails CI when a registry changes without
  the matrix being regenerated, so a new capability cannot ship without its docs.

### Changed

- **Repository visibility flipped to public** (2026-06-02). Aligns
  with the OSS launch plan (memory: `OSS CLI launch` PO lock
  2026-06-01: "publish free Apache-2.0 decoy-cli + decoy-engine on
  PyPI"). Trigger for the flip: the `release-smoke.yml` workflow in
  the sibling `decoy` CLI repo needs to clone the engine from
  `git+https://github.com/louiskeep/decoy-engine@main` during the
  pre-publish window; cross-repo `git clone` of a private repo from
  inside a public-workflow runner fails with `could not read Username`
  (no TTY for the auth prompt). Making the engine public unblocks
  the cross-repo clone without introducing a PAT secret.
- Pre-flip pre-flight (working-tree only, 2026-06-02): LICENSE +
  NOTICE present and correct (Apache-2.0); no tracked secrets
  (AKIA*, sk_live_, password=, api_key=, private_key=); no tracked
  .env / credentials files; fixture CSVs are faker-generated
  synthetic data; logs are gitignored. Git history was not scanned
  for redacted secrets; if any historical leak surfaces post-flip,
  `git filter-repo` + force-push + immediate credential rotation is
  the recovery path.

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
