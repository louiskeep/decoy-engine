# Changelog

All notable changes to the `decoy-engine` PyPI distribution land here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Engine versions are independent of `decoy-cli`; the CLI declares the
minimum engine version it was tested against via its
`decoy-engine>=X.Y` dependency pin.

## [Unreleased]

### Added (BF1 distribution-fidelity surfacing, 2026-06-26)

- **Opt-in fidelity report on the run path** (BF1, engine slice). The
  already-built `decoy_engine.quality` metrics
  (`compute_quality_report` -> diagnostic + value-identity fidelity +
  shape fidelity) are now wired into `run_pipeline` behind a new
  default-OFF kwarg `fidelity_report: bool = False`, with an optional
  `now_iso` passthrough for deterministic `generated_at` stamping. When
  ON, a per-mask-table `quality-report/v1` block is attached under
  `ExecutionResult.quality_metrics["fidelity_reports"]` (the free-form
  dict already plumbed to the platform manifest). It is REPORT-ONLY: a
  low fidelity score never fails the job. First slice is mask-kind
  tables, marginal-only (no joint columns); generate-kind tables are
  skipped. SECURITY: only the assembled, aggregate-only report is
  emitted (column names + kinds + scores); the intermediate
  distribution snapshots that carry category labels / raw values are
  consumed but never attached, pinned by a guard test. Default-OFF
  leaves the hot path byte-for-byte unchanged, so golden / compat-corpus
  fixtures do not move; no persisted-format or seed-protocol bump.
### Added (internal module hygiene, 2026-06-26)

- **`decoy_engine.identifiers` sub-import namespace** (F9). The identifier
  families (Ein/Mrn/Ndc/Npi/Ssn adapters, domains, and validators, plus
  `IdentifierError`/`IdentifierFormatError`) are now addressable via a focused
  module: `from decoy_engine.identifiers import EinValidator`. The top-level
  `decoy_engine` package keeps all 21 symbols as module bindings for backward
  compatibility, but they are no longer part of `decoy_engine.__all__`; the
  canonical import path is `decoy_engine.identifiers`. `BundlePool`,
  `PoolCache`, `CompositeAddress`, and `composite_city_state_zip` are similarly
  removed from `__all__` while keeping their top-level bindings.

### Changed (internal module splits, 2026-06-26)

No behavior or API change. Modules that exceeded the 600-LOC orchestration cap
were decomposed into private helpers. All external import paths are preserved.

- **`generators/columns.py`** (1333 LOC) split into `_distribution.py`
  (distribution-snapshot sampler methods, F11a) and `_formula.py` (formula
  column evaluation methods, F11a); both are private mixins folded into
  `ColumnGenerator` via multiple inheritance.
- **`storm/detectors.py`** (1356 LOC) split: regex catalog extracted to
  `_patterns.py` and detector validation helpers extracted to `_validators.py`
  (F11b).
- **`storm/profiler.py`** (999 LOC) split: column-shape classification helpers
  extracted to `_classification.py` and distribution-snapshot builders extracted
  to `_distributions.py` (F11c).
- **`plan/_compile.py`** (845 LOC) split: seed-envelope builder extracted to
  `_seed_envelope.py` and relationship/namespace graph builders extracted to
  `_graph.py` (F11d). `_compile.py` now sits below the 600-LOC cap and its
  allowlist entry has been removed.
- **`generation/synthesize.py`**: the one type-only function-body import
  (`collections.abc.Iterator`) hoisted to a `TYPE_CHECKING` block (F12). All
  other deferred imports in that module are real runtime imports and were left
  in place.

### Added (BF3 generation completeness, 2026-06-26)

- **Cross-column `formula` references in v2 generation**. A generate
  `formula` column carrying `references: [...]` (e.g. an `email` column
  built from `first_name`/`last_name` siblings) is now computed instead
  of returning all-null placeholders plus a "not yet supported"
  UserWarning. `generate_tables` runs a single declared-order in-memory
  post-pass (`_fill_referenced_formula_columns`) after every sibling
  column is finalized, delegating to the existing
  `ColumnGenerator.fill_referenced_formula_column`. It reuses the same v6
  per-row family derivation as the inline formula path, so there is NO
  `SEED_PROTOCOL_VERSION` / persisted-format change. `null_probability`
  applies to the computed values; a reference to a missing column logs a
  warning and yields nulls. LIMITATION: the post-pass is a single
  declared-order pass -- a referenced formula that reads a
  LATER-declared referenced formula sees that sibling's null placeholder
  (no multi-pass dependency resolver).

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

### Added (BF2 field-recognition harness, 2026-06-26)

Groundwork for a future ML column classifier (ML2+, gated and not built
here). Both additions are off the public run path and are intentionally
NOT re-exported from `decoy_engine.__init__`.

- **Regex-detector baseline harness + labeled fixtures** (`storm/eval/`,
  BF2/ML0). Five deterministic synthetic datasets with per-column
  ground-truth labels: `hipaa` (mrn, icd10, npi, health_plan_id),
  `pci` (pan, cvv, iban), `account_order` (account_id, order_id),
  `claim` (claim_id, service_date, amount), and `cryptic_header` (real
  PII under opaque column names). Identifier values (PAN, NPI, IBAN) are
  constructed with their real checksums so the structural detectors
  actually fire; the checksum-digit generators in `fixtures.py` are
  independent of `storm/detectors.py` to avoid "cheating" by sharing
  code under test. `run_baseline()` runs the registered detector set over
  all fixtures and returns a `HarnessReport` with per-field-type recall,
  precision, review-burden, and false-negative lists. Pinned
  misses at overall recall 0.8462 (11 of 13 PII columns): name-hint-gated
  health detectors (mrn, health_plan_id) miss entirely under opaque
  headers; content detectors (ssn, email, pan) stay header-agnostic and
  fire correctly; account_id is a confirmed false positive (the mrn
  detector claims generic account/acct identifiers by design). Read-only
  over the detector set; no run-path change.

- **Deterministic column feature builder** (`storm/features/`, BF2/ML1).
  `build_column_features(series, col_name)` produces a `ColumnFeatures`
  artifact: header tokens, inferred dtype, null/distinct/unique rates,
  char-class fractions, stdlib Shannon entropy (raw and normalized),
  per-detector regex weak signals (including checksum-gated rates for pan,
  iban, ipv4, icd10, npi), standalone checksum pass rates (no regex gate),
  and a `ShapeSignature` (dominant value mask, length stats). Reuses the
  profiler's four coarse classifiers (alphabet, casing, value-set-size,
  numeric-range) and the detector regex constants and validators so a
  detector change flows through automatically. Deterministic: content
  features sample `iloc[:200]` (matching the profiler's head-sample
  convention, never a random draw). `ColumnFeatures` is a separate
  artifact from `StormProfile`/`FieldStats` by design so it never
  crosses the persisted-format compatibility boundary.

### Added (ML-foundation measurement substrate, 2026-06-27)

Measurement gates for a future ML column classifier (ML2.2+). Scaffolding
is off the public run path and intentionally NOT re-exported from
`decoy_engine.__init__`.

- **Extended harness with F2, confusion matrix, and aggregate metrics**
  (`storm/eval/harness.py`, ML0/§A.1, §A.7). `run_baseline()` now
  computes per-type precision, recall, and F2 (β=2, recall-weighted per
  Presidio SpanEvaluator conventions). Aggregate metrics: macro-F2,
  weighted-F2 (corpus-prevalence weighted), balanced_accuracy (macro-average
  recall), entity-type confusion matrix (truth rows x predicted columns),
  and enumerated FP/FN lists identifying which columns false-positive or
  false-negative. This is the foundational evidence artifact proving where
  the regex detectors miss. The baseline report is frozen at
  `docs/v2/ml/baseline-report.json` with a regression-test gate
  (`tests/snapshots/test_ml_baseline_golden.py`).

- **StratifiedGroupKFold split scaffolding** (`storm/eval/split.py`,
  ML0/§A.3). Held-out split utility guarded against data leakage: group
  = the unique PII value string, so the same value cannot appear in both
  train and test. Prevents a future model memorising strings instead of
  learning column-shape patterns. `make_split_inputs()` converts labeled
  fixtures to `(X, y, groups)` for sklearn's `StratifiedGroupKFold`.
  Requires the optional `[ml]` extra (`pip install 'decoy-engine[ml]'`,
  pins scikit-learn >= 1.4, < 3). The regex baseline has no training phase
  and does not use this utility; it is scaffolding for ML2.2.

- **Confidence bands and per-column latency benchmark** (`storm/eval/bands.py`,
  ML0/§A.4). Three operational confidence bands for STORM field-recognition
  suggestions: high (precision >= 0.95), review (0.70 <= precision < 0.95),
  low (precision < 0.70). Thresholds calibrate to the regex baseline
  precision (not probabilistic model outputs; calibration deferred to ML2.2).
  Includes per-column latency micro-benchmark (target: < 50ms dev-tier budget).

- **Privacy test for baseline artifact** (`tests/privacy/`,
  test_no_raw_values_in_baseline_report.py, ML0/§B.4). Asserts no raw PII
  cell values in the frozen baseline report or feature dicts, a guard
  against accidental training-data leakage into version-controlled artifacts.

### Added (ML3 field classification and provenance, 2026-06-27)

Production column-type classification and manifest integrity features built
on the ML1/ML2 foundation. Gated by the `[ml]` optional extra; off by default.

- **ML3.1: `classify_fields()` public function** (`storm/model_pack/classify.py`).
  Entry point for LightGBM-backed field-type classification: loads the model pack
  via `ModelPackLoader`, builds ML1 aggregate column features, and returns per-
  column predictions with calibrated confidence scores and operational confidence
  bands (high/review/low). Output contains metadata only; no raw cell values are
  included (privacy invariant per ml-benchmarking-and-privacy.md §B.4). Returns
  `None` (never raises) when ML is disabled (`DECOY_ML_DISABLED=1`) or the pack
  is missing/corrupt, so callers can fall back to the deterministic regex baseline.
  Deterministic: given the same `DataFrame` and pack, always returns identical
  results. The platform's HTTP classify-fields endpoint and review UI consume this
  function (ML3.3, frontend lane).

- **ML3.2: HMAC-SHA256 provenance signing** (`storm/model_pack/provenance.py`).
  New functions `sign_manifest()` and `verify_manifest()` bind manifest integrity:
  canonical-JSON payload (all fields except `manifest_hmac` itself) is signed with
  HMAC-SHA256, binding the weights file hash, eval report hash, feature schema
  version, and pack identity. Uses stdlib `hmac` + `hashlib` (established keyed-hash
  primitive used throughout the engine). The `ModelPackLoader` enforces signature
  verification when a signing key is configured via `DECOY_PACK_SIGNING_KEY` env
  var (hex-encoded 32 bytes): unsigned packs rejected, tampered manifests detected
  via constant-time comparison. Without a key, packs are accepted with a warning
  (forward compatibility for development/testing). Production signing-key source is
  escalated (see Sprint C hand-off); key management not configured in this module.

### Added (SP-01 perf guard coverage, 2026-06-27)

- **PERF.BASE.3 guard coverage restored for the V2 baseline** (`tests/perf/`,
  SP-01). The guard suite deleted in b9b73e1 (when the engine became V2-only)
  is restored against the V2 substrate. `test_baseline_schema.py` (6 tests)
  pins `meta.schema_version`, the `["pandas", "polars"]` substrates
  declaration, 11-strategy x {small, medium} coverage, all required top-level
  and substrate timing fields, and the p95 >= p50 sanity invariant against
  `tests/perf_fixtures/engine-v2-baseline.json`.
  `test_baseline_reproducibility.py` (2 tests) runs a subprocess dual-run on
  mid-band cells (date_shift, hash) and asserts the polars p50 values land
  within 3x of each other: a harness-sanity check that a broken or
  non-deterministic benchmark script surfaces in CI. The 3x bound is
  deliberately loose and is NOT the regression gate. The throughput regression
  gate is `scripts/compare_baselines.py`, which flags any cell more than 5%
  slower than the committed baseline JSON. `docs/v2/perf/engine-v2-baseline-report.md`
  is the accompanying baseline report (human-readable gate table and caveats).
  These tests run under `pytest -m "not benchmark"` (the `perf` marker is not
  excluded from the CI regression gate).

### Added (SP-04 checksums + FPE valid-by-construction, 2026-06-27)

- **`decoy_engine.checksums` check-digit registry** (SP-04 / P5.INFRA.1).
  New module `src/decoy_engine/checksums.py` exposes a uniform pair of
  functions for seven structured-identifier schemes:
  `validate(scheme, value) -> bool` and `calc_check_digit(scheme, body) -> str`.
  Schemes and backing implementations: `luhn` (python-stdnum 2.2, Luhn 1954),
  `npi` (hand-rolled per CMS NPPES check-digit spec; enforces the 1/2
  leading-digit NPPES allocation rule), `iban` (python-stdnum 2.2 stdnum.iban,
  ISO 13616 / ISO 7064 mod-97), `vin` (hand-rolled per NHTSA 49 CFR Part 565 /
  ISO 3779), `isbn13` (python-stdnum 2.2 stdnum.isbn via GS1 EAN algorithm),
  `ean13` (python-stdnum 2.2 stdnum.ean), `gtin` (python-stdnum 2.2 stdnum.ean;
  covers all four GTIN lengths 8/12/13/14). `python-stdnum >= 2.2` is now a core
  dependency declared in `pyproject.toml`.

- **FPE `checksum:` parameter: valid-by-construction masked identifiers**
  (SP-04 / P5.INFRA.1). `transforms/fpe.py` and
  `execution/_strategies/_fpe.py` accept a new `checksum: <scheme>` config key.
  After the Feistel permutation rewrites the value body, the engine recomputes
  the check digit in place. The masked value is valid for the named scheme by
  construction, in both the forward (mask) and inverse (unmask) directions.
  Determinism is preserved: the same input, key, and scheme always produce the
  same output. `checksum:` takes priority over `validate_luhn:` when both are set.

  Schemes valid-by-construction in FPE mode: `luhn`, `npi`, `vin`, `isbn13`,
  `ean13`, `gtin`. Scheme-specific constraints applied at permutation time:
  NPI output pins the 1/2 NPPES leading digit; VIN constrains the permutation
  to the VIN alphabet (A-Z excluding I/O/Q, plus digits 0-9); ISBN-13 pins
  the 978/979 GS1 prefix.

  Three fail-closed behaviors (no silent passthrough of unmasked data):

  1. `iban` in FPE mode: `checksum: iban` raises
     `PlanCompileError(fpe_checksum_iban_unsupported)` at plan-compile and
     `FpeChecksumError` at runtime. Per-country BBAN structure enforced by
     `stdnum.iban.validate` cannot be satisfied by a format-preservation
     permutation. `checksums.validate("iban", ...)` and
     `checksums.calc_check_digit("iban", ...)` still work for validation-only
     use cases; only FPE checksum mode is unsupported for IBAN.

  2. Unknown scheme: a `checksum` value not in the known-scheme set (for example
     a typo) raises `PlanCompileError(fpe_checksum_unknown_scheme)` at compile.
     There is no silent fallback to plain FPE.

  3. Incompatible charset: a column whose configured charset cannot represent
     the scheme's required alphabet (for example `checksum: vin` with a
     digits-only charset, missing the letter characters VIN requires) raises
     `PlanCompileError(fpe_checksum_charset_incompatible)` at compile. This
     prevents a silent no-op where values would pass through unmasked because
     they fail the per-scheme minimum-body-length guard at runtime.

  `FpeChecksumError` (new typed error) is exported from `decoy_engine.errors`.

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
