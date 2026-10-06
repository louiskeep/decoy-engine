Status: plan (revision 1, author = Opus). Awaiting Codex plan gate.
Rules consulted: 00-universal, development-loop, refactoring, architecture, testing, code-review, scope-discipline, api-and-compatibility

# R1: one descriptor per masking operator

Roadmap: decoy-platform `docs/ROADMAP.md`, Refactor track, R1 "before C5b-ii". Source: `decoy-platform docs/records/2026-10-06-codebase-health-and-refactor-report.md` §2.1 (E1, E3, E4, E7).

Branch: `feat/r1-operator-registry` off engine main `01c560da`. It is rebased after R0 merges; conflicts are expected only in `tests/sentry/test_module_size.py` and the CHANGELOG.

**Behavior-preserving refactor.** No routing decision, reason code, output, evidence key or error changes. Every derived table must equal its current literal value, proven by a snapshot test.

## 1. Goal and scope

Every Phase C slice re-edited 8-14 source files, because the facts about each operator are hand-written in many places. The R1 inventory (2026-10-06, engine `01c560da`):
- 9 operator ids written out 4 times
- 9 strategy names written out 3 times
- the backend, kernel and evidence facts restated per strategy on the chunked side and per operator on the unified side

R1 puts each operator's shared facts in ONE frozen descriptor and derives every table from it. Adding an operator then means one descriptor entry plus its kernel call.

In scope:
- **E1.** An `OperatorSpec` registry. The existing tables become one-line derivations over it, keeping their names so consumers do not change.
- **E3.** Delete the unused `phase3_c1_eligibility` predicate; its one production fact, `C1_PROVIDER_ALLOWLIST`, moves into the Faker descriptor.
- **E4 (narrow).** Collapse the three identical string-only chunked source-type gates (categorical, bucket_perturb, date_shift) into one function. Reason codes stay byte-identical.
- **E7 (narrow).** The forced-oracle test stand-in (numeric-categories categorical, `tests/native/_chunked_entry_support.py`):
  - add a guard test that fails loudly, with migration instructions, if the stand-in ever becomes native-admitted;
  - fix the three stale support-module docstrings naming earlier stand-ins.

Out of scope:
- **E2 (merge the two per-operator dispatch chains)** moves to a separate slice, **R1b**. The inventory showed the chains differ on purpose per route: evidence records, the date_shift error channel (chunk-local vs rebased), missing-companion handling (coded downgrade vs `ShadowDifference`), where the group_key sibling comes from, the positional categorical variant, and degenerate-output handling. Merging them is a behavior-sensitive change, not a table cleanup. R1b is planned after R1 lands, on top of the registry.
- Route-specific facts stay route-specific and are NOT unified (section 3d).
- `CHUNKED_ROUTE_VETOED_STRATEGIES` (now empty, with test monkeypatches) is unchanged.
- The open question of whether categorical belongs in `_POSITIVE_KERNEL_EVIDENCE_OPERATOR_IDS` (the physical route always marks it compiled) is recorded as a follow-up. R1 keeps `{hash, faker}` exactly.

## 2. Established facts (R1 inventory)

1. Tables keyed by operator id:
   - `_unified_slice_admission.py:96-176`: `ALLOWED_OPERATOR_IDS`, `BACKEND_BY_OPERATOR_ID`, six `*_OPERATOR_ID` constants, `_COMPANION_DEPENDENT_OPERATOR_IDS`, `_OPERATOR_REQUIRED_KERNEL`, `_ROUTED_DIAGNOSTIC_OBLIGATIONS`
   - `_unified_slice_evidence.py:42`: `_POSITIVE_KERNEL_EVIDENCE_OPERATOR_IDS`
   - `physical/_shadow_operators.py:55-63`: nine op-id literals
2. Tables keyed by strategy:
   - `physical/_shadow_bindings.py:44,58`: `SLICE_STRATEGIES`, `OPERATOR_ID_BY_STRATEGY`
   - `_unified_slice_resident_types.py:21`: `_ADMITTED_RESIDENT_TYPES`
   - `native/_requirements.py:126,166`: `NATIVE_KERNEL_STRATEGIES`, `NATIVE_POOL_STRATEGIES`
   - `native/_dispatch.py:87`: `_INDEX_KERNEL_STRATEGIES`, plus the hash→crypto and group_key→raw_hex loads at `:470,486`
   - `native/_chunked_evidence.py:40-64`: `_COMPANION_STRATEGIES`, `_planned_backend`
   - `native/_chunked_schema_rule.py:49`: `_STRING_OUTPUT_STRATEGIES`
   - `physical/_shadow_assembly.py:23,26`: `_TOKENIZING_STRATEGIES`, `_NULL_ON_EMPTY_STRATEGIES`
   - `native/_operator_config_rejections.py:195`: `_NATIVE_GROUP_KEY_SIBLING_TYPES`
   - `native/_phase3_eligibility.py:59`: `C1_PROVIDER_ALLOWLIST`
3. Proved restatements (member lists in the inventory):
   - ALLOWED = values(OPERATOR_ID_BY_STRATEGY) = keys(BACKEND) = the `_shadow_operators` literals
   - SLICE_STRATEGIES = keys(_ADMITTED_RESIDENT_TYPES) = NATIVE_KERNEL ∪ NATIVE_POOL
   - COMPANION_DEPENDENT = keys(_OPERATOR_REQUIRED_KERNEL) = {backend ≠ arrow_python}
   - `_COMPANION_STRATEGIES` = {backend = rust_companion}
   - `_INDEX_KERNEL_STRATEGIES` = {kernel = index}
   - `_NATIVE_GROUP_KEY_SIBLING_TYPES` = `_ADMITTED_RESIDENT_TYPES["passthrough"]`
   - `_ROUTED_DIAGNOSTIC_OBLIGATIONS` = the capability-derived reducers for the slice operators (only date_shift has row-error modes)
4. `phase3_c1_eligibility` has no production caller. Production imports only `C1_PROVIDER_ALLOWLIST` (`_shadow_bindings.py:30`, `_real_type_admission.py:46`). The platform imports `native_route_eligibility` (`decoy-platform api/jobs/_phase1_eligibility.py:217`), which is kept.
5. **Import constraints.**
   - `_unified_slice_admission.py` restates op ids specifically so cheap admission never imports `execution.physical`.
   - `_pipeline.py:100` pulls the admission closure in at import time.
   - The backend constants live in `_chunked_evidence`, which imports `_plan`/`_requirements`, so a registry importing them would create a cycle.
   - The seam sentry (`tests/sentry/test_physical_seam_disconnection.py`) requires new non-physical execution files on a permitted list. Its fresh-import probe must still pass.
6. **Established pattern to follow:** `native/_capabilities.py` (`_CAPS` of frozen `StrategyCapabilities`, `capabilities_for` raises `KeyError` on an unclassified strategy, `classified_strategies()`, and a totality test in `tests/native/test_capabilities.py`). The draw-site catalogue (`DRAW_SITES`, `test_draw_site_inventory_coverage.py`) is the same shape.

## 3. Decisions

**3a. The registry module.** A new leaf module `src/decoy_engine/execution/_operator_registry.py`.
- It imports only the stdlib and `pyarrow` (enforced by a sentry, 5.3).
- It OWNS the backend vocabulary (`RUST_COMPANION`, `RUST_POOL_SELECT`, `ARROW_PYTHON`, `PANDAS_ORACLE`). `native/_chunked_evidence.py` re-imports those names from it, so existing imports keep working and the cycle disappears.
- It exposes `OPERATORS: Mapping[str, OperatorSpec]` keyed by strategy, plus `operator_spec(strategy)`, which raises `KeyError` like `capabilities_for`.

**3b. `OperatorSpec` fields.** Only facts the current tables actually encode.

| Field | Values | Encodes |
|---|---|---|
| `strategy` | str | the key |
| `operator_id` | str | `OPERATOR_ID_BY_STRATEGY`, `ALLOWED_OPERATOR_IDS`, the `*_OPERATOR_ID` constants and `_shadow_operators` literals |
| `shape` | `"kernel"` / `"pool"` | `NATIVE_KERNEL_STRATEGIES` vs `NATIVE_POOL_STRATEGIES` |
| `planned_backend` | one of the 3 native backends | `BACKEND_BY_OPERATOR_ID`, `_COMPANION_STRATEGIES`, `_planned_backend` |
| `required_kernel` | `None` / `"crypto"` / `"index"` / `"raw_hex"` | `_OPERATOR_REQUIRED_KERNEL`, `_COMPANION_DEPENDENT_OPERATOR_IDS`, `_INDEX_KERNEL_STRATEGIES` |
| `positive_kernel_evidence` | bool | `_POSITIVE_KERNEL_EVIDENCE_OPERATOR_IDS` (true for hash and faker only, unchanged) |
| `unified_resident_types` | frozenset[pa.DataType] | `_ADMITTED_RESIDENT_TYPES` (unified slice only; the chunked gates keep their own domains) |
| `full_frame_assembly` | `"tokenizing"` / `"null_on_empty"` / `"type_preserving"` | `_TOKENIZING_STRATEGIES`, `_NULL_ON_EMPTY_STRATEGIES` |
| `provider_allowlist` | frozenset[str] or None | `C1_PROVIDER_ALLOWLIST` (faker only) |

Not fields, and why:
- `_ROUTED_DIAGNOSTIC_OBLIGATIONS` stays derived from capabilities (`_diagnostic_reducers`), the authority for diagnostics; a second copy in the registry would restate it. The admission module computes it from `capabilities_for` restricted to registry operators.
- `_STRING_OUTPUT_STRATEGIES` and the conditional pins stay in `native/_chunked_schema_rule.py`. They are a chunked-route output contract with config conditions, not a per-operator constant (3d).
- `_NATIVE_GROUP_KEY_SIBLING_TYPES` becomes a derivation from `OPERATORS["passthrough"].unified_resident_types` (its own comment says it is that set), but it stays named where it is.

**3c. Derived views keep their names.** Each existing table is replaced IN PLACE by a comprehension over `OPERATORS`, with the same name and type (frozenset / dict / Final). For example:

```python
ALLOWED_OPERATOR_IDS = frozenset(s.operator_id for s in OPERATORS.values())
_OPERATOR_REQUIRED_KERNEL = {s.operator_id: s.required_kernel for s in OPERATORS.values() if s.required_kernel}
```

Each derived view gets a one-line "derived from the operator registry; edit the registry" comment. Consumers stay untouched, which keeps the diff small and the existing tests meaningful. The six `*_OPERATOR_ID` constants become `OPERATORS["hash"].operator_id` and so on. `_shadow_operators.py`'s literals use the registry.

**3d. Route-specific facts that stay where they are** (inventory "do not unify"):
- the chunked source-type domains (`_real_type_admission`), which differ from the unified domains for passthrough, redact, truncate, hash and Faker large_string
- degenerate-output handling (chunked null cast and string pins vs `assemble_column`)
- the two evidence records
- missing-companion handling
- date_shift's error channel
- the positional categorical variant
- group_key's sibling source and its order-dependence check
- the null-bearing-int guard
- coded reasons (chunked) vs `None` declines (unified)

**3e. Delete `phase3_c1_eligibility` (E3).**
- Move `C1_PROVIDER_ALLOWLIST` to `OPERATORS["faker"].provider_allowlist`, and keep a module-level `C1_PROVIDER_ALLOWLIST = OPERATORS["faker"].provider_allowlist` re-export where the two production importers read it (or update both importers; the builder picks one and records it).
- Delete `native/_phase3_eligibility.py`'s predicate, `Phase3Eligibility`, `_faker_column_rejection` and `_is_reclassified_faker_kernel_rejection`. Delete the module entirely if nothing remains.
- Delete `tests/native/test_phase3_eligibility.py`.
- In the four `test_chunked_*_admission.py` files, remove only the assertions that call `phase3_c1_eligibility`. Each removed assertion compared the predicate to the real chunked admission; the real admission's own assertions in the same tests stay.
- Clean up the comment-only references.
- `native_route_eligibility` (the platform's import) is untouched.

**3f. One string-source gate (E4).** Replace `categorical_source_type_rejection`, `bucket_perturb_source_type_rejection` and `date_shift_source_type_rejection` with one `string_source_type_rejection(strategy, column, schema)`. It returns exactly `f"{strategy}_source_type_not_string:{column}:{typ}"` for each, matching today's codes byte for byte. Callers and the `if/elif` at `_real_type_admission.py:123-147` use it. The old names may stay as thin aliases only if a test imports them; otherwise remove them.

**3g. Stand-in guard (E7).** Add a test in `tests/native/` asserting that the stand-in config is NOT native-admitted on the chunked route. Its failure message must name the helper to migrate and the procedure. Fix the stale docstrings at `_chunked_categorical_support.py:4`, `_chunked_bucket_perturb_support.py:4` and `_chunked_date_shift_support.py:4`.

## 4. Design notes

- **Principles (dev-rules `architecture.md`):**
  - Open/closed: new operators register a descriptor instead of editing about 14 tables.
  - Single source of each fact: one place per fact.
  - Deep module: the registry hides the per-route derivations behind named views.
- **Concrete change pain:** adding an operator touched 8-14 source files per Phase C slice (C2 8, C4 9, C3 14, C5a 10). After R1 it touches the registry plus the operator's kernel-call branch(es) and its route-specific gates.
- **Established pattern followed:** the engine's own `native/_capabilities.py` registry (frozen spec, `KeyError` on an unknown key, totality test), not an invented one.
- **Where facts live:** each shared operator fact lives only in `_operator_registry.py`. Diagnostics stay in capabilities, and route-specific contracts stay in their route modules (3d).
- **Named refactorings (Fowler):**
  - Introduce Parameter Object → `OperatorSpec`.
  - Replace Magic Literal → operator ids.
  - Extract Function / Consolidate Duplicate Conditional Fragments → the string-source gate.
  - Remove Dead Code → `phase3_c1_eligibility`.
  - Move Function → `C1_PROVIDER_ALLOWLIST`.

## 5. Acceptance tests (written first; red-before recorded)

1. **Snapshot equality.** The behavior-preservation proof.
   - For every derived view, a test asserts it equals the literal value copied from engine main `01c560da` into the test as an explicit literal: `ALLOWED_OPERATOR_IDS`, `BACKEND_BY_OPERATOR_ID`, `_COMPANION_DEPENDENT_OPERATOR_IDS`, `_OPERATOR_REQUIRED_KERNEL`, `_ROUTED_DIAGNOSTIC_OBLIGATIONS`, `_POSITIVE_KERNEL_EVIDENCE_OPERATOR_IDS`, `SLICE_STRATEGIES`, `OPERATOR_ID_BY_STRATEGY`, `_ADMITTED_RESIDENT_TYPES`, `NATIVE_KERNEL_STRATEGIES`, `NATIVE_POOL_STRATEGIES`, `_INDEX_KERNEL_STRATEGIES`, `_COMPANION_STRATEGIES`, `_TOKENIZING_STRATEGIES`, `_NULL_ON_EMPTY_STRATEGIES`, `_NATIVE_GROUP_KEY_SIBLING_TYPES`, `C1_PROVIDER_ALLOWLIST`, and the six op-id constants.
   - Written and green on main BEFORE the refactor, so it pins today's values.
2. **Registry totality.** `OPERATORS` keys equal `SLICE_STRATEGIES`. Operator ids are unique. Every spec's `planned_backend` and `required_kernel` are consistent: `arrow_python` ⇔ `required_kernel is None`, and `rust_pool_select` ⇔ `shape == "pool"`. `operator_spec("nope")` raises `KeyError`.
3. **Single source sentry.** An AST/grep sentry asserts no module under `src/decoy_engine/execution/` other than `_operator_registry.py` contains a string literal equal to any operator id (e.g. `"native_keyed_hash"`). Strategy names are excluded because they are config vocabulary used everywhere.
4. **Leaf-module sentry.** `_operator_registry.py` imports only the stdlib and `pyarrow`. The existing fresh-import probe passes. `import decoy_engine.execution._unified_slice_admission` in a fresh interpreter does not import `decoy_engine.execution.physical`.
5. **String gate.** Parametrized over the three strategies × {string, large_string, int64, null}: the reason (or None) equals main's exact output.
6. **Phase3 deletion.** No reference to `phase3_c1_eligibility` / `Phase3Eligibility` remains in `src/` or `tests/` (grep gate). `native_route_eligibility` still imports and behaves identically. The chunked admission tests that lost their phase3 assertions still pass.
7. **Stand-in guard** (3g).
8. **No behavior change.** The existing suites pass unchanged except for the files in 3e/3f/3g: `tests/native`, `tests/physical`, `tests/unit/execution`, `tests/parity/native`, `tests/sentry`.

Mutation targets (each must be killed):
- change Faker's `planned_backend`
- set categorical `positive_kernel_evidence=True`
- drop an operator from `OPERATORS`
- change date_shift's `required_kernel` to `crypto`
- make the string gate emit a different prefix
- reintroduce an operator-id literal in `_shadow_operators.py` (sentry 3)

## 6. Risk, rollback, gates

- **Risk: R1-R2.** A behavior-preserving refactor on the admission path, proven by snapshot equality plus unchanged suites. Rollback is a revert.
- **Gates:** Codex plan gate → Sonnet build (tests first) → dennis (including the design check) → Codex final → ci-mirror → merge under the standing authority.

## 7. Open questions for the plan gate

1. Is keeping the old table names as derived views (3c) the right trade, versus updating consumers to call the registry directly? Keeping names gives a smaller diff and lets existing tests keep pinning behavior.
2. Should the backend vocabulary move to the registry (3a), or into a separate tiny constants module both import?
3. Is the E2 split to R1b justified by the inventory's route-difference list, or is any part of E2 safe enough to fold in here (for example, the identical passthrough/redact/truncate branches)?
