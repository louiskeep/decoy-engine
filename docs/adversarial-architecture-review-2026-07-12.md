# Decoy Engine Adversarial Architecture Review

**Review date:** 2026-07-12
**Frozen reviewed revision:** `c1c4f2c2b33af39e1de4874788a7df78a352970c` (`main`)
**Release phase:** `pre-ga`
**Scope:** `decoy-engine` architecture, application behavior, cryptography, data integrity,
performance methodology, packaging, public contracts, tests, and operations
**Change boundary:** report only; production source was not modified

## Concurrent-Work Notice

After the full final-baseline suite completed, the shared checkout moved to
`track-b/tb1-out-of-core-streaming` and acquired execution changes. They were initially uncommitted
during review and were later committed/pushed as `9244847`. The change adds lazy source handles and
a streaming sink to the isolated/governed relationship path, addressing part of DE-09 and the
governor budget gap in DE-15. It is post-baseline and was not accepted as closing either finding:
the direct public `source_loader` branch in `run_out_of_core_route` still eagerly resolves all
missing tables, and the frozen full gate did not run on `9244847`. Re-run the DE-09 bite and full
gate on that commit before changing disposition.

## Executive Verdict

Decoy is a substantial pre-GA masking and synthetic-generation library with a coherent plan and
execution model, unusually broad tests, typed errors, deterministic compatibility controls, and
several well-designed fail-closed checks. It is not ready for a security or GA claim at its current
runtime and release boundaries.

Three engine-level issues are direct release blockers:

1. The shipped FPE strategy is a custom HMAC/Feistel cipher whose input-domain behavior can retain
   raw identifier characters or entire values. A fallback introduced to prevent one leak is not
   reversible, despite FPE being presented as reversible.
2. Masking and vault keys derive from the integer reproducibility seed. The effective secret is at
   most 64 bits, defaults to zero in lower-level paths, and is commonly shown with low values.
3. Source schema is open by default. A new or omitted column receives no work node and is emitted
   unchanged on every route.

Separately, the ML pack loader accepts unsigned joblib artifacts by default. A matching SHA-256 in
an adjacent manifest is not authentication and joblib deserialization can execute code. This is a
HIGH unsafe-provenance defect in the engine as reviewed. It becomes CRITICAL if a less-trusted
tenant, job author, archive, or writable directory can influence both pack selection and contents;
that platform/CLI trust boundary was outside this review.

These are not theoretical concerns inferred from naming. Synthetic counterexamples reproduced the
three masking-boundary failures and the joblib execution mechanism. Several current tests
intentionally assert unsafe behavior, which is why the final suite can be green while the findings
remain valid.

**Release decision:** do not flip `RELEASE_PHASE` to GA, publish a distribution built from the
current dirty workspace, or market FPE, vault, disguise-pack, or post-validation behavior as a
complete confidentiality boundary until the critical findings and their verification gates are
closed.

## What Decoy Does

`decoy-engine` is an in-process Python data-plane library. It has no server, authentication layer,
or background daemon of its own. It runs with the caller's privileges and is consumed by the Decoy
CLI and platform.

Its main V2 lifecycle is:

```text
PipelineConfig validation
        |
        v
profile config source descriptors (bounded by default)
        |
        v
compile frozen Plan + SeedEnvelope
        |
        v
build namespaces + relationship graph
        |
        v
relationship route: sequential / out_of_core / full_frame / reject
        |
        v
non-FK route: pandas / Polars / auto-chunk
        |
        v
mask and/or generate -> validators/quarantine -> ExecutionResult or sink
```

Related capabilities sit beside this lifecycle rather than consistently inside it:

- STORM profiles data and detects likely PII.
- Post-mask STORM is called by the platform after a successful mask.
- FK-aware subsetting is a separate `run_subset` operation that callers must compose manually.
- Token vaults record reversible source-to-mask mappings for otherwise one-way strategies.
- Test-flight and proof manifests provide acceptance/evidence layers outside the core execution
  result.

The most important architectural fact is that profiling reads `config["sources"]`, while execution
uses a separate caller-supplied `sources` mapping. The compiled plan is not cryptographically or
structurally bound to the exact object that is later masked.

## Architecture Directory

| Area | Primary paths | Responsibility | Review conclusion |
|---|---|---|---|
| Public config | `src/decoy_engine/config/` | Strict Pydantic schema and cross-field rules | Strong schema discipline, but accepted fields do not all have runtime owners |
| Profiling | `src/decoy_engine/profile/` | Source metadata, bounded samples, PII/profile facts | Bounded-read fix landed; default sample is a biased head sample |
| Plan | `src/decoy_engine/plan/` | Frozen transformation and audit plan | Useful choke point; omits undeclared columns and does not bind the execution source |
| Determinism | `src/decoy_engine/determinism/` | HKDF/HMAC derivation and stable indices | Good domain framing; fed by insufficient and conflated key material |
| Execution spine | `src/decoy_engine/execution/_pipeline.py` | Profile, compile, route, execute, finalize | Central contract is broader than behavior; multiple route authorities remain |
| Adapters | `execution/_pandas_adapter.py`, `execution/polars/` | Plan execution on pandas/Polars | Pandas is semantic oracle; some Polars-native claims are actually pandas ports |
| Relationships | `src/decoy_engine/relationships/`, `execution/out_of_core/` | Namespace, FK resolution, bounded joins | Considerable parity coverage; large-int corruption and lazy-loader residency gap remain |
| Strategies | `execution/_strategies/`, `transforms/` | Hash, FPE, redaction, generation, derived transforms | Many checks are sound; FPE and pool contracts are unsafe |
| Generation | `src/decoy_engine/generation/`, `generators/` | Synthetic tables, pools, composite values | Broad feature set; open generator parameters and seed-identity coupling |
| Privacy checks | `src/decoy_engine/storm/`, `validators/`, `validation/post/` | PII discovery and post-output checks | Useful components, but not one pre-commit safety barrier |
| Durable artifacts | `vault.py`, `quarantine.py`, transactional/isolated sinks | Vault, quarantine, outputs, evidence | No single transaction spans every artifact |
| Packaging/CI | `pyproject.toml`, `.github/workflows/`, `testflight/` | Distribution and gates | Source gates are strong; artifact and compatibility gates are materially weaker |

## Severity Method

- **CRITICAL:** plausible raw-data disclosure, code execution, or systemic bypass of the claimed
  masking boundary. Blocks publication/GA.
- **HIGH:** silent data corruption, false success, failed-job publication, OOM on a bounded route,
  or a release/evidence control that cannot support production claims.
- **MEDIUM:** material inefficiency, misleading capability/telemetry, or a correctness gap with a
  narrower precondition or lower direct impact.
- **LOW:** documentation, hygiene, or release-process drift that does not directly change output.

Risk ranking assumes ordinary production conditions: upstream schemas can evolve; configs and
masked outputs may be copied independently; and malformed real-world identifiers can occur. DE-04
is HIGH because this in-process library did not establish an untrusted pack-selection boundary: its
direct caller already has Python execution privileges. Escalate it to CRITICAL if the platform lets
a less-trusted principal control both selected pack files across a privilege boundary. An FPE leak
requires an out-of-domain value; the current contract does not validate or reject that condition.

## Findings Directory

| ID | Severity | Problem statement | Required outcome |
|---|:---:|---|---|
| DE-01 | CRITICAL | FPE retains raw input outside its charset, uses custom crypto, and is not always reversible | Replace or strictly contain FPE; no fail-open values |
| DE-02 | CRITICAL | Public/reproducibility seed is cryptographic key material | Separate strong independent mask keys from public seeds |
| DE-03 | CRITICAL | Undeclared source columns pass through unchanged | Closed output schema; explicit passthrough only |
| DE-04 | HIGH | Unsafe-by-default joblib provenance becomes code execution across an untrusted pack boundary | Safe model format or authenticated provenance |
| DE-05 | HIGH | Profile and executor can consume different sources; missing sources succeed | One bound source contract and output completeness checks |
| DE-07 | HIGH | Sdist membership depends on workspace state; wheel omits default ML pack | Hermetic, installed-artifact release gate |
| DE-08 | HIGH | Quarantine writes before sink commit | Run-scoped publication protocol across durable artifacts |
| DE-09 | HIGH | Lazy out-of-core route eagerly loads every table and emits false telemetry | Pass lazy handles through public route; lifecycle memory proof |
| DE-10 | HIGH | Pandas/sequential FK resolution rounds integers above `2**53` | One lossless FK typing contract across routes |
| DE-11 | HIGH | Pool preflight and runtime disagree on capacity and config location | One typed PoolSpec and shared invariant |
| DE-13 | HIGH | Evidence version is caller-controlled and stale | Engine-owned version provenance |
| DE-14 | HIGH | Supported Python/artifact and performance/OOM contracts lack CI ownership | Installed-artifact matrix and real benchmark gates |
| DE-06 | MEDIUM | Engine-owned sink/validation contracts are ignored or unreachable; orchestration ownership is unclear | Honest runtime contract and documented ownership |
| DE-12 | MEDIUM | Bounded profiling marks samples but omits deterministic-head method/coverage provenance | Representative/provenanced sampling; sampled facts never prove safety |
| DE-15 | MEDIUM | Memory routing has multiple authorities and opt-in safety | One initial plan plus an auditable runtime transition record |
| DE-16 | MEDIUM | Polars-native labels include pandas ports; `max_workers` is inert | Truthful capability enum and telemetry |
| DE-17 | MEDIUM | Public auto-chunk reassembles and copies every chunk | Sink-aware streaming without whole-output retention |
| DE-18 | MEDIUM | Generator extras accept typos; same-config columns can be identical | Typed built-ins, explicit extensions, and stable generation IDs |
| DE-19 | MEDIUM | Typing, public API, extras, and installed-wheel claims are not tested together | Consumer contract tests for every advertised surface |
| DE-20 | LOW | Security, CODEMAP, version, and release documentation drift | Generate stable facts and add docs sentries |

## Critical Findings

### DE-01 - FPE can disclose raw identifiers and is not an approved construction

**Problem.** Decoy calls a custom eight-round HMAC/Feistel permutation FPE. It is explicitly not
FF1. Values outside the configured charset are handled by retaining those characters, returning the
whole value unchanged, or applying a one-way covering hash. These behaviors respectively disclose
source material or violate the claimed inverse.

**Evidence.** `transforms/fpe.py:9-25,55-82,166-177,366-452`; the repository's own disclosure at
`docs/what-we-cannot-prove.md:147-169`; and passing leak demonstrations in
`tests/unit/disguises/test_pack_charset_no_leak.py:61-111`. A synthetic probe produced:

```text
STATUS-1 -> STATUS-3       # raw prefix retained
STATUS-1 -> STATUS-1       # preserve_separators=False, complete no-op
---      -> 297 -> 456     # covering fallback does not invert
```

This affects a first-class product path. Regulatory disguise packs under
`src/decoy_engine/disguises/` use FPE for SSNs, MRNs, NPIs, account numbers, device IDs, VINs, and
other identifiers.

**Resolution.** Until replacement, reject any non-domain value and reject false separator mode for
sensitive fields. Replace the custom primitive with a reviewed FF1 implementation that satisfies
current domain-size guidance, or use tokenization. Define typed formats so punctuation and
data-bearing characters are not inferred from charset membership. Record algorithm/key versions
and provide a protocol migration/rekey plan. Do not describe the existing implementation as
NIST-modeled merely because its key/tweak shape resembles FF1.

**Verification.** Use cross-implementation known-answer vectors. No out-of-domain value is
admitted; every admitted value round-trips exactly; mixed-alphabet inputs either match a complete
typed domain or fail before output. Instrumented/structural tests must prove that every admitted
character participates in the permutation rather than being copied by the separator branch.
Whole-value fixed points need an explicitly documented policy; per-position equality is not proof
of passthrough. Test every disguise rule through its actual config.

**Sources.** [NIST SP 800-38G](https://csrc.nist.gov/pubs/sp/800/38/g/upd1/final) specifies the
approved FPE methods; its [Revision 1 second public draft](https://csrc.nist.gov/pubs/sp/800/38/g/r1/2pd)
raises FF1 domain-size requirements; [OWASP Cryptographic Storage](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html)
advises against custom algorithms.

### DE-02 - Cryptographic strength is capped by an integer seed

**Problem.** The same `global_settings.seed` serves reproducibility, HMAC masking, FPE, unmasking,
and vault encryption. It is normalized to eight bytes and therefore provides at most 64 bits before
considering the frequent use of small human-selected values. HKDF cannot repair low input entropy.

**Evidence.** `config/_global_settings.py:33`; `plan/_seed.py:35-108`;
`execution/_pandas_adapter.py:171-178`; `execution/_strategies/_hash.py:52-57`;
`execution/_strategies/_fpe.py:110-115`; `vault.py:115-127`. The mask path does not use the 32-byte
master-key resolver described by `docs/security/key-derivation.md`. Synthetic enumeration recovered
the example seed 42 from a vault and a known-plaintext hash in 43 attempts.

**Resolution.** Keep a non-secret reproducibility seed for generation. Require a separate 256-bit
masking secret or opaque `KeyProvider`/resolver for keyed masking and vaults. At the engine
execution boundary, accept only key bytes plus stable key/version IDs, derive versioned purpose
keys, and fail before output creation if a keyed strategy has no secret. The host platform owns
KMS/HSM storage, tenant authorization, rotation orchestration, and worker isolation; the engine
must support those controls without adding a network dependency.

**Verification.** Same seed/different secret differs; same secret/version remains stable; missing
secret fails; logs/plans contain no key; config/output does not enable a small offline key search.

**Sources.** [RFC 5869](https://www.rfc-editor.org/rfc/rfc5869.html),
[NIST SP 800-57 Part 1](https://csrc.nist.gov/pubs/sp/800/57/pt1/r5/final), and
[OWASP Key Management](https://cheatsheetseries.owasp.org/cheatsheets/Key_Management_Cheat_Sheet.html).

### DE-03 - Schema drift is a direct raw-data path

**Problem.** Only configured columns become plan nodes, but adapters carry the entire input frame to
the output. An upstream schema addition is silently published unchanged.

**Evidence.** `plan/_seed_envelope.py:77-100`; `_pandas_adapter.py:168-180,243-245`;
`polars/_polars_adapter.py:190-207,230-240`; and wide unplanned payload construction at
`tests/perf_fixtures/fk_relational.py:53-56,88-99,143-176`. A valid pipeline redacted `ssn` while
preserving an undeclared `new_sensitive='secret-tail'` column. Add a focused regression test rather
than treating the performance fixture as a policy specification. Closed-schema defaulting is this
review's architectural conclusion, not a direct requirement quoted from the external sources.

**Resolution.** Compile an exact output schema. Default unknown-column policy to error before GA;
allow explicit drop; require every intentional passthrough to be declared and acknowledged. Apply
one route-independent schema and residual-PII postcondition before transaction commit.

**Verification.** Inject a new sensitive column into full-frame, Polars, sequential, chunked, and
out-of-core jobs. Every run must fail before any table, quarantine, vault, or manifest publication.

**Sources.** [NIST SP 800-188](https://csrc.nist.gov/pubs/sp/800/188/final) and
[OWASP allowlist validation](https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html).

## High Findings

### DE-04 - Unsafe model-pack provenance can become a code-execution boundary

**Problem.** A public path parameter selects a joblib artifact. The loader verifies a SHA-256 value
from the adjacent manifest, accepts an empty signature by default, and then deserializes with
joblib. If a less-trusted actor can write both adjacent files and influence pack selection without
already having arbitrary Python execution, deserialization crosses a privilege boundary and runs
with the host's privileges. This review proved the mechanism but did not trace that precondition
through the platform/CLI, so the engine finding is HIGH rather than unconditionally CRITICAL.

**Evidence.** `storm/model_pack/classify.py:41-60,102-105` and
`storm/model_pack/loader.py:122-129,206-295`. A synthetic unsigned pack with a correct self-declared
hash created a marker file during `ModelPackLoader.load()`. The existing per-instance HMAC is useful
inside the repository's stated single-host trust model when enabled, but it is optional and is not
software-publisher identity for a distributed wheel.

**Resolution.** Prefer an inspectable, non-pickle representation such as an appropriate ONNX or
reviewed skops format. If joblib remains during migration, the engine should expose a fail-closed
loader/verifier that requires authenticated provenance and restricts pack selection. The host must
pin the trust root and isolate deserialization, unless subprocess isolation becomes an explicit
engine feature.

**Verification.** Unsigned, wrong-signer, altered, path-substituted, and dependency-incompatible
packs fail before deserialization; installed default pack loads from package resources; no fallback
can conceal a missing production model. Add a platform boundary test proving who controls
`pack_dir` before assigning CRITICAL severity.

**Sources.** [joblib.load](https://joblib.readthedocs.io/en/stable/generated/joblib.load.html) and
[scikit-learn model persistence](https://scikit-learn.org/stable/model_persistence.html).

### DE-05 - Plan/source split produces false success

`run_pipeline` profiles descriptors from config, then executes a separate Arrow mapping. No
identity or schema binding connects them. Missing planned frames are skipped by both adapters. A
valid mask pipeline with `sources=None` returned no outputs and success telemetry saying it was
fully loaded in memory.

**Resolution:** introduce one `SourceHandle` used for metadata, sampling, and execution. Validate
expected source and output table sets, schemas, and source identity before and after execution.
Missing/extra tables must be typed failures.

**Evidence:** `_pipeline.py:296-315,449-477`; `_pandas_adapter.py:213-215`;
`_polars_adapter.py:205-207`; `_pipeline.py:189-191`.

### DE-07 - Distribution membership is workspace-dependent and the wheel is incomplete

A clean export of `c1c4f2c` produced a 25 MB/1,003-entry sdist and a 1.1 MB/377-entry wheel. A build
from the later dirty shared workspace produced a 49 MB/3,116-entry sdist containing 1,279 nested
worktree paths, 1,154 Hypothesis-cache matches, and current untracked review documents. The latter
is workspace-state evidence, not frozen-commit membership, but proves the default sdist selection
can ingest non-release files. Both clean and dirty wheels omitted `py.typed` and the default ML
model/manifest. In an extracted-wheel probe, `_DEFAULT_PACK` did not exist and `classify_fields`
returned `None`.

**Resolution:** explicit Hatch allowlists for wheel/sdist; package runtime data under
`decoy_engine` and load through `importlib.resources`; build only from a clean CI checkout; inspect
archive membership; install/test both artifacts; sign/attest the exact tested bytes. Do not publish
an sdist assembled from the current dirty workspace.

**Evidence:** `pyproject.toml:221-222`; `storm/model_pack/classify.py:37-38`; dirty-tree build record
in [contracts-tests-operations.md](review-notes/2026-07-12/contracts-tests-operations.md); clean
export rebuild in [report-adversarial-check.md](review-notes/2026-07-12/report-adversarial-check.md).

**Sources:** [Hatch sdist defaults](https://hatch.pypa.io/1.12/plugins/builder/sdist/),
[Hatch build selection](https://hatch.pypa.io/1.10/config/build/), and
[PyPA packaging flow](https://packaging.python.org/en/latest/flow/).

### DE-08 - Quarantine publishes raw rows outside the commit protocol

Sequential execution writes final quarantine JSONL before `TransactionalSink.commit()`. A commit
failure aborts only table staging, leaving the raw-row sidecar behind. The writer uses the final
path with truncating mode and no run-level rollback.

**Resolution:** define a run-scoped transaction domain. Stage under one root where the storage
backend permits, publish data artifacts first, and publish an authenticated success manifest or
commit marker last; readers must reject artifacts without that marker. Across heterogeneous sinks,
require idempotency and compensation rather than claiming physical all-or-none atomicity. Decide
explicitly whether failed-run quarantine is a protected diagnostic artifact or must be destroyed,
and apply restrictive access and retention either way.

**Evidence:** `_sequential.py:369-408`; `quarantine.py:260-275`. The focused probe left a 226-byte
quarantine file after `commit boom`.

### DE-09 - Public out-of-core loading defeats out-of-core execution

The lower runner accepts `LazySource`, but the public route invokes every `source_loader` and
retains all resulting `pa.Table` objects before dispatch. Telemetry is calculated from the original
caller shape and reports `loaded_fully_in_memory=False`.

**Resolution:** make the public loader return lazy/batch handles; pass them unchanged; compute
telemetry from resolved capabilities; prove public-route RSS under a cgroup/hard cap with full-table
reads forbidden.

**Evidence:** `_pipeline_route_exec.py:195-248`; `out_of_core/_runner.py:85-113,141-196`.

### DE-10 - Pandas silently corrupts large FK identifiers

A mixed float-parent and preserved/warned integer orphan above `2**53` coerces to float64. The
source `9007199254740993` becomes `9007199254740992.0`. Out-of-core correctly rejects the same
shape, so route selection changes correctness.

**Resolution:** define one Arrow-native, lossless FK output type contract. Cast only after proving
representability; otherwise every route raises the same typed error. Stop treating pandas coercion
as the oracle for key data.

**Evidence:** `_pandas_adapter.py:424-453`;
`tests/parity/test_out_of_core_fk_parity.py:997-1057`;
`tests/parity/SEMANTIC_DIFFERENCES.md:40-47`.

### DE-11 - Pool capacity is checked against the wrong quantity and lost in the plan

Compile checks UNIQUE against source distinct count, while runtime UNIQUE draws once per row and
requires row count not exceed pool size. Top-level `pool_size` is not carried into runtime
`provider_config`, so the handler silently uses 10,000. A synthetic 500-row/50-distinct job declared
pool size 200, compiled, and produced 500 distinct outputs from the undeclared runtime pool.

**Resolution:** one typed `PoolSpec` in the plan; one field location; one shared capacity function;
UNIQUE uses non-null output-row count. Add config-to-handler boundary tests.

**Evidence:** `generation/pool/_validate.py:78-136`; `_sampler.py:118-134`;
`plan/_seed_envelope.py:203-207`; `execution/_strategies/_faker.py:46-89`.

### DE-13 - Audit evidence accepts any caller version

The package is 0.3.0, but `run_pipeline` requires an arbitrary version string, README uses 0.1.0,
the proof generator/manifest use 0.2.0, and the sentry compares the stale generator to the stale
artifact. Evidence can therefore be internally green and factually wrong.

**Resolution:** derive runtime version from installed distribution metadata, with a single source-
tree fallback; remove the public override except explicit test injection; gate package, tag,
changelog, plan, and proof-manifest equality.

**Evidence:** `pyproject.toml:10`; `__init__.py:262`; `_pipeline.py:153-187,315`;
`README.md:49-56`; `scripts/gen_proof_manifest.py:54-59`; `docs/proof-manifest.json:2`.

### DE-14 - CI does not own the supported runtime or performance contract

Metadata accepts every Python `>=3.10` while classifiers name 3.10-3.12; workflows mostly use 3.10
and do not test 3.12 or newer. Broad pip resolution ignores the lock in most jobs. Tests execute
editable source, not built artifacts. Eight benchmark-marked job/OOM gates live outside the
directory the benchmark workflow runs. Its collected Arrow-to-pandas benchmark has four parameter
cases plus a summary test, but asserts shapes/columns rather than performance-regression limits.

**Resolution:** cap `requires-python` to an owned range or test every accepted interpreter; add a
frozen determinism lane, min/latest dependency lanes, built-wheel install matrix, and `pip check`;
give every marker one CI owner; add calibrated performance gates on controlled hardware and a
public-route memory sentinel.

**Evidence:** `.github/workflows/ci.yml`, `benchmark.yml`, `testflight.yml`;
`tests/perf/test_job_performance_gates.py`; `tests/perf/test_out_of_core_memory_sentinel.py`; full
details in [contracts-tests-operations.md](review-notes/2026-07-12/contracts-tests-operations.md).

## Medium and Low Findings

### DE-06 - Runtime contracts are ignored and orchestration ownership is unclear

Two engine-owned defects are concrete: `sink` is honored only by sequential/out-of-core routes,
while full-frame and public auto-chunk silently retain output; and validated nested
`post_validation` cannot be consumed by the flat key expected by `PostValidationRunner`, which has
no production call from `run_pipeline`. Other accepted fields have different ownership questions:
`subset` is deliberately exposed as separate `run_subset`; `run_storm` belongs to the platform;
and targets may be caller metadata. Bundling them into `PipelineConfig` without an explicit owner
still makes the public contract misleading, but this engine-only review did not demonstrate a
default-on HIGH publication or memory consequence.

**Resolution:** make engine-owned sink and validation behavior route-independent, or reject those
options on unsupported routes. Publish a field-ownership table. Then either provide a host-level
orchestrator for subset -> profile -> compile -> execute -> validate -> publish, or narrow the core
config/API to fields the engine actually owns.

**Evidence:** `config/_pipeline.py:72-99,249-299`; `subset/_api.py:1-7`;
`validation/post/_runner.py:49-85`; `_pipeline.py:382-418,568-593`.

### DE-12 - Bounded profiles omit sampling-method provenance

SC7a fixed eager full-source reads, and profiles correctly mark statistics as sampled while
preserving total row count and row-count exactness. However, Parquet, fixed-width, CSV, and cloud
readers take the first N records without recording that method or its coverage; the stamped seed
does not affect those first-N reads. Ordered tails can therefore hide nulls, categories, widths,
PII shapes, and cardinality. The cited uniqueness-saturation use is behind
`use_probe_routing=False` by default, so a default-on HIGH consequence was not demonstrated.

**Resolution:** separate exact metadata from estimated statistics; use deterministic row-group
stratification or reservoir sampling where a full stream pass is acceptable; stamp method,
coverage, size, and uncertainty. Never use sampled distinctness as proof of safe admission.

**Evidence:** `profile/_readers.py:134-227`; `_source.py:207-224`;
`_walk.py:109-125`; `_pipeline_routing_signals.py:418-434`. The probe missed all tail-only nulls
and categories in a 200-row ordered Parquet file sampled at 100.

### DE-15 - Multiple routing authorities

The live relationship router, static planner, chunk classifier, estimator/probe, compatibility
gates, and runtime governor do not produce one authoritative object. Full planner routing and the
governor are disabled; byte/probe safety is opt-in; defaults are calibrated to one 32 GB width-16
fixture.

**Resolve:** use one authoritative execution-decision state machine: an immutable initial plan plus
append-only runtime transitions for measured RSS, source failure, or OOM. Verify that the explained
initial route equals the first attempted route, then record every attempted route, transition
reason, measured budget/RSS, and final executed route.

### DE-16 - Polars reporting is not substrate truth

Seven registered Polars handlers are pandas ports, yet every registry key is called native and
telemetry reports Polars. `max_workers` is accepted/stamped but unused.

**Resolve:** one capability enum: `native`, `ported`, `job_fallback`, `unsupported`; drive routing,
docs, telemetry, and benchmarks from it. Reject inert non-default knobs.

### DE-17 - Auto-chunk is not end-to-end streaming

The public route starts with a resident Arrow table, materializes every output chunk and result,
concatenates, and calls `combine_chunks()`. Peak memory can include source, all chunks, concatenated
table, and contiguous copy.

**Resolve:** stream each batch into a transactional sink and aggregate only compact metrics. Avoid
`combine_chunks` unless a caller explicitly requests contiguous resident output.

### DE-18 - Generation input and identity contracts are weak

`GenerateColumnConfig` allows arbitrary extras, so misspelled optional fields survive validation
and silently fall back. Independently named generated columns with identical generator config share
the same derivation identity and can be byte-identical.

**Resolve:** use typed built-in generator variants with `extra="forbid"` plus an explicit namespaced
extension/provider-config object; add a stable, immutable `generation_id` separate from display
name, with documented stream-sharing defaults and an explicit seed-protocol migration.

### DE-19 - Advertised consumer surfaces are not tested as installed

The wheel claims inline typing without `py.typed`; README lists a top-level
`PolarsExecutionAdapter` export that does not exist; optional capabilities mostly skip under the
default CI environment; coverage configuration exists without coverage tooling/gate. The README's
canonical `PipelineConfig` example also omits the required `targets` mapping, so its documented
validation step fails before execution. README calls Polars the default while public
`run_pipeline` explicitly defaults to pandas.

**Resolve:** installed-wheel documentation examples and external-consumer typing test; required
extras matrix with zero unexpected skips; measured branch coverage as a blind-spot signal.

### DE-20 - Stable facts drift in documentation and release mechanics

`SECURITY.md` calls the package 0.1.0, README stamps 0.1.0, CODEMAP contains obsolete routing/file
claims, and no release workflow builds/tests/publishes an immutable artifact.

**Resolve:** generate versions, exports, route/capability tables, and path existence from code;
tag-triggered environment-approved Trusted Publishing after artifact verification and provenance.

## Target Crypto Architecture

The remediation should separate reproducibility, pseudonymization, and artifact authenticity:

```text
public reproducibility_seed -----------------> sampling and synthetic generation

host-managed 256-bit mask secret or KeyProvider
        |
        v
inject key bytes + stable key/version ID at execution boundary
        |
        +-- HKDF(info="decoy/hash/v2/<namespace>/<key-version>")
        +-- HKDF(info="decoy/fpe/v2/<namespace>/<key-version>")
        +-- HKDF(info="decoy/vault/v2/<run>/<key-version>")

publisher signing key (offline/CI) ----------> model and release provenance
```

KMS/HSM storage, tenant authorization, rotation orchestration, and worker isolation belong to the
host platform. The engine contract should accept an opaque resolver or injected key material; it
should not require direct network access to a key service.

Recommended method by purpose:

| Purpose | Method | Required caution |
|---|---|---|
| Deterministic token | HMAC-SHA-256 with full output or reviewed truncation budget | Equality/frequency leakage remains; low-domain data is pseudonymized, not anonymized |
| Format preservation | Reviewed FF1 implementation, or token vault | Enforce complete input domain and minimum domain size; no custom fallback |
| Vault encryption | Strong independent key with authenticated encryption and envelope/key rotation | Vault is re-identification data; separate access from masked output |
| Synthetic generation | Public seed when only reproducibility matters; keyed PRF when unpredictability is required | Do not call public-seed output cryptographically unpredictable |
| Model/release authenticity | Publisher signature/attestation with pinned verification identity | Unkeyed hash is not authenticity; pickle must not cross an untrusted boundary |

Deterministic masks intentionally reveal equality and can reveal frequency. No key-management or
cipher change converts that property into anonymization. Product and evidence language should use
`pseudonymization` unless an explicit risk assessment proves otherwise.

## Remediation Program

### Phase 0 - Immediate containment

1. Disable or hard-reject FPE for values not proven inside a complete typed domain; remove
   `preserve_separators=False` from sensitive configs.
2. Require an independent strong mask key or `KeyProvider` for hash, FPE/unmask, and vault; reject
   seed-only secure modes.
3. Default undeclared columns to error; add a temporary explicit passthrough allowlist.
4. Disable caller-selected/unsigned joblib packs wherever selection crosses a trust boundary; make
   deterministic STORM fallback an explicit mode rather than a silent production fallback.
5. Do not publish the dirty-workspace sdist.

### Phase 1 - Security and contract spine

1. Introduce `SourceHandle`, exact planned schema, and source/output completeness postconditions.
2. Build a single orchestrator and run post-mask safety checks before the run-scoped publication
   protocol commits.
3. Replace/migrate FPE; introduce a key-provider boundary and key IDs/versioning/rotation support.
4. Replace pickle model format or implement mandatory provenance verification, with host isolation
   where an untrusted artifact boundary exists.
5. Unify `PoolSpec` and fail-closed capacity semantics.

### Phase 2 - Scale and integrity

1. Carry lazy handles through the public out-of-core route.
2. Replace pandas FK coercion with a shared Arrow-native typing contract.
3. Use representative/provenanced profile sampling.
4. Collapse routing into one initial plan plus audited runtime transitions; make auto-chunk
   sink-aware.
5. Produce public-route cgroup/RSS evidence for narrow, wide, skewed, and high-cardinality jobs.

### Phase 3 - Release and evidence

1. Hermetic wheel/sdist allowlists and clean artifact CI.
2. Engine-owned version/proof provenance.
3. Cap the accepted Python range or test every accepted interpreter from installed artifacts; add
   an extras matrix.
4. Give every benchmark/OOM test a workflow owner.
5. Trusted Publishing, signing/attestation, and installed-artifact docs tests.
6. Flip `RELEASE_PHASE` only after the critical/high gate matrix is green.

## Required Gate Matrix

| Gate | Done condition |
|---|---|
| Crypto | Standard primitive/KATs, strong independent key/provider, key separation, versioning, missing-key fail |
| No raw passthrough | Unexpected columns and out-of-domain values fail before any durable write |
| Model safety | No unauthenticated pickle load; wrong/unsigned pack rejected pre-deserialization |
| Source binding | Profiled handle is executed handle; complete source/output table set |
| Publication | Run-scoped commit marker; same-domain atomicity; heterogeneous-sink idempotency/compensation |
| FK integrity | Exact values/schema across all routes at numeric boundaries |
| Memory | Public lazy/sink route passes hard cgroup cap; telemetry matches residency |
| Packaging | Archive allowlist, clean checkout, wheel/sdist installs, model resource loads |
| Compatibility | Every claimed Python and extra tested from built artifact |
| Evidence | Installed version/key/algorithm IDs are engine-owned and reproducible |

## Strengths Worth Preserving

- Strict Pydantic models and a validate-once choke point are a good base for a closed contract.
- Frozen plans, namespace graphs, route compatibility checks, and typed error codes support
  auditability.
- The compatibility corpus, property/parity tests, source-hygiene sentries, and broad regression
  suite provide strong migration protection.
- Out-of-core scratch roots use restrictive permissions and structured cleanup.
- Vault v2 bounded encryption avoids a second full plaintext serialization.
- Documentation often admits limitations directly; this made the adversarial review faster and
  should remain culturally protected.
- Final checks are green: 6,281 tests passed, ruff check/format passed, and mypy passed over 375
  source files.

## Prior-Finding Reconciliation

- July 9 eager profiling and reject-after-profile findings are materially remediated by SC7a/SC7b.
- End-to-end SC7c memory proof remains absent, and the public lazy OOC boundary is still eager.
- Dangling mypy overrides and GA-stub sentry gaps are closed.
- Compatibility corpus now exists.
- Partial Polars, multiple routing authorities, and large orchestration modules remain.
- Earlier vault whole-plaintext and seed-protocol format issues are improved; the more fundamental
  seed-entropy/key-separation defect remains.

## Verification Record

Frozen revision source checks:

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

Artifact probes were kept separate from that source-gate record:

```text
clean export of c1c4f2c: wheel 1.1 MB / 377 entries; sdist 25 MB / 1,003 entries
later dirty shared tree: wheel 1.1 MB; sdist 49 MB / 3,116 entries
```

Focused probes confirmed:

- FPE partial and complete raw retention plus non-invertible fallback;
- vault and known-plaintext hash recovery for a small example seed;
- undeclared-column passthrough;
- silent success with missing resident source;
- top-level pool size lost before handler execution;
- unsigned joblib execution mechanism (platform privilege boundary not traced);
- installed-wheel default model absence;
- dirty-workspace sdist inclusion of local worktree/cache/review state;
- quarantine persistence after table commit failure;
- eager public OOC loading and false telemetry;
- biased head-profile statistics;
- pandas large-FK rounding.

## Coverage Limits

- No real cloud account, external database, SFTP service, production key service, or production PII
  was accessed.
- The companion platform/CLI repositories were not reviewed end-to-end. Statements about their
  hooks are based on explicit engine comments/contracts, not external runtime observation.
- No multi-million-row or 50M/100M public-route memory benchmark was run. Memory findings use
  object-lifetime analysis, focused probes, and existing route tests.
- Optional extras skipped by the main environment remain a coverage gap, not evidence of failure.
- This report is current for `c1c4f2c`; later changes require re-running the focused bite probes.

## Evidence Directory

- [Evidence index](review-notes/2026-07-12/README.md)
- [Lead security and config note](review-notes/2026-07-12/lead-security-and-config-contracts.md)
- [Execution and data-integrity note](review-notes/2026-07-12/execution-and-integrity.md)
- [Contracts, tests, packaging, and operations note](review-notes/2026-07-12/contracts-tests-operations.md)
- [Independent adversarial check](review-notes/2026-07-12/report-adversarial-check.md)
- [Review plan](plans/2026-07-12-adversarial-architecture-review.md)
- [Prior consultant review](engine-consultant-findings-2026-07-09.md)

## External Source Index

- [NIST SP 800-38G, Format-Preserving Encryption](https://csrc.nist.gov/pubs/sp/800/38/g/upd1/final)
- [NIST SP 800-38G Rev. 1, second public draft](https://csrc.nist.gov/pubs/sp/800/38/g/r1/2pd)
- [NIST SP 800-57 Part 1 Rev. 5, Key Management](https://csrc.nist.gov/pubs/sp/800/57/pt1/r5/final)
- [NIST SP 800-188, De-Identifying Government Datasets](https://csrc.nist.gov/pubs/sp/800/188/final)
- [RFC 5869, HKDF](https://www.rfc-editor.org/rfc/rfc5869.html)
- [OWASP Cryptographic Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html)
- [OWASP Key Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Key_Management_Cheat_Sheet.html)
- [OWASP Input Validation Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html)
- [joblib.load security warning](https://joblib.readthedocs.io/en/stable/generated/joblib.load.html)
- [scikit-learn model persistence](https://scikit-learn.org/stable/model_persistence.html)
- [Hatch sdist builder](https://hatch.pypa.io/1.12/plugins/builder/sdist/)
- [Hatch build selection](https://hatch.pypa.io/1.10/config/build/)
- [PyPA packaging flow](https://packaging.python.org/en/latest/flow/)
- [PEP 561, inline typing marker](https://peps.python.org/pep-0561/)
