# Changelog

All notable changes to the `decoy-engine` PyPI distribution land here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Engine versions are independent of `decoy-cli`; the CLI declares the
minimum engine version it was tested against via its
`decoy-engine>=X.Y` dependency pin.

## [Unreleased]

### Added (capability gaps, 2026-06-12)

- **Chunked mask execution** (WS4). New public API
  `run_mask_pipeline_chunked(config, chunks, *, table, engine_version)`
  streams one table through the engine chunk-by-chunk for inputs too
  large for memory. The contract is byte parity with a full-frame run,
  honest because chunked mode only admits VALUE-KEYED strategies (hash,
  fpe, redact, truncate, text_redact, date_shift, bucketize,
  passthrough -- each output cell a pure function of its input cell +
  config + seed). `check_chunked_compatibility` rejects shuffle,
  composite/nested, faker/categorical (pool state; deferred),
  relationship configs, and generate tables with typed codes. The plan
  compiles once from a first-chunk profile; every chunk runs the
  standard pandas adapter, so parity holds by construction.

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

### Fixed (remediation batch 1, 2026-06-26)

Targeted correctness and hardening fixes from the F-series findings register
(`docs/remediation-source.md`). Behavior changes are called out explicitly.

- **Typed `MaskKeyDerivationError` for FPE and date-shift key failures** (F15).
  `transforms/fpe.py` and `transforms/date_shift.py` previously raised a bare
  `RuntimeError` when the per-column key derivation failed, which escaped an
  upstream `except DecoyError` handler. Both now raise
  `MaskKeyDerivationError(DecoyError, code="mask.key_derivation_failed")`, so
  the failure is catchable at the engine boundary like every other typed engine
  error. The `.strategy` attribute names the originating strategy (`"fpe"` or
  `"date_shift"`). `MaskKeyDerivationError` is exported from
  `decoy_engine.errors`. BEHAVIOR CHANGE: callers catching bare `RuntimeError`
  on these paths must update to `DecoyError` or `MaskKeyDerivationError`.

- **Deterministic shuffle binds the column name into its derivation source**
  (F4). Before this fix, two shuffle columns sharing a namespace derived their
  permutation from `derive(job_seed, namespace, b"")`, so both received the
  same permutation and permuted in lockstep. That re-links values across columns
  that masking is meant to decouple: a privacy regression. The source is now
  `derive(job_seed, namespace, column_name.encode("utf-8"))`, so each column
  draws a distinct permutation. BEHAVIOR CHANGE: deterministic-shuffle output
  shifts for all columns. This fix bundles into the upcoming
  `SEED_PROTOCOL_VERSION` v6 bump (not yet bumped); do not assume v6 has landed.

- **`vault: true` fails at compile when `cryptography` is not installed**
  (F14a). A vaulted column without the `vault` extra (`cryptography` package)
  previously reached vault-write time hours into a run before failing. The plan
  compiler now rejects it immediately with
  `PlanCompileError(code="vault_requires_cryptography")`. Install the extra with
  `pip install 'decoy-engine[vault]'`. BEHAVIOR CHANGE: configs with
  `vault: true` that previously ran until vault-write now fail at compile.

- **NER model version mismatch raises at run time** (F14b). When
  `text_redact` is configured with `ner: true`, the spaCy model version is
  stamped into the plan at compile time. If the installed model version differs
  at run time, the engine now raises
  `StrategyError(code="ner_model_version_mismatch")` before any redaction
  runs, rather than silently producing different redactions for the same config
  and seed. Pin the model version or recompile the plan after a model update.
  Plans compiled before this version have no stamped version and skip the guard.

### Security / Changed (vault hardening F13, 2026-06-26)

- **Vault format bumped to `decoy-vault/v2`; per-chunk streaming encryption**
  (F13). BEHAVIOR CHANGE: vault files written by this engine use the new
  `decoy-vault/v2` format (magic `DCYVAULT2\n`) and are not readable by any
  prior engine version. v1 vault files are not readable by this engine. This is
  a pre-GA hard cutover: no vaults exist in the wild, so no migration is
  required at this point. The forever-readable rule begins at the first
  in-the-wild v2 vault.

  Format change: the file now contains an unencrypted JSON header (`format`,
  `seed_protocol_version`, `ambiguous_dropped`, `chunk_rows`, `chunk_count`)
  followed by a sequence of length-prefixed Fernet tokens, one per bounded
  chunk of up to 65 536 sorted entries.

  Privacy fix: `VaultWriter.write` now serializes and encrypts one bounded
  chunk at a time (F13). The previous implementation serialized the entire
  source-value table into a single Parquet buffer before encrypting it. That
  created a window where the full plaintext source-value table sat in heap
  as one unencrypted blob. The new path drops each chunk's plaintext
  immediately after encrypting it; the full-table plaintext blob is never
  materialized.

- **New typed error `vault_protocol_version_mismatch`** (F13). `load_vault`
  reads the unencrypted v2 header before any decryption attempt. If the
  header's `seed_protocol_version` does not match the running
  `SEED_PROTOCOL_VERSION`, it raises
  `VaultError(code="vault_protocol_version_mismatch")` with a message naming
  both versions. Previously a cross-version vault would surface as an opaque
  `vault_key_mismatch` (because the protocol version byte is mixed into the
  derived vault key). The new code is distinct from `vault_key_mismatch` (wrong
  seed, correct version). Cross-version unmask remains unsupported; F13 makes
  the error diagnosable. `unmask_pipeline` surfaces this code in its per-column
  error list alongside the existing vault error codes.

- **Single shared seed validator** (F5). `plan/_seed.py` is a new internal
  module containing `_normalize_job_seed` and `_normalize_job_seed_int`. The
  pipeline profile path (`execution/_pipeline.py`), the plan compiler
  (`plan/_compile.py`), and generation (`generation/synthesize.py`) all route
  through it. Previously the profile path accepted a bool seed
  (`isinstance(True, int)` is True in Python), so `seed: true` in YAML would
  seed `random.Random(True) == random.Random(1)` on the profile path while
  later being rejected by the compiler, producing a non-deterministic profile.
  Now rejected uniformly across all paths. BEHAVIOR CHANGE: a non-numeric seed
  passed to the public `generate_tables` now raises `PlanCompileError`
  (code `seed_not_numeric`) instead of `ValueError`; callers catching
  `ValueError` on that path must update to `PlanCompileError` or `DecoyError`.
  BEHAVIOR CHANGE: a config with no `seed` (or `seed: null`) now defaults to
  `0` on the profile path too, so seedless profiling is deterministic and the
  former "called without a seed" warning no longer fires; callers that relied
  on seedless runs drawing fresh entropy must set an explicit random seed.

- **Shared-state RNG removed from bare `MASK_GLOBALS`** (F16a). The three RNG
  bindings (`randint`, `choice`, `random`) are no longer present in the base
  `MASK_GLOBALS` scope. They were bound to the module-global `random._random`
  instance, so two formula strategies in the same job shared process-global
  random state: column B's output depended on column A's execution order and
  was non-deterministic across runs. The only supported RNG path is
  `make_mask_globals(rng)`, which binds a per-formula isolated
  `random.Random(formula_seed)`. BEHAVIOR CHANGE: a formula that calls
  `randint`, `choice`, or `random` against the bare scope now raises
  `InvalidExpression` (undefined name) instead of silently reading shared state.

### Fixed (generation determinism v6 rewrite, 2026-06-26)

Resolves findings F2 and F3 from `docs/remediation-source.md`. References the
F4 shuffle fix (shipped earlier on its own branch) that also rides the v6 bump.

- **`SEED_PROTOCOL_VERSION` bumped 5 to 6** (F2/F3). BEHAVIOR CHANGE: all
  synthetic-generation output and all masked output shift at v6. This is a
  pre-GA hard cutover; no plans or vaults exist in the wild, so no migration is
  required at this point. A v5 vault over a synthetic column cannot be unmasked
  under v6 (the regenerated seed diverges). The explicit cross-version vault
  protocol guard (error on mismatch instead of silently returning wrong values)
  is deferred to the vault-hardening work (F13); see
  `docs/compatibility-contract.md`.

- **Generate-path seed widened from 32 bits to 256 bits** (F2). The legacy
  `synthetic_column_seed` helper truncated every HKDF-derived key to 4 bytes
  (`int.from_bytes(b[:4], "big")`), leaving a 32-bit keyspace. The replacement
  `GenDeriveContext` (`generators/derivation.py`) resolves a full 32-byte
  column root via `derive_key("gen:" + fingerprint)`, consuming all 256 bits.
  `GenDeriveContext` is the public replacement; `synthetic_column_seed` is
  removed.

- **Per-row `seed + i` arithmetic replaced with per-family HMAC derivation**
  (F3). The old `column_seed + i` per-row loop meant column A (base `S`) and
  column B (base `S+1`) produced row-shift-identical seed sequences. The new
  `row_int(family, i)` method on `GenDeriveContext` derives each row's integer
  via a version-mixed HMAC keyed to the column root and RNG family, so adjacent
  columns never share seeds under any row shift.

- **Three RNG families now draw from disjoint sub-keys** (F3). `py`
  (`random.Random`), `np` (`numpy.random.default_rng`), and `faker`
  (`Faker.seed_instance`) each receive a distinct family key derived from the
  column root. Before v6, all three were seeded from the same truncated integer.

- **Generation mixes the protocol version byte into its HMAC** (F2/F3). The
  new `_gen_hmac` helper in `generators/derivation.py` mirrors the mask-path
  envelope in `determinism/_derive.py`: both mix `SEED_PROTOCOL_VERSION` into
  the HMAC input. The protocol version is now the single compatibility knob
  across both determinism roots; a bump re-keys both masked output and
  synthetic-generation output together.

- **V2 null-injection path unified to V1 numpy-vectorized mask** (F2/F3).
  `generation/synthesize.py` (`_apply_null_probability`) previously used a
  per-row Python `random.Random` reseed loop, which converged to the correct
  null fraction but did not produce the same null pattern as V1 (which uses
  `numpy.random.default_rng(column_seed).random(n) < null_prob`). Both engines
  now use the same numpy vectorized draw seeded from `GenDeriveContext.base_int
  ("np")`, so null-probability columns are byte-identical across the two
  generation engines.

- **New tests** (F6): subprocess byte-identity gate for `generate_tables`
  (`tests/unit/generation/test_synthesize_determinism.py`), cross-column seed
  independence tests, and a full `GenDeriveContext` contract suite
  (`tests/unit/generators/test_gen_derive_context.py`).

### Added

- **Generated engine capability matrix** (`docs/capability-matrix.md`, emitted by
  `scripts/gen_capability_matrix.py`). Reads the live registries (mask + generate
  strategies, synthetic providers, connectors + capabilities, STORM detectors,
  disguises) and writes a correct-by-construction reference. A `tests/sentry/
  test_capability_matrix.py` drift guard fails CI when a registry changes without
  the matrix being regenerated, so a new capability cannot ship without its docs.

### Added (F1 compatibility-corpus expansion, 2026-06-26)

- **`distribution-snapshot/v1` added to the cross-version compatibility corpus**
  (F1). The corpus (`tests/integration/compat_corpus/`) previously covered only
  `decoy-vault/v2`. It now also freezes a synthetic `distribution-snapshot/v1`
  artifact and verifies it through the real `load_spec` reader (numeric,
  categorical, and conditioned-joint branches) on every CI run. A schema-version
  tamper bite-test confirms the guard fires for this artifact kind. Every
  corpus artifact now stamps `seed_protocol_version`. Corpus version bumped to 2.

### Added (BF4 post-mask tests, 2026-06-26)

- **TDD synthetic fixture test suites for post-mask check runners** (BF4).
  Two pure-engine test modules exercise the behavioral contracts of the
  residual-PII scanner and FK-preservation checker, both shipped as part of
  Reframe-A. `tests/unit/storm/test_bf4_residual_pii.py` (12 test scenarios,
  374 LOC) covers failed-hash detection (S1), successful masking (S2),
  unconfigured PII columns (S3), redact failures (S4), multi-column mixtures
  (S5), non-PII columns (S6), and a security invariant asserting that report
  findings never leak raw cell values. `tests/unit/storm/test_bf4_fk_preservation.py`
  (15 test scenarios, 417 LOC) validates consistent masking (F1), orphan
  detection on inconsistent hashing (F2), null FK handling (F3), multi-child
  independence (F4), namespace routing (F5), composite FK tuples (F6), missing-
  parent error gracefully handled (F7), and the security invariant that findings
  carry no raw key material.

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
