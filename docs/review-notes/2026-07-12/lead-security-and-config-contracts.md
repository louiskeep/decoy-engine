# Lead Security and Runtime-Contract Review - 2026-07-12

## Scope

This note covers cryptographic boundaries, fail-open masking behavior, ML model-pack loading,
source-to-plan binding, schema closure, and whether accepted `PipelineConfig` fields have an
effective runtime owner.

- Frozen reviewed revision: `c1c4f2c2b33af39e1de4874788a7df78a352970c`.
- All probes used synthetic data in temporary directories.
- No external integration or production data service was exercised.

## Findings Directory

| Rank | Severity | Finding | Direct consequence |
|---:|:---:|---|---|
| 1 | CRITICAL | FPE is a custom cipher with fail-open input-domain behavior | Raw identifier characters or whole values can remain unchanged; some values cannot be unmasked |
| 2 | CRITICAL | Reproducibility seed is also cryptographic key material | Mask/vault confidentiality is capped at a 64-bit, often low-entropy config value |
| 3 | CRITICAL | Runtime schema is open by default | New or omitted source columns are published unchanged |
| 4 | CRITICAL | Unsigned joblib model packs execute by default | A selected or replaced pack can execute Python with caller privileges |
| 5 | HIGH | Profiling and execution consume different source objects | Plans can describe one dataset while another is executed; missing sources succeed silently |
| 6 | HIGH | Accepted configuration is not one executable contract | Subset and post-validation controls are inert or unreachable; targets and sinks are route-dependent |
| 7 | HIGH | Pool capacity has two meanings and two locations | Compile admission does not describe runtime pool behavior |
| 8 | HIGH | Installed ML and release artifact boundaries are broken | The wheel omits the default model; the sdist includes local untracked state |

## 1. CRITICAL - FPE is fail-open, non-standard, and not uniformly reversible

### Evidence

- `src/decoy_engine/transforms/fpe.py:9-25` explicitly implements an in-house eight-round
  HMAC/Feistel construction and says it is not NIST FF1.
- `src/decoy_engine/transforms/fpe.py:55-82` contains the round construction. Functional
  bijection tests do not establish cryptographic strength.
- `src/decoy_engine/transforms/fpe.py:379-404` preserves every out-of-charset character when
  `preserve_separators=True`; with it false, any out-of-charset character returns the whole value
  unchanged.
- `src/decoy_engine/transforms/fpe.py:166-177,386-390` replaces an all-out-of-charset input with a
  one-way covering hash. The decrypt path has no inverse for this branch.
- `src/decoy_engine/transforms/fpe.py:287-312` returns too-short NPI/ISBN/VIN values unchanged.
- `docs/what-we-cannot-prove.md:147-169` accurately calls the partial and false-mode behavior a
  clear-text leak.
- `tests/unit/disguises/test_pack_charset_no_leak.py:61-111` contains passing tests that prove
  source characters are retained.
- Shipped HIPAA, GLBA, PCI, FERPA, GDPR, CCPA, CPNI, SOX, and P&C disguise packs select FPE for
  regulated identifiers under `src/decoy_engine/disguises/`.

Synthetic probe:

```text
partial_source= STATUS-1
partial_masked= STATUS-3
false_masked= STATUS-1
all_out_source= --- covered= 297
covered_decrypted= 456
```

### Resolution

1. Immediately reject `preserve_separators=False` for security-classified columns and reject any
   value outside a declared, complete domain. Never reinterpret unrecognized data characters as
   harmless separators without a typed, position-level format schema.
2. Replace the custom cipher with a reviewed FF1 implementation or a token-vault design. NIST SP
   800-38G specifies FF1/FF3 methods; OWASP explicitly advises against custom cryptographic
   algorithms.
3. Define a per-strategy input-domain policy: `error`, `redact`, or explicitly non-reversible
   fallback. Do not return source values unchanged.
4. Stamp algorithm ID, parameters, key ID, and key version. Treat the migration as a protocol
   break with compatibility tests and a rekey plan.
5. Add negative tests over mixed alphabets, invalid lengths, empty values, Unicode, malformed
   check digits, and every disguise-pack field. Assert both no raw-position retention and exact
   invertibility where reversibility is claimed.

Primary standards: [NIST SP 800-38G](https://csrc.nist.gov/pubs/sp/800/38/g/upd1/final) and
[OWASP Cryptographic Storage](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html).

## 2. CRITICAL - A 64-bit reproducibility seed is the mask and vault key

### Evidence

- `src/decoy_engine/config/_global_settings.py:33` models the security input as an integer seed.
- `src/decoy_engine/plan/_seed.py:35-57,97-108` defaults missing seed material to zero and limits
  it to unsigned 64-bit before converting it to eight bytes.
- `src/decoy_engine/execution/_pandas_adapter.py:171-178` puts that seed into the masking
  `StrategyContext`.
- Hash uses it at `execution/_strategies/_hash.py:52-57`; FPE derives its key from it at
  `execution/_strategies/_fpe.py:110-115`; the vault derives Fernet material from it at
  `vault.py:115-127`.
- `src/decoy_engine/vault.py:12-16` and `docs/security/token-vault.md:52-58` acknowledge that the
  config plus vault permits re-identification.
- `docs/security/key-derivation.md:1-85` describes a 32-byte `DECOY_MASTER_KEY`, but the V2
  `run_pipeline` mask path does not receive or consume `ExecutionContext.derive_key`; its
  `derive_key` kwarg is forwarded only to generation at `_pipeline.py:424-429`.
- Public examples repeatedly use values such as 0, 42, and 1,234,567. The complex example calls
  the seed a secret, but the representable space remains only 64 bits.

Synthetic probes recovered a seed of 42 by enumerating 0 through 99 from both a vault and a known
plaintext hash token:

```text
vault_seed_recovered= (42, '123-45-6789')
known_plaintext_hash_seed_recovered= 42
```

HKDF provides domain separation; it cannot create entropy that is absent from the input. RFC 5869
explicitly warns that extraction does not amplify low entropy.

### Resolution

Split the concepts:

- `reproducibility_seed`: public/non-secret, used only for synthetic generation and sampling.
- `mask_key_ref`: required for keyed masking and vault operations; resolves to at least 128 bits,
  preferably 256 bits, from a KMS/HSM/secrets manager at the execution boundary.

The plan and logs should carry only a key ID/version. Derive purpose-specific keys with versioned
labels for hash, FPE/tokenization, vault encryption, model signing, and generation. Fail before
profiling or output creation when a security-classified strategy lacks a key. Provide rotation,
dual-read/rekey, and protocol-migration procedures.

Verification must prove: same seed/different key differs, same key/version is stable, missing key
fails, old and new key IDs are distinguishable, and knowledge of config/output does not make a
small offline seed search useful.

Primary standards: [RFC 5869](https://www.rfc-editor.org/rfc/rfc5869.html),
[NIST SP 800-57 Part 1](https://csrc.nist.gov/pubs/sp/800/57/pt1/r5/final), and
[OWASP Key Management](https://cheatsheetseries.owasp.org/cheatsheets/Key_Management_Cheat_Sheet.html).

## 3. CRITICAL - Undeclared columns pass through unchanged

### Evidence

- `src/decoy_engine/plan/_seed_envelope.py:77-100` creates work only for configured columns.
- Both adapters begin with every source column and return the complete frames:
  `execution/_pandas_adapter.py:168-180,243-245` and
  `execution/polars/_polars_adapter.py:190-207,230-240`.
- `tests/perf_fixtures/fk_relational.py:241-245` deliberately relies on undeclared columns riding
  through every route.
- `docs/what-we-cannot-prove.md:69-76` states that a sensitive passthrough succeeds.
- Post-mask STORM is platform-owned, default-off, and runs only after successful output; it is not
  a pre-publication schema closure control.

Synthetic end-to-end result:

```text
passthrough_columns= ['ssn', 'new_sensitive']
redacted_ssn= ['REDACTED']
undeclared_value= ['secret-tail']
```

### Resolution

Add a schema contract to the compiled plan and default
`unconfigured_column_policy=error` before GA. `drop` may be an explicit alternative; passthrough
must require a column declaration with `strategy: passthrough` and an acknowledgement. Run one
shared pre-write schema/postcondition guard on every route, including generated and FK outputs.
Residual-PII checks must run before the same transaction commits, not after publication.

Tests must add an unexpected sensitive column to every execution route and prove the run fails
before sink/quarantine/vault publication. This follows an allowlist posture and NIST's de-
identification framing: a successful run must establish what left the boundary, not only what was
transformed.

Sources: [NIST SP 800-188](https://csrc.nist.gov/pubs/sp/800/188/final) and
[OWASP allowlist validation](https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html).

## 4. CRITICAL - Unsigned model packs can execute Python

### Evidence

- `classify_fields(..., pack_dir=...)` accepts a caller-selected path at
  `storm/model_pack/classify.py:41-60,102-105`.
- The loader checks an unkeyed SHA-256 copied from the adjacent manifest, then makes signature
  enforcement optional: `storm/model_pack/loader.py:122-129,206-279`.
- With no key and the default `DECOY_PACK_REQUIRE_SIGNATURE=0`, unsigned packs are accepted with a
  warning.
- `storm/model_pack/loader.py:281-295` calls `joblib.load`. Hash verification against an
  attacker-controlled manifest proves integrity of transfer, not authenticity of the producer.
- The committed pack intentionally has an empty HMAC and is expected to be signed in place at
  deployment (`docs/v2/ml/model-card.md:247-257`).

A controlled synthetic pack with a matching SHA-256 and empty signature created a marker file when
loaded:

```text
unsigned_manifest= True
marker_exists_after_load= True
marker_contents= executed
```

Joblib's own documentation states that `joblib.load` can execute arbitrary Python and must not load
untrusted sources. Scikit-learn gives the same warning for pickle/joblib persistence.

### Resolution

Prefer a non-pickle format whose contents can be inspected before execution, such as an appropriate
ONNX or reviewed `skops.io` representation. If joblib must remain temporarily:

- make authenticated provenance mandatory and fail closed by default;
- use publisher authentication suitable for distributed artifacts, such as a vendor signature
  verified by a pinned public key or Sigstore, rather than a per-instance HMAC applied after install;
- remove or strictly authorize arbitrary `pack_dir` selection;
- verify before deserialization in a low-privilege isolated process;
- pin and verify the complete runtime dependency set.

Sources: [joblib.load security warning](https://joblib.readthedocs.io/en/stable/generated/joblib.load.html)
and [scikit-learn model persistence](https://scikit-learn.org/stable/model_persistence.html).

## 5. HIGH - The plan and executor can see different datasets

### Evidence

`run_pipeline` profiles file/cloud descriptors from `config["sources"]` at
`execution/_pipeline.py:303-315`, but masks the separate caller-owned `sources` mapping at
`_pipeline.py:296-297,449-477`. There is no binding between them. Dtype, cardinality, row-count,
and memory decisions can therefore describe one object while execution consumes another.

The pandas and Polars adapters silently skip a planned table missing from the resident mapping at
`_pandas_adapter.py:213-215` and `_polars_adapter.py:205-207`.

Synthetic result from a valid mask config with no resident source:

```text
missing_source_outputs= []
missing_source_telemetry= {'execution_mode': 'full_frame',
 'route_reason': 'no_relationships', 'eviction': 'none',
 'outputs_streamed': False, 'loaded_fully_in_memory': True}
```

This contradicts `run_pipeline`'s claim at `_pipeline.py:189-191` that outputs cover every output
table.

### Resolution

Use one typed `SourceHandle` per table for schema, metadata, bounded sample, and execution batches.
Either have `run_pipeline` load from the validated descriptor itself or profile the exact handles
passed for execution. Validate the planned table set, source identity/schema, and output table set
before work and again before commit. Missing or extra source tables must be typed failures.

## 6. HIGH - `PipelineConfig` is not an executable contract

### Ownership trace

| Field/control | Accepted shape | Actual owner/behavior |
|---|---|---|
| `subset` | `PipelineConfig.subset` | `run_pipeline` never reads it; callers must invoke `run_subset` and replace sources manually |
| `global_settings.post_validation` | nested boolean | `run_pipeline` never invokes `PostValidationRunner`; the runner checks a nonexistent top-level `post_validation` key |
| `post_validation_skip`, `post_validation_sample_size` | runner reads top-level | strict `PipelineConfig` has no such fields, so validated configs cannot express them |
| `run_storm` | top-level boolean | explicitly platform-owned; engine execution ignores it |
| `targets` | required non-empty mapping | excluded from plan semantics and not written by `run_pipeline` |
| `sink` kwarg | public runtime control | consumed only by sequential/out-of-core routes; full-frame and auto-chunk ignore it |

Sources: `config/_pipeline.py:72-99,249-299`, `subset/_api.py:1-7,190-211`,
`validation/post/_runner.py:49-85`, and `execution/_pipeline.py:382-418,568-593`.

Documentation also conflicts: `docs/relationships.md:133-138` says adding `subset:` runs a pre-mask
stage, while the integration test manually calls `run_subset` first
(`tests/integration/subset/test_subset_then_mask.py:78-120`).

### Resolution

Choose one honest API boundary:

1. An orchestrator owns validate -> subset -> profile -> compile -> execute -> post-validate ->
   commit all artifacts; or
2. Rename `run_pipeline` to the narrower core it implements, remove externally owned fields from
   its config, and expose a separate orchestrator contract.

Create a machine-readable field-ownership/capability table and a sentry requiring every accepted
field to have one runtime consumer or an explicit external namespace. Reject a sink on a route that
cannot honor it instead of silently retaining outputs.

## 7. HIGH - Pool capacity validation and runtime disagree

### Evidence

- Compile preflight checks `source.distinct_count <= ColumnConfig.pool_size` for UNIQUE at
  `generation/pool/_validate.py:78-136`.
- Runtime UNIQUE promises one output per row without replacement and requires
  `n <= pool.size` at `generation/pool/_sampler.py:11-15,118-134`.
- The top-level `ColumnConfig.pool_size` is not copied into `ColumnSeed.provider_config` by
  `plan/_seed_envelope.py:203-207,256-268`.
- Faker runtime reads only `provider_config.pool_size`, defaulting to 10,000 at
  `execution/_strategies/_faker.py:46-80`.
- The unit preflight fixture uses `row_count=distinct*10` and calls a 200-value pool sufficient for
  500 UNIQUE rows at `tests/unit/generation/pool/test_validate.py:19-31,124-130`.

Synthetic end-to-end result:

```text
profile_rows= 500 profile_distinct= 50
configured_pool_size= 200
planned_provider_config= ()
output_rows= 500 output_distinct= 500
```

The declared 200-value capacity was ignored; runtime silently built the 10,000 default pool.

### Resolution

Define one typed `PoolSpec` in the frozen plan. Preflight and sampler must call the same capacity
function. UNIQUE capacity is non-null output-row count, not source distinct count. Use one
`pool_size` location and reject contradictory legacy locations. Add end-to-end config -> plan ->
handler tests; do not validate only hand-built profiles/plans.

## 8. HIGH - Release artifacts do not deliver the reviewed ML boundary

The current build produced a 1.1 MB wheel and 49 MB source distribution.

- The wheel has 377 files but no `model.joblib`, model manifest, or `py.typed` marker.
- `_DEFAULT_PACK` resolves outside the installed package
  (`storm/model_pack/classify.py:37-38`). An extracted-wheel probe reported
  `default_pack_exists=False` and `classify_result=None`.
- The sdist had 3,116 entries: 1,279 under an untracked nested agent worktree, 1,154 matching
  Hypothesis cache paths, and three current review documents.
- `pyproject.toml:221-222` constrains only the wheel; there is no explicit sdist allowlist.

This combines a functionality failure (installed ML silently falls back) with a supply-chain and
confidentiality risk (local untracked state enters a release artifact). Detailed release evidence
and remediation are in `contracts-tests-operations.md` F1 and F5.

Remediate with explicit wheel/package data via `importlib.resources`, an explicit Hatch sdist
allowlist, clean-checkout artifact CI, archive-member assertions, install-from-wheel/sdist tests,
and artifact provenance/signing. Do not publish the current sdist.

## Positive Controls Observed

- Expression evaluation uses `simpleeval` or a closed Lark grammar with depth/length controls;
  no direct `eval`/`exec` path was found in the reviewed V2 expression surfaces.
- Out-of-core temp roots and DuckDB temp directories are chmod 0700 and relation/join/staged
  subtrees are removed in `finally` (`execution/out_of_core/_runner.py:141-196`,
  `_duckdb.py:21-28`).
- Vault v2 encrypts bounded chunks rather than constructing a second full-table plaintext blob.
- The FPE leak and profiler uncertainty are unusually well documented. The issue is that the
  runtime and product claims still permit or depend on those known gaps.

## Verification Record

Frozen revision `c1c4f2c`:

```text
.venv/bin/pytest -q
6281 passed, 38 skipped, 13 deselected, 21 warnings in 385.01s

.venv/bin/ruff check src tests testflight scripts
All checks passed!

.venv/bin/ruff format --check src tests testflight scripts
761 files already formatted

.venv/bin/mypy src/decoy_engine testflight
Success: no issues found in 375 source files
```

The green suite does not falsify these findings. Several tests deliberately assert the unsafe
behavior (FPE character retention, pandas large-integer rounding, eager OOC loading), and others
exercise lower-level helpers with flat mini-configs that cannot be produced by `PipelineConfig`.
