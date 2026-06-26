# decoy-engine — Remediation Source Document

**Status:** Source-of-truth findings register. Downstream implementation plans are built FROM this document; it does not itself contain per-step implementation specs.
**Repo:** `/home/cam/vscode/decoy-engine`
**Date compiled:** 2026-06-18
**Provenance:** LLM council review (7 agents) → every actionable claim independently re-verified against the real code by 3 Explore passes. Findings below carry verified `file:line` evidence. Where the council overstated a claim, the correction is recorded inline.
**Rev 2 (2026-06-19):** a second council audit (7 agents) reviewed THIS document against code. It caught one blocking accuracy defect (F5's mechanism was backwards — corrected below), under-flagged severity on F4, a missing F13↔F1 sequencing dependency, and three real blind spots now added (F17 concurrency, profile-seed determinism, cross-version vaulted-unmask). Every Rev-2 correction was re-verified against code before folding in. Net verdict after Rev 2: **fit to drive implementation.**

---

## Context — why this document exists

decoy-engine is the data-plane library powering Decoy masking + synthetic-data generation (~37k LOC, 236 test files). Its **validate → compile → frozen-plan → run** spine and its **MASK-path determinism core** are production-grade and unusually well-documented. The risk is concentrated in three places: the **synthetic-generation path** (weaker determinism + thin tests), a small set of **execution-layer correctness leaks**, and **production-readiness gaps** (no cross-version compatibility corpus; in-heap plaintext vault window). This register catalogs each problem with verified evidence and a recommended fix direction so that focused implementation plans can be carved off it.

### Decisions baked into this document
1. **Altitude:** findings register + fix direction. Each item states evidence, why it matters, blast radius, fix direction, and rough effort/risk. Detailed design is the downstream plan's job.
2. **Backward-compat for output-shifting determinism fixes:** **clean break, single protocol bump.** Treat the engine as pre-GA (consistent with the RELEASE_PHASE / GA-transition gates and the not-yet-frozen compatibility corpus). All output-shifting fixes (P1 items) bundle into **one `SEED_PROTOCOL_VERSION` v6 bump**, and the cross-version compatibility corpus (P0) is frozen *after* those land — never before.

### Sequencing constraint (read before building plans)
- **The corpus freeze comes after EVERY persisted-artifact format change, not just the v6 bump.** That means: the v6 `SEED_PROTOCOL_VERSION` bump (F2–F5) **AND** F13's vault serialization change **AND** any F9 surface change that touches serialization. Freezing the corpus on v5 output then shipping v6 would invalidate it immediately; freezing it before F13 changes the vault format would pin a vault layout the very next cluster rewrites (the original Rev-1 plan ordered the corpus freeze before F13 — that was wrong; see the F13↔F1 dependency below). The safe rule: **land all format-shifting fixes → THEN freeze.** If F13 must land later, explicitly exclude vault artifacts from the v6 freeze and add them after F13.
- Several P1/P3 items share the determinism layer and `generators/columns.py`; group them so the protocol bump and the byte-identity parity tests are written once.
- **F4 is a privacy regression, not a determinism nicety.** It ships inside the v6 *delivery* bundle (one protocol bump) but is severity-decoupled from F2/F3 — it can land first, on its own branch, since it is a one-line fix with zero coupling to `columns.py`. Do not let its urgency be averaged into the larger generate-path rewrite.

---

## Severity tiers (at a glance)

| Tier | Theme | Items |
|------|-------|-------|
| **P0** | Launch-blocking before any real user holds a vault/plan | F1 |
| **P1** | Correctness / determinism (output-shifting → v6 bump) | **F4 (privacy-critical, ship first)**, F2, F3, F5 |
| **P2** | Tests that lock the claims | F6, F7, **F14b (NER-version guard)** |
| **P3** | Performance (determinism-adjacent) | F8 |
| **P4** | Architecture & maintainability | F9, F10, F11, F12, **F17 (concurrency)** |
| **P5** | Privacy posture & ops hardening | **F13 (privacy-window: P3-urgency, see note)**, F14a, F15, F16a/b/c |
| **—** | Council claims downgraded / refuted (do NOT spend effort) | D1–D5 |

> **Tier note:** tiers below are organized by *theme* for readability, but two items carry a severity that outranks their thematic bucket and are called out inline: **F4** (privacy regression, highest-urgency P1) and **F13** (plaintext-PII-in-heap window — P3-urgency despite sitting in the P5 privacy/ops section). Read the per-item "tier" callouts, not just the section header.

---

## P0 — Launch-blocking

### F1. No cross-version compatibility corpus exists
- **Evidence:** `docs/compatibility-contract.md §3.4` freezes the `decoy_engine/__init__.py` + `sdk.py` symbol surface and output-affecting defaults as a within-major-version obligation. Golden snapshots today only pin SHA-256 hashes produced by the *current* code, so they cannot detect a format-reader regression in `plan/_serialize.py`, `vault.py`, or any disguise schema. No prior-version frozen artifacts are loaded by the current engine in CI.
- **Why it matters:** the moment a real user holds a vault file or a frozen plan, any serialization change becomes an unreadable-file incident with no CI signal. The contract is currently aspirational, not enforced.
- **Blast radius:** vault read/write, plan (de)serialization, disguise schema loaders, every persisted artifact.
- **Fix direction:** freeze a corpus of real artifacts (vault files, plan YAMLs, masked CSVs, distribution snapshots) from the first stable version; add a CI job that loads them with the current engine and asserts they still parse + round-trip. **Must be frozen AFTER the P1 v6 bump lands** (see sequencing constraint).
- **Effort/risk:** Medium effort, low risk. Pure additive test infrastructure. Cross-references existing memory: compatibility-contract + methodology-hardening already track this as launch-blocking.

---

## P1 — Correctness / determinism (output-shifting; bundle into one `SEED_PROTOCOL_VERSION` v6 bump)

> **SHIPPED (2026-06-26, branch feat/v6-generation-determinism):** `SEED_PROTOCOL_VERSION` is now `6` (`determinism/_derive.py:117`). F2 and F3 are closed by the `GenDeriveContext` rewrite (`generators/derivation.py`). The MASK path is excellent: `derive()` is HKDF-SHA256 + HMAC-SHA256 over **length-prefixed** input, tested against RFC 5869 vectors and a subprocess byte-identity gate. Generation now matches that bar.
>
> **Cross-version vaulted-unmask hazard (Rev 2, open).** F2/F3 changed generate-path output bytes at v6. A v5 vault over a synthetic column cannot be unmasked under v6: the regenerated seed diverges. The explicit guard that detects the mismatch and returns a clear error rather than wrong values is deferred to F13 (vault-hardening). See `docs/compatibility-contract.md §3.3` for current state. Until F13 lands, cross-version unmask of vaulted synthetic columns is unsupported.

### F2. Generate-path keyed-synthesis seed consumes only 4 bytes of HKDF output *(headline correctness risk)* -- SHIPPED 2026-06-26
- **Evidence (CONFIRMED):** `synthetic_column_seed` (`generators/derivation.py:1-73`) has **four** resolution paths, all funnelling through `_bytes_to_seed`, which returns `int.from_bytes(b[:4], "big")` (`:72-74`) — a uint32. The keyed path that matters (`:60` `return _bytes_to_seed(derive_key(f"gen:{fingerprint}"))`) therefore consumes only the **first 4 bytes** of the HKDF output, discarding 28 bytes of key material. **Scope correction (Rev 2):** frame this precisely — the keyed synthesis path consumes 4 bytes, it is *not* a blanket "post-HKDF truncation accident." Path 4 (`:71-73`, `fallback_seed XOR md5(fingerprint)[:4]`) is a **deliberately non-crypto** reproducibility fallback (`# noqa: S324`) and is out of scope for a crypto-strength critique; the `fresh`/`os.urandom(4)` path (`:33`) is intentionally non-deterministic.
- **Why it matters:** (1) 32-bit keyspace → birthday collisions between columns become likely well before a realistic column count, and a config-holding adversary can brute-force a generated column's seed; (2) it forks the engine into "strong mask determinism / weak generate determinism," undermining the otherwise-airtight reproducibility story.
- **Blast radius:** every keyed `generate`/synthesize column; categorical generation; null injection.
- **Fix direction — decide between two options in the downstream plan (they have different output-shift and risk profiles):**
  - **(A) Full per-row `derive()`** — replace the per-column int seed with `derive(job_seed, gen_namespace, row_index.to_bytes(...))`, consuming full 256-bit material per row. Eliminates the F3 correlation entirely and unifies mask + generate determinism under one primitive. Maximal blast radius, maximal output shift, the cleanest end state.
  - **(B) Widen the seed + bind the column name** — keep the per-column-seed structure but widen `_bytes_to_seed` from `[:4]` to `[:8]`/`[:16]` and mix the column name into the `gen:` fingerprint. Kills both the birthday-collision ceiling and the F3 `+i` cross-column correlation at lower effort/risk, but leaves three RNG families keyed off one (wider) int.
  - Recommended default: **(A)** for the clean determinism story, unless the downstream plan finds the per-row `derive()` cost prohibitive on wide synthetic tables, in which case **(B)**. State the choice + rationale in the plan.
- **Effort/risk:** (A) Medium-high / medium; (B) Low-medium / low-medium. Output-shifting either way → rides the v6 bump. Pairs with F6 tests.

### F3. Adjacent generated columns produce row-shifted-identical streams -- SHIPPED 2026-06-26
- **Evidence (CONFIRMED):** `generators/columns.py:373-375` — `row_seed = column_seed + i; row_rng.seed(row_seed); faker_inst.seed_instance(row_seed)`. Column A (base `S`) and column B (base `S+1`) emit the same seed sequence offset by one row, so `A.row[i]` and `B.row[i-1]` share a seed. Same 32-bit space mixes three RNG families keyed off the one int: `random.Random` (`columns.py:458`, `generation/synthesize.py:320,526`), `numpy.random.default_rng` (`columns.py:266` null injection), and Faker's internal RNG.
- **Why it matters:** for a *synthetic-data* product, correlated cross-column streams leak structure and degrade statistical independence/fidelity — the opposite of the product's promise. The codebase itself documents that numpy and Python `random` diverge for the same int seed (`_derive.py:64-71`), underlining how brittle one shared int is.
- **Blast radius:** statistical quality of all multi-column synthetic output.
- **Fix direction:** subsumed by F2 — per-row `derive()` with a column-name-bound namespace removes the `+i` correlation and the cross-RNG-family seed sharing. Keep F2 and F3 in one plan.
- **Effort/risk:** folds into F2.

### F4. Deterministic shuffle uses an empty-bytes source → identical permutation across a namespace *(PRIVACY-CRITICAL — highest-urgency P1, arguably conditional-P0; ship first, independent branch)*
- **Evidence (CONFIRMED):** `execution/_strategies/_shuffle.py:46` — `seed_int = int.from_bytes(derive(ctx.job_seed, plan.namespace, b"")[:8], "big")`. The source arg is constant `b""`, so every deterministic-shuffle column in the same namespace derives the **same** seed and gets the **identical** permutation.
- **Why it matters:** two shuffle columns in one namespace (e.g. `email`, `phone` under `pii`) are permuted in lockstep, which **re-links values that masking was meant to decouple** — a functional failure of the product's core privacy promise, categorically more severe than the statistical-fidelity issues (F2/F3) it shares the P1 bucket with. Treat as the most urgent correctness item even though the *fix* is trivial.
- **Blast radius:** any job with 2+ deterministic-shuffle columns sharing a namespace.
- **Fix direction:** incorporate the column name into the derivation source: `derive(ctx.job_seed, plan.namespace, column.encode("utf-8"))`. Output-shifting → rides the v6 *delivery* bundle, but **ships on its own branch ahead of the F2/F3 rewrite** (zero coupling to `columns.py`).
- **Effort/risk:** Low effort, low risk; shuffle is irreversible (no unmask round-trip touched). One-line change + a regression test (F6).

### F5. `bool` seed reaches the profiler RNG before compile rejects it *(Rev-2 CORRECTED — mechanism was backwards in Rev 1)*
- **Evidence (CONFIRMED, Rev-2 corrected mechanism):** the actual call order in `execution/_pipeline.py` is: `:140-141` `job_seed = job_seed_raw if isinstance(job_seed_raw, int) else None` — and `isinstance(True, int)` is `True`, so `seed: true` survives as `job_seed = True`; then `:143` `profile = profile_source(config, seed=job_seed)` runs **first**; then `:145` `plan = compile_plan(...)` runs **after** and only there rejects the bool (`plan/_compile.py:591-601`). `profile_source` does `rng = random.Random(seed) if seed is not None else random.Random()` (`profile/_source.py:89`), so `seed: true` **does reach a live RNG** and seeds the reservoir sampler as `random.Random(True)` ≡ `random.Random(1)`. There is also a **second** coercion path: `profile/_source.py:70-72` falls back to `config["global_settings"]["seed"]` via `isinstance(config_seed, int)`, which likewise accepts a bool.
  - **What Rev 1 got wrong:** it claimed "compile_plan runs first and rejects the bool before the profiling path runs." The order is the reverse. The bug is therefore *not* purely latent — `seed: true` and `seed: 1` already produce **identical profiles** today, an observable seed coercion in the profile path. (The final *masked* output is still safe only because `compile_plan` aborts the whole run immediately after profiling — so no masked artifact is emitted — but the coercion is real and the framing of "guarded" was misleading.)
- **Why it matters:** an observable coercion today, plus the underlying disease — **two independent seed-normalization code paths** (`_pipeline.py:141` and `_compile.py`) that must stay in sync, with no shared helper enforcing it. The next refactor that reorders things or adds an entry point turns this into a live masked-output bug.
- **Blast radius:** profile determinism (already affected); seed normalization correctness; defense-in-depth.
- **Fix direction (re-scoped):** route the seed extraction at `_pipeline.py:141` itself through the canonical `_normalize_job_seed` (from `plan/_compile.py`, already imported e.g. at `vault.py:214`) — **not** just `compile_plan` — so the bool is rejected *before* `profile_source` is called. Also tighten `profile/_source.py:70-72`'s `isinstance(config_seed, int)` fallback to reject bool. A single shared normalizer used by all three sites is the durable fix.
- **Effort/risk:** Low effort, low risk. Now demonstrably output-shifting for the profile path (so it genuinely belongs in the v6 bundle), and folds a profile-seed determinism test into F7.

---

## P2 — Tests that lock the claims

### F6. Generate-path and shuffle invariants are untested relative to their risk
- **Evidence:** only a handful of test files touch `synthesize`/`generate_tables`. There is **no** test asserting cross-column seed independence (the exact `column_seed + i` correlation a property test should catch), **no** test of the 32-bit collision surface, and **no** subprocess byte-identity gate for `generate_tables` mirroring `tests/unit/determinism/test_process_stability.py`. Shuffle has tests that a permutation results, but none proving two columns in one namespace differ.
- **Why it matters:** F2/F3/F4 fixes have no ratchet protecting them from regression; the generate path is the least-tested area weighted by risk.
- **Fix direction:** add (a) a `generate_tables` subprocess byte-identity gate; (b) a Hypothesis property test for cross-column seed independence; (c) a shuffle same-namespace divergence test; (d) **(Rev 2) a FK-pool / cross-table byte-stability test** — generated parent tables become the FK pool for child mask tables (`_pipeline.py` generate-then-mask flow), so the F2/F3 fix must hold generated PK values byte-stable across the parent→child boundary, not only per-column. Cross-column independence alone will not catch a broken referential join. Land alongside F2–F4 so the fixes ship green.
- **Effort/risk:** Medium effort, low risk. Pure tests.

### F7. Core reproducibility claims have no direct test
- **Evidence:** no test calls `compile_plan` twice on identical inputs and asserts `plan1 == plan2` / byte-identical serialized YAML (the literal byte-stability claim). No end-to-end FPE mask→unmask round-trip equality test. No `DeriveContext`-vs-scalar-`derive()` parity test (prerequisite for F8). Vault ambiguous-mapping accounting (`vault.py:171-181`) is lightly covered.
- **Fix direction:** add cheap parametrized tests: compile-twice byte-stability; FPE mask→unmask→equality; vault ambiguous-mapping count; a **profile-seed determinism test** (same seed → same `StormProfile`, covering the F5 path at `profile/_source.py:89`); and the `DeriveContext` parity test that F8 depends on. **(Rev 2)** the `DeriveContext` parity test must compare `DeriveContext.derive_source` against scalar `derive()` **for the same namespace, byte-for-byte** — a namespace mismatch inside the context would silently diverge tokens and break joinability/unmask while an in-process equality check still passes; prefer a subprocess/cross-instance gate.
- **Effort/risk:** Low effort, low risk.

---

## P3 — Performance (determinism-adjacent)

### F8. `DeriveContext` is implemented but unused on the hot path
- **Evidence (CONFIRMED):** `determinism/_derive.py:121-180` defines `DeriveContext.for_column` to amortize the per-`(seed, namespace)` HKDF extract across rows (docstring cites ~0.5s/column saved on 1M rows). Hash and date-shift handlers still call scalar `derive()` in a per-row Python loop: `execution/_strategies/_hash.py:51-55` and `execution/_strategies/_date_shift.py:59-64`. Grep confirms no strategy handler instantiates `DeriveContext`.
- **Why it matters:** documented, recoverable ~2× HMAC waste per row on the two highest-volume substitution strategies.
- **Fix direction:** adopt `DeriveContext.for_column` in the hash and date-shift loops. **Must ship behind the byte-identical parity test from F7** before the scalar loop is removed — joinability/unmask depend on byte-stable output.
- **Effort/risk:** Low-medium effort, medium risk (touches determinism output). Strictly gated on F7's parity test.

---

## P4 — Architecture & maintainability

### F9. Public surface is oversized under a frozen compatibility contract
- **Evidence (CONFIRMED):** `__init__.py` `__all__` exports **149 symbols**, including implementation-level types: `BundlePool`, `PoolCache`, `CompositeAddress`, `composite_city_state_zip`, and ~15 identifier adapter/domain/validator symbols (5 families — Ein/Mrn/Ndc/Npi/Ssn — × Adapter/Domain/Validator). `docs/compatibility-contract.md §3.4` freezes this surface within a major version. Two parallel provider systems are both exported: `providers` (v1: `register_faker_provider`, `atomic_swap_db_providers`) and `providers_v2` (`ProviderRegistry`, `register_faker_provider_v2`) — README points at v2 but callers get no signal.
- **Why it matters:** every symbol becomes a frozen obligation at GA. A wide `__all__` is cheap to add, expensive to remove. The dual provider export is an ambiguity trap.
- **Fix direction:** before GA freezes the surface, trim `__all__` to symbols callers actually construct; move identifier validators + composite/pool internals to a dedicated sub-import (e.g. `decoy_engine.identifiers`) with its own versioning; pick one provider system as public and demote/deprecate the other.
- **Effort/risk:** Low-medium effort, low risk (additive re-org) IF done pre-GA; high cost if deferred past freeze.

### F10. Redundant relationship-graph build
- **Evidence (CONFIRMED):** `plan/_compile.py:131-170` builds the namespace registry + relationship graph during compile. `execution/_pipeline.py:126-156` calls `compile_plan` (which builds the graph), then **rebuilds both** from the raw config/profile (`build_namespace_registry` + `build_relationship_graph`), discarding the plan's graph.
- **Why it matters:** doubles graph-build cost on FK-heavy schemas and creates a latent config/plan-mismatch hazard — the adapter trusts a graph reconstructed from config, not the plan it was handed.
- **Fix direction:** have `compile_plan` expose the built registry/graph on the `Plan` (or via its return), and have `run_pipeline` consume that instead of rebuilding from config. Single source of truth.
- **Effort/risk:** Medium effort, medium risk (touches the compile/run boundary and plan shape).

### F11. Module size hotspots over the ~600-LOC cap
- **Evidence (CONFIRMED, exact):** `CLAUDE.md:19` sets a ~600-LOC orchestration cap; `tests/sentry/test_module_size.py` ratchets it with an allowlist. Current offenders: `storm/detectors.py` 1356, `generators/columns.py` 1349, `storm/profiler.py` 999, `plan/_compile.py` 921, `quality/synth_report.py` 863. Within `_compile.py`, `_build_seed_envelope` (~260 lines) interleaves per-column stamping, NER version stamping, composite-child detection and `DeriveContext` construction — untestable below full-pipeline level.
- **Why it matters:** `columns.py` is both the largest module and the locus of the F2/F3 determinism weakness; size compounds the risk of the fixes there.
- **Fix direction:** decompose `_build_seed_envelope`; split `columns.py` (separate the generate strategies from the orchestration) and `detectors.py` toward the cap. Add decomposition deadlines to the sentry allowlist rather than letting it ratchet upward silently.
- **Effort/risk:** Medium-high effort, medium risk (broad mechanical refactor). Best done AFTER F2/F3 land so the determinism fix isn't rebased across a split.

### F12. Circular-import debt (function-body deferred imports)
- **Evidence (CONFIRMED):** `plan/_compile.py:110-135` defers ~6 imports inside the function body to break a cycle (`__init__` eagerly loads `relationships` → `plan._errors` → back into compile). Same pattern at `vault.py:207-216` and `generators/columns.py:132-138`.
- **Why it matters:** you can't read `compile_plan`'s dependencies from its signature; the `plan` / `relationships` / `generation` dependency DAG isn't strictly layered.
- **Fix direction:** where the import is type-only, move to module-level `TYPE_CHECKING`. Where it's runtime, restructure the layer boundary so the cycle disappears (likely extract the shared check-registry interface). Lower priority than F9–F11.
- **Effort/risk:** Medium effort, low-medium risk.

### F17. Generation serializes across threads through a module-global Faker lock *(Rev 2: new finding; P4, or P2 if parallel generation is a shipped path)*
- **Evidence (CONFIRMED):** Faker's `seed_instance()` mutates **module-level `random` state** internally (`generation/synthesize.py:54-58` comment; `generators/columns.py:366-370` "QA-7 F1 added the cross-thread lock"). To keep determinism under concurrency, `synthesize.py:52,69` define `_DEFAULT_FAKER_LOCK` and `_FAKER_CALL_LOCK`, and the seed_instance + provider_func pair is wrapped in `with _FAKER_CALL_LOCK:` (`:374-379`). This **serializes all `generate_tables` Faker calls across threads in a single process.**
- **Why it matters:** two coupled facts. (1) **Correctness boundary:** generation's determinism depends on module-global RNG state, so any code path that touches Faker outside the lock, or a future async path, silently corrupts cross-thread reproducibility. (2) **Throughput cliff:** concurrent generate jobs in one process collapse to serial through the global lock — a scaling surprise for a data-plane library a platform will call concurrently. The prior council (Rev 1) missed this entirely.
- **Blast radius:** any multi-threaded / multi-job-per-process generation; the platform's concurrency model.
- **Fix direction:** document the single-process serialization contract explicitly in the SDK/PRODUCT_CAPABILITIES; evaluate a per-thread or per-instance Faker (locale-bound instances without shared module state) to remove the global lock, or confirm + document process-level parallelism as the supported scaling path. At minimum, an invariant test that generation under N threads is byte-identical to serial.
- **Effort/risk:** Investigation-first (spike), then medium. **Promote to P2 if concurrent in-process generation is a shipped/required path** — confirm with the platform's execution model.

---

## P5 — Privacy posture & ops hardening

### F13. Vault holds all entries as plaintext in heap, then plaintext Parquet, before encrypting *(P5 section, but P3-urgency — privacy-window)*
- **Evidence (CONFIRMED):** `vault.py:154` accumulates `(namespace, masked, source)` triples in a Python `set`; `add` (`:157-158`) does `self._entries.update(...)`; `write` serializes to Parquet bytes in heap (`:197-199` `pa.BufferOutputStream()` → `pq.write_table` → `buf.getvalue().to_pybytes()`) and only then `fernet.encrypt(...)`. No streaming/chunked path exists. (Vault key derivation `derive(seed, "vault", b"vault-key/v1")` at `:85` and ambiguous-mapping drop+count at `:171-181` are correct.)
- **Why it matters:** the primary risk is a **plaintext-PII window** — unencrypted source values sit in the heap (and potentially swap / a core dump) before `fernet.encrypt`. For a token-vault product whose core claim is source protection, that is a privacy-posture issue, not mere ops hardening — hence **P3-urgency despite the P5 placement.** **(Rev 2 — memory claim qualified):** the secondary "memory wall (~GBs)" risk applies **only to near-unique / high-cardinality vaulted columns.** The vault dedups to *distinct* triples — `vault.py:147-149` states "memory footprint is bounded by the number of DISTINCT triples, not by row count" — so the common low-cardinality case collapses the set and the memory risk largely disappears. Do not justify this finding on memory alone.
- **Fix direction:** streaming sort-and-chunk encryption at the `add`/`write` boundary (the interface already supports incremental adds). Encrypt per chunk; never hold the full plaintext table.
- **Effort/risk:** **Medium-HIGH risk (Rev 2 raised from medium):** crypto + serialization-format change + the F1 corpus interaction + the unmask round-trip invariant all in one. Gate behind the corpus and unmask-round-trip tests.
- **⚠ Sequencing (F13 ↔ F1, blocking):** this changes the **vault serialization format**. The cross-version compatibility corpus (F1) must be frozen **after** F13 lands, or vault artifacts must be explicitly excluded from the v6 freeze and added once F13 ships. Freezing vault artifacts before F13 pins a layout the very next cluster rewrites. **Therefore: land F13 (cluster 3) BEFORE the corpus freeze (cluster 2), or split the corpus freeze into "v6-output artifacts now / vault artifacts after F13."** See the revised decomposition below.

### F14. Two independent guards — split into F14a (vault-crypto compile check) and F14b (NER-version runtime guard)
- **Evidence:** the vault imports `cryptography` lazily (function-local). NER/`text_redact` plans stamp `ner_model_version` and warn when `None` (`plan/_types.py:109-114`), and `docs/what-we-cannot-prove.md` discloses spaCy-version instability — but there is no runtime check that the stamped model version matches the installed model at execution time.
- **F14a — vault-crypto compile check (P5):** a `vault: true` job with `cryptography` absent fails only after a potentially multi-hour run instead of at plan time. **Fix:** fail at compile/plan time if `vault: true` and `cryptography` is unavailable. Low effort, low risk.
- **F14b — NER-version runtime guard (Rev 2: promoted toward P2):** a silent spaCy model update produces different redactions for the same config+seed with **no error** — a silent determinism break on the highest false-negative strategy. This is a *more real* determinism hazard than the (now-corrected) F5, so it belongs with the test/determinism cluster, not buried in ops hardening. **Fix:** runtime assertion that the installed spaCy model version equals the stamped `ner_model_version` (**error, not warn**, on mismatch). Low effort, low risk.

### F15. FPE bare `RuntimeError` escapes the `DecoyError` hierarchy; key/tweak model documented in two places
- **Evidence (CONFIRMED):** `transforms/fpe.py:405-409` raises a bare `RuntimeError` on key-derivation failure (correctly refusing to degrade to seed-only encryption), but it is NOT a `DecoyError`/`ExecutionError` subclass, so an upstream `except DecoyError` (e.g. FK + composite handlers) won't catch it. The key model — one key per `(seed, namespace)` via `derive(job_seed, namespace, FPE_KEY_LABEL)` (`execution/_strategies/_fpe.py:79`) with the **column name as per-value tweak** (`transforms/fpe.py:318`) — is narrated in `fpe.py:20-26` but the v5 note in `_derive.py:82-89` omits the column-name tweak. FPE is honestly documented as **not** NIST FF1 (`fpe.py:20-26`). **Correction to council:** the broader "missing `from exc` chaining" claim is largely refuted — `_fpe.py:61-65` is a fresh raise with no cause, and the v2 faker adapter chains correctly (`providers_v2/_faker_adapter.py:146 ... from exc`).
- **Why it matters:** (a) the bare `RuntimeError` breaks uniform error handling at the worst moment (key-infra failure mid-job); (b) anyone reasoning about unmask reversibility from the `_derive.py` docstring alone will be surprised that renaming a column changes its FPE output.
- **Fix direction:** promote the `RuntimeError` to a typed `ExecutionError`/`DecoyError` with a `code`. Reconcile the FPE key/tweak narrative into one authoritative place and note the column-name-tweak → unmask-across-rename implication.
- **Effort/risk:** Low effort, low risk.

### F16. Sandboxes are denylist-shaped where an allowlist/OS boundary is more durable *(Rev 2: split into three independently-shippable items)*
- **Evidence (CONFIRMED):** `when:` predicate uses `pdf.eval(engine="numexpr", local_dict={}, global_dict={})` with a real security test (`tests/security/test_when_eval_scope.py` — scope-walk, dunder, import all covered) — this one is solid. But `expressions.py:82-102` still exposes a legacy module-global `MASK_GLOBALS` marked "dangerous path" (shared `random` state) reachable alongside the isolated `make_mask_globals(rng)`. `data_discovery.py:54-67` guards DuckDB with a denylist regex (`_BANNED` blocks `read_parquet`/`read_csv`/DDL); a denylist over a dialect the engine doesn't control (`glob()`, macros, lambdas) is structurally fragile. The pandas silent fallback to the Python engine on extension dtypes (`execution/_transforms.py:71-80`) is logged but is a posture degradation when it fires.
- **Why split:** these three have wildly different effort/risk and must not stall a single plan on the DuckDB piece.
  - **F16a — guard/remove legacy `MASK_GLOBALS`** so it can't be reached directly (the isolated `make_mask_globals(rng)` is the supported path). *Trivial.*
  - **F16b — surface the pandas-engine fallback as a warning in `ExecutionResult`**, not just logs, when it fires on extension dtypes. *Trivial.*
  - **F16c — replace the DuckDB denylist with an OS/subprocess sandbox.** A research spike with real latency/IPC tradeoffs. Recommend an **intermediate step first**: a read-only, function-restricted DuckDB instance (disable file-reading table functions at the connection level) before committing to full subprocess isolation. *Medium-high effort; do not bundle with F16a/b.*
- **Effort/risk:** F16a/F16b low / low; F16c medium-high / medium.

---

## Council claims downgraded or refuted — do NOT spend remediation effort

These appeared in the review but verification showed they are non-issues or overstated. Recorded so downstream plans don't waste cycles re-litigating them.

- **D1. `deterministic_hash` is a reachable public footgun — REFUTED.** It lives in `internal/crypto.py:18-40`, is **absent from `__all__`**, is called by no strategy, and already emits a `DeprecationWarning`. The `internal/` boundary is import-linter-enforced. Optional tiny cleanup (delete it), not a remediation item.
- **D2. `bool`-seed bypass on the MASKED-OUTPUT path is an active exploit — DOWNGRADED (Rev-2 re-scoped).** No masked artifact is emitted for `seed: true`, because `compile_plan` aborts the run immediately after profiling. **But** — correcting Rev 1 — the bool is **not** "guarded by ordering": it reaches the profiler RNG *before* compile aborts and already coerces `seed: true` ≡ `seed: 1` in profile sampling. So the *masked-output* exploit is downgraded; the *profile-path coercion* is a live (low-severity) bug tracked as F5, not a non-issue.
- **D3. Profiler leaks source values — DOWNGRADED to disclosure.** `storm/profiler.py:219` retains up to 5 literal source values, but the profiler is a **pre-mask** diagnostic; post-mask `sampled_values` reads masked output (`validation/post/_checks/_sampled_values.py:25-35`) and has a privacy test. Action is a one-line addition to `what-we-cannot-prove.md` (don't log/serialize `StormProfile.top_values`), not a code fix.
- **D4. Widespread missing `from exc` chaining — REFUTED.** Spot sites the council cited either don't apply (fresh raises with no cause) or already chain correctly. Only the F15 typed-error promotion stands.
- **D5. Stale docstring (Rev 2, doc-correctness only) — CLOSED.** Was: `determinism/_derive.py:204` said "stable derived material under `SEED_PROTOCOL_VERSION=4`" while the constant was `5`. The v6 rewrite (2026-06-26) added the v6 history block in `_derive.py` (lines 102-117) and bumped the constant to `6`; the docstring in question was updated as part of that commit. No remaining stale version references in that file.

---

## Verification / how to validate the eventual fixes end-to-end

Downstream plans should each carry their own tests; at the program level, the source-document fixes are "done" when:

1. **Determinism (F2–F5, F8):** `pytest tests/unit/determinism tests/property tests/parity` green; the NEW `generate_tables` subprocess byte-identity gate (F6) green; `SEED_PROTOCOL_VERSION == 6` with a dated rationale block in `_derive.py`; a compile-twice byte-stability test (F7) green.
2. **Compatibility corpus (F1):** new CI job loads frozen v6 artifacts (vault, plan YAML, masked CSV, distribution snapshot) and round-trips them; job is red if any reader regresses. Frozen only after F2–F5 land.
3. **Vault (F13):** memory-bounded run over a synthetic high-cardinality column completes without materializing the full plaintext table; round-trip decrypt matches; ambiguous-mapping count test (F7) green.
4. **Error/ops (F14, F15):** `vault: true` without `cryptography` fails at plan time (new unit test); spaCy version mismatch raises (new unit test); FPE key-derivation failure is catchable via `except DecoyError`.
5. **Full suite + sentry:** `pytest` green including `tests/sentry/test_module_size.py`; after F11, the allowlist ceilings drop rather than rise.
6. **Quality gate:** run the existing dennis review + barry docs pre-push gate on each branch (per methodology-hardening).

> Note: the test suite was read, not executed, during compilation of this document. The first downstream plan should run `pytest` to establish a green baseline before changing determinism output.

---

## Suggested plan decomposition (downstream)

One plan per cluster keeps the protocol bump and parity tests coherent. **Rev-2 re-sequenced** so the corpus is frozen only after every format change (the F13↔F1 dependency), F4 ships first, and the over-bundled items are split.

0. **F4 privacy hotfix** — ship the one-line shuffle-source fix on its own branch immediately (still tagged for the v6 delivery). Highest urgency, zero coupling. Optionally pair with F14b (NER guard) and F16a/F16b (trivial).
1. **Determinism v6 cluster** -- SHIPPED (2026-06-26, branch feat/v6-generation-determinism). F2 + F3 closed by `GenDeriveContext` rewrite; F5 (shared seed validator) shipped in prior batch (remediation batch 1); F6 subprocess gate and cross-column independence tests added. `SEED_PROTOCOL_VERSION` is now `6`. Cross-version vaulted-unmask guard deferred to F13 (see cluster 2). **F10** pull-forward remains open.
2. **Vault hardening** — F13 + F14a. **Must land before the corpus freeze** (changes vault format). Medium-high risk; gate on unmask round-trip tests.
3. **Compatibility corpus** — F1, frozen **after** clusters 1 AND 2 (all format-shifting changes), or split: freeze v6-output artifacts after cluster 1 and vault artifacts after cluster 2.
4. **Error/contract polish** — F15 (typed FPE error + key/tweak doc reconciliation). D5 docstring fix is closed (see D5 entry above).
5. **Performance** — F8, gated on cluster 1's `DeriveContext` same-namespace parity test.
6. **Architecture** — F9 + F11 + F12 (F10 already pulled forward), scheduled after the determinism work to avoid rebase churn in `columns.py` / `_compile.py`. Consider running F11's `columns.py` split *concurrently* with cluster 1 to shrink the regression surface.
7. **Concurrency** — F17, investigation-first; confirm the platform's in-process parallelism needs before committing.
8. **Sandbox hardening** — F16c (DuckDB OS-sandbox spike; F16a/F16b already shipped in cluster 0).

---

## Implementation decisions log (2026-06-26)

- **F4 — DONE** (branch `fix/f4-shuffle-source-privacy`, uncommitted at time of writing). The register called it a one-line fix; it was actually **two** — `execution/_strategies/_shuffle.py:46` AND its polars twin `execution/polars/_strategies/_shuffle.py:50`. Every execution strategy has a pandas + polars copy; `tests/parity/` is the safety net. Same-namespace divergence ratchet added; unit + parity + snapshot green (2200 unit).
- **F2/F3 approach = anchor-neutral (was "Option A" full per-row derive).** A platform check (decoy-platform Council "Option 1") established that re-keying generation onto `job_seed` is the *target* model but is owned by **platform S5's atomic 5-site migration**, not this engine cluster (S4 is deliberately COEXIST; jumping ahead breaks S4→S5 and risks orphaning vaults). So the engine fix keeps the existing master+pipeline-label `derive_key` closure but consumes **full per-row 32 bytes** (`derive_key("gen:{fingerprint}:row:{i}")`) and seeds the three RNG families (random.Random / numpy / Faker) from disjoint sub-derivations — killing F2 (4-byte truncation), F3 (`+i` correlation) and the shared-int family coupling **without changing the determinism anchor**. F8 amortization on the *generate* path is deferred to S5 (where job_seed+namespace make `DeriveContext` natural); F8 on the *mask* path is unaffected.

## Discussion points for a future council (not yet findings)

- **DP1 — same-config generated columns collapse to identical output.** The generation namespace is keyed on `strategy_config_fingerprint`, which deliberately excludes the column display name (R3.10 rename-stability contract, `generators/derivation.py:47-69`). Side effect: two generated columns with identical config — e.g. `first_name` and `middle_name` both `faker: name` — produce **byte-identical columns**. This is the existing designed contract, but it is arguably a synthetic-data-quality defect (real people's first and middle names are not equal). Tension: fixing it (re-introduce a column distinguisher into the namespace) versus preserving rename-stability + "two same-config columns reproduce identically." Surface to council before deciding; do NOT change as part of the F2/F3 anchor-neutral fix.
