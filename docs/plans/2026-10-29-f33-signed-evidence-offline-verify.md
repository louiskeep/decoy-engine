# F33: Unified signed evidence and offline verification

Status: plan

Summary: Authenticate a versioned evidence statement and its artifact inventory, verify it offline with independently trusted public keys, and prevent strict publication when required checks are inconclusive.

Baseline commit: `2a44723ff26ffd16c69f026582ec93998d67ca5d` (`git rev-parse HEAD`, inspected 2026-10-29).

**CRYPTO / MAJOR tier. Gate = Dennis + Codex + Gemini, adversarial and crypto-focused. This is the Codex first draft for the Codex -> Gemini -> Dennis plan-gate pipeline, not an approved plan or implementation receipt.**

Risk: R2 for the format, trust boundary, and implementation. Production key rotation, revocation, and irreversible release actions require the applicable R3 authorization and recovery gate. No such action is part of drafting this document.

Rules consulted: `/home/cam/dev-rules/00-universal.md`, `risk-and-exceptions.md`, `README.md`, `development-loop.md`, `reuse-first.md`, `exploration.md`, `scope-discipline.md`, `workspace-and-vcs.md`, `verification.md`, `feature-dev.md`, `testing.md`, `architecture.md`, `api-and-compatibility.md`, `security.md`, `config-secrets-and-supply-chain.md`, and `documentation.md`; repository `AGENTS.md`, `README.md`, `CODEMAP.md`, `CONTRIBUTING.md`, `CLAUDE.md`, `SECURITY.md`, and the relevant compatibility-contract provisions. Rulebook names after the first share `/home/cam/dev-rules/`.

## 1. FRAME

### 1.1 Problem, guarantee, and boundaries

Recipients need to distinguish an authentic statement from an editable checksum, verify the exact delivered output bytes, and know which required checks actually completed. A signature authenticates an assertion by a trusted key. It does not establish that a detector is correct, that a signer is honest, or that output is private or compliant.

The guarantee is scoped to every supported F33 evidence creation, publication, review, and export path. Every newly emitted F33 bundle, including a rejected-run evidence bundle, passes the signing boundary. Every strict data release requires both valid signed evidence and a complete, passing required-check evaluation. Signing a rejection must remain possible without authorizing data release.

The engine owns shared evidence semantics and check outcomes; the CLI owns commands and standalone distribution; the platform owns tenant authorization, persistence, review/export, publication visibility, and external KMS integration. A caller can still use the engine as a masking library without publishing F33 evidence. Such a result must never be advertised as a signed evidence bundle. Python types and sentries protect supported integrations; they cannot constrain arbitrary code running with the caller's privileges.

Non-goals: inventing a signature protocol, changing masking derivations, replacing model-pack authentication, running a public transparency service, certifying privacy/compliance, silently upgrading old evidence into authenticated history, or guaranteeing artifact availability forever. F33 does not implement a new timestamp authority.

Roadmap context supplied by the requester: Phase 1 Track B promotes signing from deferred R4 to V1; sources MX-05, G6, F missing-must-haves items 9-10, and `plans/2026-10-24-signed-output-passport.md`. Dependencies F10, F13-F14, F22-F23, F29, F31, and F34-F37 describe contract inputs and coordination. Their feature numbers are not evidence that particular APIs exist or prerequisites that must all be rebuilt. Resolve each needed interface during its grounding gate.

### 1.2 Checkout truth and grounding limits

The supplied product audit describes r3.1 canonical evidence, `manifest_hash`, reserved signing fields, a no-install `verify.py`, and a `/proof` page. **Those surfaces were not found in this engine checkout.** They remain **asserted, verify before build**. Do not fabricate an engine passport module or treat another kind of manifest as r3.1.

Searches performed before design included:

```text
rg -n 'manifest_hash|signature_alg|signature_key_id|canonical|evidence|passport' src/decoy_engine
rg -n 'manifest_hash|signature_alg|signature_key_id|r3\.1|verify\.py|F31' .
rg --files -uuu | rg '(^|/)(verify\.py|graphify-out/graph\.json)$'
rg -n 'inconclusive|INCONCLUSIVE|unavailable|available' src/decoy_engine/verification src/decoy_engine/detection
rg -n 'commit\(' src/decoy_engine/execution src/decoy_engine/subset
```

The signature-field search had no matches; there is no `verify.py`, F31 plan, or existing graph file to query here. qmd recall was attempted with `F33 signed evidence` and `F31`; its database initialization failed with `SQLITE_READONLY`. No conclusions depend on qmd snippets or an unavailable graph. F31's requested split is followed as an instruction, not claimed as a locally inspected precedent.

Verified source anchors below were opened with line numbers in this checkout:

| ID | Verified anchor | Consequence for F33 |
|---|---|---|
| E1 | `src/decoy_engine/keyprovider.py:103` | The masking `KeyProvider` returns secret bytes; its docstring explicitly says the engine emits no evidence manifest and the platform assembler stamps key versions. This is not an asymmetric signing provider. |
| E2 | `pyproject.toml:149` | `cryptography>=42` is a base dependency. Use that crypto stack, with maintained protocol/canonicalization libraries. |
| E3 | `src/decoy_engine/generation/_pinned_plan.py:167` | A local canonical helper uses Python sorted JSON and SHA-256. This is a plan fingerprint, not proof of RFC 8785 compliance or the r3.1 serializer. Preserve it. |
| E4 | `src/decoy_engine/profiling/model_pack/provenance.py:165` | Model-pack signing uses HMAC-SHA256 with a cleared `manifest_hmac`. Shared-secret authentication cannot meet recipient public-key verification. Do not reuse its wire format or key. |
| E5 | `src/decoy_engine/validation/post/_types.py:60`, `src/decoy_engine/validation/post/_scan.py:55` | `QualitySummary` has `failed_checks`; `ScanOutcome` has a Boolean `failed`, not a complete-status enum. |
| E6 | `src/decoy_engine/validation/post/_runner.py:80`, `src/decoy_engine/validation/post/_runner.py:100` | Validation can be off; skipped scans disappear. Exceptions become failed outcomes; warning detail currently uses `repr(exc)`. F33 needs explicit completion records and a safe evidence projection. |
| E7 | `src/decoy_engine/execution/_pipeline_finalize.py:271`, `src/decoy_engine/execution/_pipeline_finalize.py:468` | Post-validation forwards enforcement metadata but does not itself fail the job. STORM is report-only. Neither is the required strict publication gate. |
| E8 | `src/decoy_engine/detection/detectors.py:742`, `src/decoy_engine/detection/detectors.py:775` | Built-ins return matches; invalid custom regexes are skipped. An empty matches list does not prove every required detector ran. |
| E9 | `src/decoy_engine/detection/ner.py:62`, `src/decoy_engine/detection/ner.py:98` | Typed missing-spaCy/model errors exist. These supply availability reasons, not current `INCONCLUSIVE` records. |
| E10 | `src/decoy_engine/verification/postmask/types.py:17`, `src/decoy_engine/verification/postmask/types.py:119`, `src/decoy_engine/verification/postmask/runner.py:91` | STORM schema is `storm-post-mask/v1`, with severity counters and `pass_failed_with`; category failures are error findings. This is not evidence schema r3.1. |
| E11 | `src/decoy_engine/verification/postmask/residual_pii.py:169` | Residual scanning skips a column when no match is returned. Completion must be recorded at execution, not inferred later from findings. |
| E12 | `tests/unit/validation/test_failed_evidence_manifest.py:55` | Existing regression preserves a quality block after a scan crash. Its name/comments are not proof of publication enforcement; assertions check the block. |
| E13 | `tests/sentry/test_public_import_boundary.py:1`, `tests/sentry/test_public_import_boundary.py:85` | There is a local architectural-sentry pattern, including an assertion that inventoried surfaces exist. F33 adds behavioral bypass tests as well. |

The evidence/passport module and r3.1 schema cannot be read in full because they are absent here. Reading both in full, locating the old verifier, and recording their actual hashes/callers is a blocking consumer-grounding task, not permission to invent substitutes.

### 1.3 Established solutions: survey before design

Online searches were run for `RFC 7515 detached JWS appendix F RFC 8785 canonical JSON`, `COSE RFC 9052 Sigstore offline verification bundle in-toto DSSE`, `cryptography python asymmetric signing KMS provider abstraction Signer sigstore`, and targeted library/KMS queries. Primary standards and upstream documentation were then opened. The decision follows those sources:

| Established option | Fit and trade-off | Decision |
|---|---|---|
| [JWS, RFC 7515, sections 5 and 7.2.2, Appendix F](https://datatracker.ietf.org/doc/rfc7515/?include_text=1) | Detached content and a protected header fit the existing JSON evidence family. Requires strict algorithm/header policy and exact payload reconstruction. | Choose flattened detached JWS with one signature. |
| [COSE, RFC 9052, sections 4 and 4.4](https://www.rfc-editor.org/rfc/rfc9052.html) | COSE_Sign1 offers a standardized binary signing structure and protected attributes. Adds CBOR tooling and a second serialization model to the current JSON contract. | Valid alternative; unnecessary migration cost for V1. |
| [in-toto Statement v1](https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md) and [DSSE envelope](https://github.com/secure-systems-lab/dsse/blob/master/envelope.md) | Strong subject/predicate model and typed payload signing. DSSE key identifiers are hints; identity/algorithm semantics need an application profile. Embedding base64 payloads changes the current JSON presentation. | Useful attestation export later; do not layer a second signature format into V1. |
| [Sigstore bundle](https://docs.sigstore.dev/about/bundle/) | Bundles can carry verification material including transparency evidence. Valuable for identity and time evidence, but introduces certificate/log trust and operations beyond explicit customer public-key trust. | Do not make Sigstore infrastructure a V1 dependency. Reuse its provider-interface pattern only. |
| [JCS, RFC 8785](https://datatracker.ietf.org/doc/html/rfc8785) | Defines interoperable JSON bytes, including number serialization and UTF-16 property sorting. A Python `sort_keys=True` call alone does not establish this behavior. | Choose JCS for the new signed profile; preserve legacy hash rules under their own version. |

Algorithm: **ES256 only in profile v1**, ECDSA P-256 with SHA-256 per [RFC 7518 section 3.4](https://www.rfc-editor.org/rfc/rfc7518.html). It has an established KMS mapping and support in the repo's `cryptography` stack. Use maintained JOSE code for the signature encoding. Do not implement curve arithmetic, nonce generation, ASN.1 parsing, or JCS number formatting. ECDSA signatures need not be byte-identical across retries; statement bytes and statement identifiers must be stable.

Implementation candidates selected for the dependency spike are [joserfc JWS](https://jose.authlib.org/en/guide/jws/), its [strict registry](https://jose.authlib.org/en/guide/registry/), and [Trail of Bits rfc8785](https://github.com/trailofbits/rfc8785.py). joserfc supplies the protocol; [cryptography ECDSA](https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ec/) supplies primitives. Before committing to versions, prove detached-content support, external-signer injection, protected-header validation, packaging compatibility, license/provenance, and security status. Pin the standalone distribution's dependency hashes. If the library cannot expose the needed KMS boundary through supported extension points, return to the plan gate; do not implement a home-grown JOSE stack to bridge it.

For the provider abstraction, follow [Sigstore's PublicKeyProvider / Signer / Verifier interfaces](https://pkg.go.dev/github.com/sigstore/sigstore/pkg/signature) and its [in-memory and cloud KMS policy](https://github.com/sigstore/sigstore/blob/main/POLICY.md). Reuse the platform's merged `api/keys/` external-KMS envelope, credential resolution, and lifecycle. That internal anchor is **asserted, verify before build**. The external pattern describes the seam; it does not prove what `api/keys/` currently implements.

Every new protocol, canonicalization, signing, trust, and KMS-adapter module must cite its applicable source patterns in its module docstring, including the verified `api/keys/` source after grounding. Custom code is limited to Decoy schema validation, checked-result projection, trust policy, publication orchestration, and narrow provider adaptation.

### 1.4 Typed envelope and exact signing contract

These are **proposed new types/profile names**, not existing files or APIs. Freeze the schema with consumer owners before a production producer switches formats.

`EvidenceStatementV1` carries:

- A unique schema/profile identifier, bundle ID, job ID, attempt ID, issuer context, engine identity, and creation time explicitly labeled as signer-asserted.
- Semantic fingerprints and their algorithm/version labels, source snapshot identity, counts, dispositions, warnings, and publication state.
- An artifact inventory with unique logical IDs, safe relative paths, media types, byte lengths, SHA-256 digests, and availability/disposition. Hash the final file bytes, including Parquet metadata and encoding, not a DataFrame reconstruction.
- A required-check policy ID/version/digest, expected subject inventory, and all check outcomes. Each outcome binds method, version/model identity, subject artifact or snapshot identity, limits/sample coverage, severity, allowlisted safe metrics, status, and coded reason.

The standard signature object is a flattened detached JWS object with exactly `protected` and `signature`; omit `payload`, reject `header` and multi-signature forms in v1. A published bundle carries that object and the canonical statement. The typed `SignatureEnvelopeV1` exposes validated protected-header fields, rather than trusting duplicated outer labels:

| Protected field | Contract |
|---|---|
| `alg` | Literal `ES256`; also enforced by verifier configuration and trusted key policy. |
| `kid` | Opaque identifier for one immutable key version. Never an active-key alias, URL to fetch, or sole proof of trust. |
| `typ` | Exact profile-specific type, proposed `application/vnd.decoy.evidence-signature.v1+jws`. An application identifier, not a claim of IANA registration. |
| `cty` | Exact statement type, proposed `application/vnd.decoy.evidence.v1+json`. |
| `decoy_signer` | Non-secret stable issuer identity, authorized by the supplied trust policy. |
| `decoy_key_version` | Immutable version identifier matched to `kid` and the trusted public key. |
| `crit` | Exactly the profile's critical extension names, including `decoy_signer` and `decoy_key_version`; unknown extensions fail. |

Keep the transmitted protected-header bytes for verification. Do not parse and reserialize them before checking the signature. The JOSE library constructs the standard signing input from that protected segment and the base64url representation of the detached canonical statement. Do not sign only the old `manifest_hash`. No `b64=false`, JWT claim processing, `none`, HMAC, algorithm guessing, embedded key trust, or network key discovery in this profile. Apply algorithm restriction, explicit typing, and context validation principles from [RFC 8725 sections 3.1, 3.11, and 3.12](https://www.rfc-editor.org/rfc/rfc8725.html).

Canonicalization rules:

1. Parse strict UTF-8 JSON with duplicate-member rejection at every depth, no trailing content, no non-finite values, and no unpaired surrogates. Apply byte, nesting, field-count, and inventory limits before expensive work.
2. Validate the exact supported statement schema, then use the maintained RFC 8785 implementation. Reject unknown semantic fields unless the versioned schema explicitly defines their treatment. Do not silently discard fields before verification.
3. Require the detached `evidence.json` bytes to equal the canonical result, with no BOM or trailing newline. This profile makes even whitespace mutation of the machine statement detectable as `NON_CANONICAL`; HTML/PDF formatting remains a separate view.
4. JCS has a constrained numeric domain. Counts outside the interoperable safe-integer domain use schema-defined decimal strings; arbitrary precision metrics likewise need explicit decimal-string fields. Never silently round a legacy value or stringify arbitrary objects. Array order is schema-defined and stable. Preserve Unicode strings without normalization.
5. Define the new statement ID as SHA-256 of these canonical bytes, stored externally or computed by the verifier. Avoid a self-hashing field cycle. Existing semantic fingerprints and legacy manifest hashes remain opaque, version-labeled inputs with unchanged historical algorithms.

The wire format is a JWS application profile, not a new signature envelope algorithm. Deeply immutable bytes are the authority after signing; no mutable `quality_metrics` dictionary is a signed carrier.

### 1.5 Offline verification and trust distribution

The verifier consumes a bundle, an artifact root, an independently supplied trust policy/public-key set, and a caller-chosen verification purpose. It makes no network calls. Trust comes from an operator-pinned file or an authenticated out-of-band distribution whose digest the recipient pins. A key bundled by the signer is only a candidate key. Never trust `jku`, `x5u`, an embedded JWK, a tenant selected by an unsigned field, or a fetched URL.

Trusted entries bind signer identity, issuer/tenant scope, immutable key version, public key fingerprint, allowed algorithm, validity/lifecycle metadata, and the policy's issued-at/expiry/version. Reject duplicate or conflicting key IDs. Match all identity fields and expected scope. An authentic statement for a different job/attempt is not evidence for the requested release. Expected job/attempt/bundle identifiers come from caller context; when absent, verification reports identity without asserting freshness or applicability to a new job. The verifier's public entry takes a typed `VerificationContext` (expected job/attempt/bundle IDs, expected artifact digests, trust policy, purpose) as the SOLE source of trusted expected claims; it fails on any mismatch and never derives an expectation from the untrusted bundle itself. Loose string parameters are not an accepted substitute, because a valid signature for one job must not be replayable as evidence for another.

Return a structured result with independent dimensions:

- `schema`: supported, unsupported, or malformed.
- `signature`: valid, invalid, unsigned, or not evaluated; cryptographic validity alone is never a release pass.
- `trust`: trusted, unknown key, scope mismatch, retired, revoked, or stale/unknown lifecycle policy, with the policy digest and evaluation time.
- `artifacts`: complete match, mismatch, missing, or unavailable, with one result per expected artifact.
- `checks`: complete pass, failed, or inconclusive, plus explicit missing/duplicate subject results.
- `publication`: the signed historical state and a separately recomputed release-policy verdict.
- `historical_authorization`: established under identified external evidence, disallowed, or unknown.
- `time`: two distinct fields, the untrusted `created_at` copied verbatim from the signed statement and the verifier's own `evaluation_time` (local clock) used to evaluate trust, revocation, and policy expiry. A consumer must never read `created_at` as a trusted timestamp.

Full verification must open and hash every required artifact. Missing files stay visible and never count as a successful signature-only verification. Reject absolute paths, `..`, duplicate normalized paths, symlink escapes, device files, and archive traversal. Use bounded streaming reads and safe handles; do not hash one path and later deliver a mutable replacement. Extra files are reported as unbound and cannot be included in a verified release. A diagnostic signature-only mode returns a partial result, never the full-success exit status.

A changed artifact byte makes artifact verification fail even if the unchanged statement's signature remains valid. A changed statement, signature, or protected identity field fails signature/profile validation. This distinction must appear in text, JSON, HTML, and PDF views.

### 1.6 KMS, rotation, revocation, and historical truth

The engine-side seam accepts an immutable signing identity plus a signer operation and public-key lookup for that resolved version. Conceptually: resolve an authorized version once; sign the JOSE signing input using that version; return the signature and resolved identity; verify locally before emitting. These are behavioral requirements, not invented method names for `api/keys/`.

The platform adapter **consumes the merged external-KMS envelope under `api/keys/`**. It must preserve its tenant scoping, credentials, key references, authorization, errors, retries, and audit lifecycle. Inspect whether that envelope already supports asymmetric signing or only wrapping/encryption. If signing capability is absent, add the narrow supported capability within that existing abstraction after review. Do not pretend encryption keys are signing keys; do not create a parallel credential store, KMS envelope, or provider registry in the engine. Keep signing keys separate from masking secrets and model-pack HMAC keys.

For an AWS adapter, [KMS Sign](https://docs.aws.amazon.com/kms/latest/APIReference/API_Sign.html) documents the raw-message/digest distinction. With ES256 and `DIGEST`, send SHA-256 of the full JOSE signing input exactly once, with `ECDSA_SHA_256`; do not submit the artifact digest or hash it twice. KMS ECDSA DER output must be converted to the JOSE fixed-width signature using maintained library helpers. Validate algorithm, curve, resolved key identity, and the resulting signature against the pinned public key. Other providers must prove equivalent behavior before support is claimed.

On timeout, denial, disabled key, wrong key, corrupt signature, or unknown response, no unsigned fallback and no data publication. Retries reuse the same frozen statement, resolved key version, and publication idempotency identity. Resolve an alias only before signing; an alias rotation mid-flight cannot change the header or silently retry under a different key. Immediately before publication, the platform rechecks authorization/lifecycle under its transaction or fenced policy generation; a concurrent revocation invalidates the release permit.

Rotation retains old public keys and immutable signatures. New releases use the new version. Lifecycle state has distinct concepts: allowed to sign now, allowed for verification, and revoked/compromised. Never delete historical public verification material merely because private signing material is retired.

Historical truth is deliberately two-dimensional:

| Situation | Honest result |
|---|---|
| Old bundle verified with its retained public key after routine rotation | Signature and artifact integrity can still be valid. Report the old version and retired status; do not pretend it was signed with the current key. |
| Revoked/compromised key, signature still mathematically valid | Report valid signature plus revoked trust. Current strict acceptance fails. Revocation does not rewrite the bytes or turn the original assertion into an unsigned one. |
| Claimed creation time predates compromise, no independent evidence | Historical authorization is `UNKNOWN`. A compromised signer can backdate a statement. |
| Operator supplies an authenticated archived decision binding this exact statement/signature identity and historical policy | Report what that evidence establishes and its provenance, separately from current revocation. Never infer that archive authenticity from the evidence bundle itself. |
| Offline trust snapshot is expired or lacks current revocation knowledge | Mathematical verification may succeed; current authorization is unknown and strict acceptance fails. Explicit historical evaluation names its snapshot and cannot be presented as current clearance. |

[RFC 3161](https://www.rfc-editor.org/rfc/rfc3161.html) is the established option if a later scope requires independent proof of signing time. V1 does not claim trusted time from `created_at`, a local clock, or a self-signed receipt. Timestamp validation would need a separate reviewed extension, not an improvised timestamp field.

### 1.7 Required-check catalog and strict release gate

Introduce `PASS`, `FAIL`, `INCONCLUSIVE`, and `NOT_APPLICABLE` outcomes separate from severity. Unavailable, disabled, skipped, crashed, timed-out, unsupported, or insufficient-coverage required detectors produce `INCONCLUSIVE` with a safe reason code. A no-match result is `PASS` only after successful execution on the specified subject with adequate coverage. Adequate coverage is machine-readable, not an adjective: each check outcome that can produce a no-match `PASS` carries a non-optional `coverage` object (for example `rows_scanned`, `rows_total`, `columns`), and the release-gate evaluator validates it against policy-defined minimums. A required check that ran on a zero-row subject, or below the policy minimum, yields `INCONCLUSIVE`, never `PASS`, unless the catalog explicitly allows an empty subject. `NOT_APPLICABLE` requires a catalog rule and evidence for its predicate, not a caller assertion.

The required catalog is a versioned policy input authorized by the release owner, pinned before execution, and bound into the statement. The verifier also requires an independently trusted minimum policy; a malicious producer cannot sign an empty catalog to satisfy strict verification. Store catalog definitions once; adapters report execution facts instead of independently recreating applicability logic.

Initial catalog families, grounded in existing engine surfaces:

| Family | Required scope and pass condition |
|---|---|
| Artifact inventory and contract completeness | Every expected output/disposition and required identity is accounted for; all published artifact digests match. Empty jobs need an explicit policy-supported empty-output disposition. |
| Residual detection | Every policy-required detector/version on every applicable output subject, including explicitly required custom/NER detectors. Registry removal or missing model yields inconclusive. Findings are evaluated by policy; pattern hits are not automatically privacy failures. |
| Transformation/policy validation | Applicable configured masks and source-comparison subjects; unavailable source or incomparable data cannot be converted into a pass. |
| Structural checks | Applicable PK/FK, null, cardinality, format, and composite-coherence checks from `src/decoy_engine/validation/post/_checks/__init__.py:31`, with explicit subject coverage. |
| Leakage and reproducibility checks | Applicable leakage and determinism checks with declared method/sample limits. A sample is never labeled a full scan. |

`sampled_values` is an evidence aid, not a required pass criterion. Do not copy its rows into signed evidence. Check metrics are an explicit scalar/count allowlist; exclude source values, raw regexes/queries, exception messages, and secrets. E6's `repr(exc)` must not flow through the signed projection; repair the touched failure-record path to use safe codes and exception types.

Strict eligibility is the conjunction of supported schema, trusted signature identity, complete artifact binding, expected policy/catalog, valid job/attempt binding, and exactly one acceptable outcome for each required subject. Missing/duplicate/unknown results fail closed. Required `INCONCLUSIVE` blocks publication regardless of signature validity. Warning-only findings may pass only where the catalog permits them. Optional inconclusive checks remain visible.

A signed `REJECTED` statement may be exported for audit with artifacts marked withheld/unavailable. No output download URL, release marker, or published state may accompany it. Failure to sign the rejection leaves only an internal diagnostic, explicitly not an evidence bundle.

### 1.8 Standalone support window and compatibility

The standalone verifier must run without installing/importing `decoy_engine`, pandas, PyArrow, or the full CLI. A standard-library-only verifier cannot implement ES256 safely; do not preserve a single-file appearance by adding handwritten cryptography. Preserve the legacy hash-only `verify.py` lane once located. Deliver signed verification as an offline, unpack-and-run distribution containing the narrow verifier plus pinned JOSE/JCS dependencies and platform-appropriate `cryptography` wheels/runtime assets. No package download, bootstrap network call, or engine install at verification time. The verifier's release notes carry a security-considerations note that it relies on the integrity of the host Python interpreter and standard library, which is the standard trust boundary for this distribution model. The entry script and dependency artifact names remain **proposed**, pending CLI grounding.

Support decision: v1 readers support the signed profile v1 and the grounded r3.1 legacy integrity profile. Keep those readers for at least 24 months after signed-profile GA and at least two minor verifier releases after any successor, whichever is later. Publish the exact end dates at GA and never remove a reader without fixtures and migration evidence. Security fixes apply to the current verifier release; old binary retention is not a promise to run vulnerable tooling. Readers may report algorithm policy rejection while retaining historical parse/integrity results. Unknown schemas remain inspectable as unverified metadata and never receive full-success status.

Initial standalone packaging targets Python 3.10-3.12, matching the observed engine README; each advertised OS/architecture requires a tested offline artifact. The CLI grounding gate must reconcile its real runtime matrix before claiming support. Unsupported platforms fail clearly, without downloading code.

Compatibility path:

1. Freeze genuine CLI/platform r3.1 examples and exact legacy canonicalization/hash exclusions before changing producers. Reserved `signature`, `signature_alg`, and `signature_key_id` are **asserted, verify before build**; do not assign semantics to them from memory.
2. Keep legacy artifacts byte-for-byte. Legacy verification reports integrity-only / `UNSIGNED`; it cannot satisfy strict F33 release. Never silently reinterpret a legacy hash as a signature.
3. New producers emit the new signed profile, with a documented mapping for every old field. No in-place serializer change under r3.1. Old fingerprints retain their version tags.
4. A migration may create a new signed statement binding the exact legacy document digest and the artifacts rechecked now. It must say when that re-attestation happened and which original facts remain unknown. It cannot claim the original job was signed or the original missing detector ran.
5. During migration, existing legacy downloads may remain available under explicit integrity-only labeling. No new F33 producer has an unsigned fallback. HTML/PDF are derived views with statement ID and signature/trust/check results; they are not alternate authorities. If exported view bytes need integrity protection, bind them as non-recursive attachments before final signing, or state clearly that the view is not a bound artifact.
6. Deploy readers first, then producers and strict publication. Rolling back signed producers pauses new strict releases; it does not downgrade them to unsigned. Retain published bytes, old readers, and public keys. Preserve current CLI exit meanings after consumer inspection: full success alone may return success; malformed/partial/untrusted outcomes must be nonzero with stable machine-readable reasons.

### 1.9 Failure modes and acceptance tests, before implementation

These tests are the agreed behavior contract. Each slice lands its failing acceptance cases before implementation. Tests must assert outputs and absence of publication, not merely call counts or a raised exception.

| ID | Failure or behavior | Required acceptance evidence |
|---|---|---|
| A1 | One changed output byte | Verify a real signed fixture offline, alter one byte without altering size, and require `artifacts=MISMATCH`, nonzero full-verification status, and unchanged `signature=VALID`. Repeat for each artifact kind. |
| A2 | Changed statement/envelope | Mutate counts, dispositions, job/attempt, check status, artifact digest, signer, key version, and algorithm independently; no variant fully verifies. Non-canonical machine JSON is rejected. |
| A3 | Required unavailable detector | Real runner path with a missing required detector/model produces `INCONCLUSIVE`; sign the rejection with a valid test key; verifier reports valid signature, blocked checks, and no data release. Optional-unavailable control remains visible without automatically blocking. |
| A4 | Completeness forgery | Remove a check, subject, detector registration, policy entry, or expected artifact; duplicate conflicting results; turn validation off; request skip. Compare with the trusted required catalog and fail closed. Empty findings must not prove completion. |
| A5 | Rotation and revocation | Sign with version A, rotate to B, verify A with retained public material, then revoke A. Preserve mathematical validity while reporting revoked trust and rejecting current strict acceptance. Backdated unproven history stays unknown; stale trust snapshots do not grant current clearance. |
| A6 | Install-free verification | On a clean host/container, block network and assert engine/CLI/data libraries are absent. Unpack the offline verifier artifact and run the complete conformance corpus. Verify parity with engine and platform results. |
| A7 | Signing unavailable or malicious | Denial, timeout, alias change, wrong KMS key/curve, malformed DER, invalid signature, double hashing, and successful sign followed by revocation all yield no unsigned bundle and no strict release. |
| A8 | Publication crash/race | Inject failure after staging, hashing, checking, signing, and immediately before/after visibility commit. No reader observes an unsigned/partial release. A failed commit cannot expose a statement claiming publication; retries do not mix attempts. |
| A9 | Artifact substitution and paths | Replace a file between check/hash/sign/commit, escape the artifact root, use duplicate paths or symlinks, remove a file, append an unbound export. Refuse or report incomplete/mismatch; do not read outside the authorized root. |
| A10 | Compatibility | Legacy golden hashes remain exact; migrated evidence names its new attestation time; old missing facts stay unknown. Unknown schema, unsigned legacy, and missing artifact each have distinct non-success results. |
| A11 | Privacy of evidence | Seed source values, secret references, raw exception messages, regexes, and sample values with canaries. None may occur in statement bytes, rendered views, logs, or error output. |
| A12 | Signing bypass | Disable the signing boundary, invoke every registered producer/export path, and assert zero newly emitted bundles. Mutants that return raw bytes, strip signatures, omit required checks, or call publication early must be killed by the sentry. |

## 2. Signing choke-point, SENTRY, and verifier vectors

### 2.1 Construction and emission inventory

Use one shared finalization boundary:

```text
DraftEvidence + sealed artifact inventory + complete check ledger + trusted release policy
  -> schema validation and release evaluation
  -> immutable canonical statement bytes
  -> standard JWS signing through resolved versioned signer
  -> local cryptographic verification
  -> SignedEvidence (or signed RejectedEvidence)
  -> publication authorization recheck
  -> atomic visibility of the exact artifacts and signed bundle
```

Only the signing boundary constructs the externally serializable signed carrier. Its constructor is not a public dictionary conversion; runtime validation accompanies typing. A deserialized envelope is an untrusted parsed value until verification succeeds. The low-level JWS/ES256 signature check (the mathematical signature over the signing input) is a private function; the only public verification entry is the full Decoy-profile verifier that enforces the `crit` headers, `typ`/`cty`, and signer/version identity, so no caller can make an authorization decision on a mathematically valid but out-of-profile signature. An export of existing evidence copies verified immutable bytes and preserves the original signer/version; it does not reassemble or re-sign a historical statement under today's key. A new view/re-attestation is explicitly a new artifact.

Publication requires a permit bound to the exact statement digest, artifact inventory, attempt, and policy generation. A signed rejection has no release permit. Persisted worker results, retries, cache hits, and administrative exports cannot manufacture one from a Boolean `signature_valid` field. The emitter revalidates bindings when loading persisted carriers.

There is no single existing engine passport emitter to wrap. The following opened sites explain why adding a call at the bottom of `run_pipeline` is insufficient:

| Existing site | Verified anchor | F33 treatment |
|---|---|---|
| Four top-level route returns | `src/decoy_engine/execution/_pipeline.py:313` | All routes used by F33 must return into a staging/publication coordinator; no route-specific early release. |
| Full-frame and streamed completion | `src/decoy_engine/execution/_pipeline_full_frame.py:227`, `src/decoy_engine/execution/_chunked_output_sink.py:154` | Checks currently precede stream commit, but signed artifact bytes do not exist here yet. Make F33's final consumer publication depend on the signed carrier. |
| Sequential commit then quarantine sidecar | `src/decoy_engine/execution/_sequential.py:576` | A naive late signing hook leaks outputs first. Run inside a private outer staging directory; include/withhold sidecars explicitly before F33 visibility. |
| Out-of-core commit | `src/decoy_engine/execution/out_of_core/_runner.py:309` | Its commit must target private staging for F33; required checks must examine sealed outputs before outward release. |
| Mixed route commit | `src/decoy_engine/execution/_pipeline_mixed_route.py:253` | Same staging boundary, including generation and mask outputs. |
| Isolated-worker commit | `src/decoy_engine/execution/_isolated_run.py:576` | Child completion and parent rename must remain private until the F33 coordinator signs and authorizes visibility. Worker completion is not publication authority. |
| Atomic filesystem precedent | `src/decoy_engine/execution/_transactional_sink.py:269` | Reuse private staging and atomic rename semantics for a complete release directory; do not assume an arbitrary sink supports them. |
| Subset manifest write | `src/decoy_engine/subset/_api.py:181`, `src/decoy_engine/subset/_manifest.py:22`, `src/decoy_engine/subset/_manifest.py:44` | This is `SubsetManifest` version 1, whose type is at `src/decoy_engine/subset/_types.py:203`. It is a legacy component report. F33 exports bind its exact bytes as an attachment; it cannot substitute for the signed bundle. |
| Marketing proof generator | `scripts/gen_proof_manifest.py:2`, `scripts/gen_proof_manifest.py:1234` | This imports the engine and emits marketing capability proof, not an install-free job verifier or r3.1 passport. Keep outside F33 job signing and do not label it authenticated job evidence. |
| Platform passport, review, API response, archive/HTML/PDF export; CLI signer/verifier and cached/failed-run exports | **asserted, verify before build** | Ground every caller and sink in those repos before claiming the complete emitter inventory. There is no local evidence for their function names. |

For F33, inner engine writes may complete only in a coordinator-owned private namespace. File ownership/permissions prevent untrusted mutation during sealing; checks, hashing, and publication use the same sealed bytes. Do not rely on a pathname `stat` comparison alone. A sink with immediate external visibility is refused for strict F33 use until it provides a tested staging adapter. Lower-level engine computation APIs remain usable, but cannot emit F33 evidence or release permits.

For filesystem releases, stage final bytes plus a signed statement whose state is `PUBLISHED`, then expose them together by atomic rename. Before rename, they are private candidates, never returned as published evidence. On failure, discard the candidate or create a separately signed `REJECTED` statement. For platform object storage, stage inaccessible immutable objects and the signed candidate, then expose a single transactional manifest/pointer referencing their immutable versions. Storage ACLs, cached URLs, review downloads, and API listing visibility are part of the grounding and race tests. No claim of cross-system atomicity without that evidence.

### 2.2 SENTRY

Proposed engine test: `tests/sentry/test_evidence_signing_boundary.py` (new). Companion sentries live in CLI/platform at locations determined during grounding.

The sentry has three independent parts:

1. Architecture: discover all F33 serializers/exporters and compare them to an explicit reviewed inventory; every inventory entry must exist and execute in a behavioral test. Ban signed-carrier construction and raw evidence serialization outside the boundary. AST/import checks supplement runtime tests; a regex alone is insufficient.
2. Fault injection: replace the signing boundary with a refusal and exercise success, failed-check evidence, retry, cache, worker, subset attachment, API download, HTML/PDF, archive, and administrative export paths. Capture actual returned bodies/files/visibility pointers. None may emit a newly created F33 bundle or release data. Existing signed export must instead pass its verification boundary and preserve bytes.
3. Non-vacuity: each path has a successful real-key control that emits a verifiable artifact. Mutation tests bypass signing, omit a required check, accept a raw dict, or commit early. The sentry must fail for each mutant. A newly discovered untested emitter is a gate failure, not an automatic allowlist addition.

The complete cross-repo SENTRY cannot pass from this checkout alone. Engine unit success is not F33 end-to-end completion.

### 2.3 Conformance vector set

Create a versioned, synthetic, redistributable corpus before implementation, proposed `tests/fixtures/signed_evidence/v1/` (new). Each vector includes exact statement/header bytes, public key, signature, artifacts, trust/policy snapshot, evaluation time/purpose, expected structured result, and expected exit class. Keep any test private keys clearly marked and unrelated to production. Record vector origin, generator implementation/version, and corpus digest.

Required vector families:

- Standards vectors: RFC 7515 ES256 example and RFC 8785 number/property-order cases. Separate generic JWS primitive vectors from full Decoy-profile vectors; the former lack mandatory Decoy headers and must not accidentally pass profile validation.
- Independent interoperability: valid Decoy fixtures produced by an implementation independent of the production Python verifier, plus Python-produced fixtures verified by it. KMS signatures verify offline without KMS access. Do not use only sign-then-verify tests sharing the same serializer.
- Encoding: UTF-16 key ordering versus Python ordering, astral Unicode, escaped/control characters, negative zero, float boundaries, safe-integer limits, decimal-string fields, duplicate keys, non-finite values, lone surrogates, trailing bytes, noncanonical JSON and malformed base64url.
- Signature/profile: altered protected bytes, empty/truncated signatures, wrong curve/key, `none`/HMAC/unknown algorithm, DER instead of JOSE signature, unsupported critical headers, unprotected identity, wrong `typ`/`cty`, missing signer/version, inconsistent `kid`, and multiple signatures. Add known-bad vectors from the jose-cookbook plus strict signature-encoding rejection: require fixed-width P1363 (64 bytes for ES256) and reject DER-encoded, truncated, and over-long signatures, proving the verifier uses a strict parser. Low-s canonical-form enforcement is explicitly OUT of scope: standard JOSE ES256 verification accepts both low-s and high-s signatures, enforcing a canonical form would require hand-rolled signature normalization that this plan forbids, and ECDSA non-malleability is not load-bearing here because publication binds to the SHA-256 statement digest, not to signature uniqueness.
- Content/policy: every A1-A4 and A9-A11 case, including absent artifacts, unknown schema, replayed attempt, forged smaller catalog, insufficient detector coverage, validly signed rejection, and legacy integrity-only evidence.
- Lifecycle: active, retired, revoked, unknown, conflicting, expired-policy, and backdated-history cases from A5. Record expected mathematical and trust results separately.
- Operations: empty output by declared policy, bounded large inventories, fixed resource-limit failures, all supported offline packaging targets, and all supported consumer adapters.

Property tests generate reordered dictionaries, supported numeric/Unicode values, and mutations of each bound field. File-byte mutation tests use real files. Keep expected outputs independently reviewed and frozen; never regenerate expected signatures/results from the implementation under test during CI.

## 3. PLAN

### 3.1 Ordering, design notes, and common gate

Order: S0 grounding and fixtures; S1 envelope/canonicalization; S2 engine signing boundary; S3 standalone verifier; S4 platform KMS; S5 lifecycle; S6 strict check/publication gate; S7 compatibility and coordinated rollout. Within each slice, tests precede implementation. S2 starts with a pure gate interface and deny-by-default publication; it cannot ship a producer before S6 completes. S3 and S4 integration wait for their own consumer grounding even if engine fixtures are ready.

Design notes: keep protocol/canonicalization/verification in a narrow dependency-light component with one authoritative schema/profile and policy evaluator. The engine's public facade and the CLI's standalone artifact consume the same versioned implementation without importing engine initialization. Dependency inversion isolates local signing from platform KMS changes; immutable carriers prevent post-sign mutation; a separate release evaluator prevents authentication from becoming accidental authorization. Use typed contexts, small single-purpose functions, and separate operations for signing, verifying, and publishing. Do not use a flag that turns a verifier into a publisher. Proposed package location and packaging metadata must be selected in S1 so standalone reuse does not become copied crypto code.

Common gate G: reviewed exact artifact, all specified acceptance/vector tests, lint/format/types on changed Python, relevant unit/integration/compatibility suites, and companion/no-companion tests where execution routes change. Measure changed-unit branch/line coverage and mutation strength for signature, trust, completeness, and publication decisions. Surviving security mutants are build work. Dennis runs adversarial code/security/QA review; Codex and Gemini supply the required cross-model crypto gate. Barry updates shipped durable documentation during implementation. This first draft deliberately leaves their review provenance empty. No push/gate receipt or completed-feature claim is authorized by a draft.

### S0. Ground consumers and freeze the acceptance corpus

**Scope:** establish the real r3.1 machine contract, old serializer, reserved fields, standalone verifier, all emitters, and `api/keys/` capabilities. Capture CLI/platform baseline commits. Read the passport implementation and schema in full. Read the actual F31 plan if available. Record a field-level mapping and actual file:line anchors; do not substitute roadmap prose for source. When re-opening the E1-E13 anchors, land on the exact field or statement line (or note "dataclass/function at :N, field at :M"), since several current citations point at the enclosing construct's start rather than the exact line.

**Verified engine anchors:** E1-E13 and the emission inventory. Consumer anchors: `decoy-platform/api/passport/`, `decoy-platform/api/keys/`, platform roadmap and signed-output-passport plan, CLI signer/verifier and `verify.py`: **asserted, verify before build**.

**Tests first:** import authentic legacy fixtures without rewriting them; freeze legacy hashes and verifier results. Define A1-A12 expected results and independent standards vectors. Prove a malformed/truncated corpus cannot pass through an empty test run.

**Gate:** **ground before building** for every consumer-dependent change. Missing consumer checkout blocks those integrations, not an honestly labeled engine core prototype. Schema reuse/migration approval is required before freezing S1's production field mapping. Do not create a replacement r3.1 schema just because it is absent here.

### S1. Envelope, canonicalization, and verifier core [engine-owned, buildable now]

**Scope:** add proposed typed statement/envelope/check/result carriers and narrow shared profile implementation. Pin JWS/JCS library choices after the extension/packaging spike. Implement strict parsing, JCS, ES256-only verification, identity matching, artifact hashing, and result dimensions. Pure protocol fixtures are buildable now; mapping the real legacy passport fields waits for S0.

**Verified anchors:** E2-E5; E3/E4 are existing hash/authentication contracts to preserve, not utilities to silently replace. New module paths are design outputs, not existing prerequisites.

**Tests first:** A1, A2, A9, A10, A11; canonicalization and algorithm-confusion vectors; independent interoperability and property tests. Baseline failure is missing signed-profile support, not an invented failing production test.

**Gate:** G plus exact wire/profile review by all crypto gate participants. Reject a dependency choice that forces handwritten crypto, engine import in the core, or unsupported external-signer injection. No production producer enabled yet.

### S2. Signing boundary and sealed engine evidence [engine-owned, buildable now]

**Scope:** implement the single signed-carrier constructor using the library and local test signer; capture immutable statement bytes and verify signatures before returning. Add safe engine fact adapters. Define a staging coordinator contract, release permit, and signed-rejection path. Wire supported engine-facing F33 routes to that boundary; application visibility adapters remain S4/S6 consumer work. Default release evaluator denies until the full catalog exists.

**Verified anchors:** E1, E6-E7, E13; `src/decoy_engine/execution/_pipeline.py:313`, `src/decoy_engine/execution/_transactional_sink.py:269`, and every engine site in section 2.1.

**Tests first:** A7, A8, A11, A12; mutate a source dictionary after signing and show emitted bytes do not change; prove each admitted route stages privately. Refuse non-staging sinks and keep normal computation APIs compatible. Test rejected evidence independently of data-release capability.

**Gate:** G plus engine SENTRY, including successful controls and killed bypass mutants. Do not claim platform/CLI emission coverage. No signed producer goes live before S6 and S7.

### S3. Offline verifier and CLI signer [consumer: ground before building]

**Scope:** wrap the shared verification/signing semantics in CLI commands; preserve existing exit/argument contracts after inspection. Build unpack-and-run signed verification with all dependencies offline and no engine import. Preserve legacy `verify.py` hash verification once located. CLI local keys use established key serialization and explicit authorized secret input; never derive signing keys from job seeds or masking keys.

**Anchors:** engine E2 and the shared S1 artifact once built. CLI verifier/signer, standalone script, packaging and exit-code definitions: **asserted, verify before build**.

**Tests first:** A1, A5, A6, A9, A10 and the full vector corpus in clean Python 3.10-3.12 environments. Disable sockets, clear import paths/site packages, and prove `decoy_engine` cannot be imported. Exercise the distributed artifact rather than the developer checkout. Assert partial verification is nonzero.

**Gate:** G plus **ground before building** CLI changes, tested platform matrix, dependency provenance/hashes, and identical structured results across standalone and engine consumers. Failure to package crypto offline blocks signed-verifier release; no pure-Python homemade primitive fallback.

### S4. KMS interface and platform passport boundary [consumer: ground before building]

**Scope:** adapt the merged `api/keys/` external-KMS envelope to the established versioned signer seam. Resolve and authorize signer identity before signing, locally verify provider results, handle retry/timeout without fallback, and make passport/export persistence consume signed immutable bytes. Preserve the existing key envelope and lifecycle architecture.

**Anchors:** E1 proves the masking provider is different. `decoy-platform/api/keys/`, `api/passport/`, review/export services, storage visibility and authorization points: **asserted, verify before build**. Provider source pattern is Sigstore; concrete raw/digest semantics follow the chosen provider's official API.

**Tests first:** A7-A9, A11-A12; provider contract tests with a real nonproduction KMS integration as well as deterministic fakes. Test wrong tenant/version/key, alias races, lost responses, signature format conversion, and local verification. No production-key operations during testing.

**Gate:** G plus **ground before building**, proof of actual `api/keys/` reuse, backend capability matrix, exact signing-input parity, and platform emission inventory/SENTRY. A merged encryption envelope alone does not pass the asymmetric-signing gate.

### S5. Rotation, revocation, and truthful history [engine core now; consumer lifecycle gated]

**Scope:** implement pure trust-policy evaluation in the shared verifier; retain public keys/version identities and represent current versus historical results separately. Platform lifecycle changes reuse `api/keys/`; CLI distributes/imports trust snapshots explicitly. Pin policy generation at release and recheck it under publication authorization.

**Anchors:** E1's version concept is masking-specific. New verifier trust logic is S1-owned. Platform key-version storage, rotation/revocation, audit events and CLI trust import: **asserted, verify before build**.

**Tests first:** A5, A7, A10 with time and policy snapshots fixed. Include revoke-between-sign-and-publish, unknown compromise time, conflicting key IDs, expired trust, archive replay, and rotation retry. Assert historic bytes and mathematical outcomes remain unchanged.

**Gate:** G; **ground before building** lifecycle mutations. Review must reject acceptance based only on signer-asserted time. Before any real rotation/revocation, require the R3 action approval, recovery procedure, retained verification material, and tested dry run.

### S6. Inconclusive check ledger and strict release gate [engine core now; publication gated]

**Scope:** record explicit per-detector and per-scan completion at execution, preserving legacy finding APIs through adapters. Compile the catalog's expected subject set before checks. Add safe failure mapping and source/coverage identities; evaluate required results against trusted policy. Bind eligibility to the exact sealed artifact set. Wire platform/CLI release visibility to this result and the signed carrier.

**Verified anchors:** E5-E12; `src/decoy_engine/validation/post/_checks/__init__.py:31`; all section 2.1 commit sites. Platform/CLI required-policy selection, check scheduling, review approval and publication transactions: **asserted, verify before build**.

**Tests first:** A3, A4, A8, A11, A12; missing NER/custom detector, invalid regex, skipped scan, runner exception, no-match success, partial sample, no applicable subjects, and optional inconclusive controls. A validly signed rejection must be exportable while every release/download visibility probe fails. For bounded routes, prove required checks run against sealed output; otherwise refuse strict release explicitly.

**Gate:** G plus engine/CLI/platform SENTRY across all admitted routes. **ground before building** consumer enforcement. Catalog reviewed as policy, not reconstructed from whatever results happen to exist. A report-only `post_validation_enforce` field is insufficient evidence of this gate.

### S7. Compatibility, views, and coordinated activation [consumer: ground before building]

**Scope:** field-level legacy mapping, read-first rollout, explicit migration/re-attestation, support-window documentation, HTML/PDF truthfulness, and rollback that pauses strict releases. Publish the shared profile and conformance corpus version alongside verifier artifacts. Reconcile the platform roadmap's V1 status only after all acceptance evidence exists.

**Anchors:** E3-E4 unchanged legacy hashes; subset and proof distinctions in section 2.1; compatibility provisions read in `docs/compatibility-contract.md`. CLI legacy formatter/verifier, platform r3.1 schema, `/proof`, review/export, roadmap and prior signed-passport plan: **asserted, verify before build**.

**Tests first:** A6, A8, A10-A12; old bundle/current reader, new bundle/old reader rejection, legacy re-attestation, renderer labels, partial artifact delivery, interrupted rollout, rollback, and exact-byte export through each surface. Establish actual old-reader behavior instead of assuming unknown fields are ignored safely.

**Gate:** G plus **ground before building**, full cross-repo A1-A12, support matrix published, all consumer emitter inventory entries tested, Barry's durable-doc sync, and adversarial final crypto verdicts for exact artifacts. No completed F33 claim while a consumer slice remains asserted.

## 4. Cross-repo contract and handoff

| Owner | Owns | Consumes | Required handoff evidence |
|---|---|---|---|
| Engine | Typed statement/check contracts, canonical signing/verification semantics, safe fact projection, pure policy evaluator, local signer seam | Caller-provided issuer/job/attempt/snapshot context, required policy, signer and immutable artifact facts | Versioned schema/profile, vectors, public facade, SENTRY, unchanged computation compatibility |
| CLI | Standalone no-engine verifier, local signer commands, artifact filesystem handling and offline distribution | Shared narrow implementation, trusted keys/policy supplied independently | Clean-host verification, exact exit mapping, legacy fixture mapping, complete command/export inventory; anchors **asserted, verify before build** |
| Platform | Passport assembly, review/export, immutable release visibility, tenant authorization, KMS, lifecycle, trust distribution | Engine contract and the merged `api/keys/` envelope | Grounded `api/passport/` and `api/keys/`, actual schema/serializer/verifier sources, provider capability proof, transaction race tests, signed rejection example; anchors **asserted, verify before build** |

One wire profile and one conformance corpus define semantics. Do not fork schema rules or crypto implementations per surface. The standalone distribution must be able to reuse the shared component without importing the engine. Platform-generated evidence must pass the standalone verifier; CLI-generated evidence must pass platform ingestion with the same independently trusted identity/policy inputs.

Required grounding deliverable from each consumer owner: baseline commit; opened file:line anchors; full legacy schema/serializer and verifier inspection; emit/call graph including failures, retries, caches and administrative paths; key/policy authority model; staging/visibility proof; fixture corpus; supported runtime matrix; and unresolved differences. Re-enter the plan gate if these facts contradict the proposed profile or publication model. No unverified anchor becomes an implementation prerequisite by repetition.

Draft verification performed: baseline and clean initial worktree inspected; source searches and numbered reads completed; established standards and upstream library/provider sources consulted. This document specifies tests; it does not claim implementation tests, cross-repo inspection, KMS interoperability, or independent review have run.

## 5. Provenance stub

### Gemini review

Gemini 2.5 Pro (crypto plan gate, 2026-10-29): **GO-WITH-FIXES**. Assessed as a model for a critical cryptographic feature; the signature, canonicalization, trust, KMS, and release-gate design is sound and correctly rejects header-trust, algorithm confusion, and unsigned fallback. Six fixes folded: (High-1) the verifier takes a typed `VerificationContext` as the sole source of trusted expected identity, never derived from the bundle, closing cross-job replay; (High-2) `coverage` is a machine-readable check-outcome field enforced against policy minimums, so a zero-row or under-covered required check is `INCONCLUSIVE` not `PASS`; (Med-3) the verifier result carries the untrusted `created_at` and the verifier's own `evaluation_time` as distinct fields; (Med-4) the low-level JWS verify is private and only the full Decoy-profile verifier is public; (Low-5) negative conformance vectors add ECDSA malleability, truncated/over-long signatures, and jose-cookbook known-bad cases; (Low-6) the standalone verifier documents its host-interpreter trust boundary.

### Dennis review

Dennis (repo-grounded crypto plan gate, 2026-10-29): **GO**, 0 blocker / 0 high / 0 medium / 2 LOW. Verified every E1-E13 and section-2.1 emission anchor first-hand against baseline `2a44723f`; confirmed no phantom prerequisites (no signing fields, `verify.py`, passport module, or `api/` in the checkout; the model-pack HMAC correctly rejected as a signing base). Judged the crypto sound (detached ES256 JWS + JCS, verify over the exact transmitted protected bytes, AWS-KMS DIGEST single-hash + DER->P1363 conversion, established libraries only, the return-to-gate guard real), the trust/release separation honest (VerificationContext sole trust source, coverage-enforced INCONCLUSIVE, independently-trusted minimum catalog, two-dimensional historical-truth table), and the signing sentry non-vacuous. Both LOWs folded: (LOW-1) S0 lands exact anchor field/statement lines; (LOW-2) the malleability vector is clarified to strict P1363 encoding rejection with low-s canonicalization explicitly out of scope. A separate Codex cross-model crypto pass runs at the BUILD gate for this crypto/MAJOR feature.
