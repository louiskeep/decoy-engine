Status: plan (revision 3, author = Opus). Codex plan gate: rounds 1 and 2 REVISE folded; round 3 pending.
Rules consulted: 00-universal, development-loop, refactoring, architecture, testing, code-review, scope-discipline

# R1b: one kernel step per operator, shared by both routes

Roadmap: decoy-platform `docs/ROADMAP.md`, Refactor track, R1b "before C5b-ii". Source: `decoy-platform docs/records/2026-10-06-codebase-health-and-refactor-report.md` §2.1 E2, split out of R1 (`docs/plans/2026-10-06-r1-operator-registry.md` §1).

Branch: `feat/r1b-merged-dispatch` off engine main `a71800f3` (R1 merged).

**Behavior-preserving refactor.** No routing decision, reason code, output byte, output type, evidence key or value, row-error coordinate, or exception type changes on either route.

## 1. Goal and scope

Each native operator is dispatched twice today: once in the unified full-frame route (`physical/_shadow_operators.py::run_operator`, about 229 LOC) and once in the chunked route (`native/_chunk_masking.py::_mask_chunk_native` and its three helpers, about 328 LOC). Underneath the route-specific wrappers, both chains do the same three things per operator: resolve config defaults, assemble the kernel's arguments, and reduce `derive_calls` to "a compiled kernel ran". That logic is written twice and has already drifted in form (defaults resolved at bind time in one route and per chunk in the other).

C5b-ii adds the position-keyed non-deterministic Faker variant to both routes. Without R1b it is two more parallel branches; with R1b it is one parameter field and one step branch.

In scope:
- **E2a. One typed parameter object per operator**, resolved once per run by one resolver both routes call.
- **E2b. One kernel step per operator**, called by both routes, returning the output, whether a compiled kernel ran, and any format-error positions.
- **E2c. Route adapters keep every route contract** (listed in §2.2), unchanged.

Out of scope (deliberately):
- Faker pool resolution (`_resolve_pool` in the coordinator vs `_resolve_faker_pools` in the chunked route). Both already call `resolve_faker_pool_identity`; the remaining difference is route lifecycle.
- Merging the two evidence records (`OperatorCallEvidence` vs `NativeRouteEvidence`). Different contracts by design.
- The categorical positive-evidence question (open follow-up from route evidence).
- Any change to admission, preflight, or the schema rule, except one representation-only read in unified admission (§3b: `_unified_slice_resident_types.py:58` reads the group_key sibling name from the binding). In particular `_prepared_categoricals` (`native/_chunked_entry.py:375`), which runs before route selection, skips inadmissible columns, and feeds the output-type pin on BOTH legs, is untouched.

## 2. Established facts (inventory at `a71800f3`, verified by reading the code)

### 2.1 Duplicated (to be shared)

| Item | Unified route | Chunked route |
|---|---|---|
| redact default `"REDACTED"` | `_shadow_operators.py:166` | `_chunk_masking.py:355` |
| truncate length coercion (`length if isinstance(length,int) else 0`) | `_shadow_operators.py:168-171` | `_chunk_masking.py:360-363` |
| truncate `keep` | bind-time `_resolve_truncate_keep` (`_shadow_bindings.py:183-185`) | per-chunk `_resolve_truncate_keep` (`_chunk_masking.py:364`) |
| bucket default `"month"` | `_shadow_bindings.py:276` | `_chunk_masking.py:187` |
| date_shift day defaults | `_shadow_bindings.py:327-328` | `_chunk_masking.py:225-226` |
| group_key `length` 16, `str(prefix)`, namespace `f"group_key/{target}"` | `_shadow_bindings.py:295-301` | `_chunk_masking.py:264-268` |
| categorical categories + CDF | `_shadow_bindings.py:250-262` (inline) | `prepare_categorical` (`_categorical_prepared.py:34-76`) |
| `sum(derive_calls) > 0` | `_shadow_operators.py:261, 298, 311` | `_chunk_masking.py:197, 234, 273` |
| kernel argument assembly, all nine operators | `_shadow_operators.py:163-313` | `_chunk_masking.py:352-478` + helpers |

Already shared: `sample_faker_array`, `_resolve_truncate_keep`, and `prepare_categorical` (the unified admission gate `categorical_config_rejection` already delegates to it, `_operator_config_rejections.py:44-60`).

### 2.2 Route contracts (stay in the adapters, unchanged)

| Concern | Unified adapter keeps | Chunked adapter keeps |
|---|---|---|
| Missing crypto / raw-hex companion | catches `CryptoExtensionUnavailableError` and raises `ShadowDifference(NATIVE_COMPANION_UNAVAILABLE)` for hash and group_key | never catches; preflight already downgraded the table (`_dispatch.py:474-497`) |
| group_key kernel | passes `raw_hex_kernel=None` so the kernel loads inside the call each batch (its only companion probe, pinned by `test_shadow_group_key.py:808`) | passes the preflight kernel and asserts it is loaded |
| group_key sibling | `batch.select([group_by])` from the coordinator | `raw_chunk.select([group_by])`, before null casts |
| Kernel invariant failures | `operator_invariants_fail_loud` wraps the call | propagate raw |
| Categorical variant | asserts `categorical_deterministic`; never positional | positional variant with `row_offset`; empty positional chunk is idle, uncounted, typed empty `string` |
| Evidence | `OperatorCallEvidence`: hash, faker, categorical set `compiled_kernel_executed=True`; bucket_perturb, group_key, date_shift OR it with "ran"; `actual_operator`, `executed`, `batches_run` | `NativeRouteEvidence`: hash and categorical set compiled; faker sets `pool_select_executed` and `pool_select_calls += 1`; bucket_perturb, date_shift, group_key set compiled if ran, else add the column to `kernel_idle` |
| date_shift errors | batch-local `RowError(column, i, "format_error")`; the coordinator rebases; requires `column` | chunk-local positions into `format_errors[name]`; requires the channel when positions exist |
| bucket_perturb degenerate output | none here (whole-column `null_on_empty` in `_shadow_assembly`) | casts an empty or all-null chunk to `pa.nulls(len)` |
| Categorical preparation | inline at bind time (replaced by `prepare_categorical`, which admission already runs) | `_prepared_categoricals` once per table before route choice; tolerant (skips inadmissible); also feeds `build_schema_rule(categorical_columns=...)` on both legs. Stays exactly as is. |
| "Ran" signal per kernel | from `derive_calls` | same. `derive_calls` semantics differ by kernel and are NOT normalized: group_key appends `n` for any non-empty sibling, all-null included (a null cell is stringified and hashed, `_group_key_kernel.py:130-142`), and nothing for zero rows; date_shift appends `int(usable.any())` (`_date_shift_ext.py:181-182`); bucket_perturb per its kernel. |
| Timing / bookkeeping | `timed_strategy` per node | `perf_counter` per column per chunk; `counted`; `unconfigured`, `stored_index` |
| Namespace guard | `KeyBinding` required (asserted) | `col_seed.namespace`, with `or ""` applied for categorical, bucket_perturb, date_shift only (not hash) |

### 2.3 Test surface

- `ExecutionBinding(...)` is constructed in 9 test sites (`tests/physical/test_shadow_{faker_lifecycle,date_shift,coordinator x2,diff_catalog,bucket_perturb,categorical,group_key}.py`, `test_unified_route_evidence.py`) and in `_shadow_bindings.py`. The per-operator fields are read in 10 test lines.
- 8 tests patch kernel names in `_chunk_masking` via `monkeypatch.setattr(_chunk_masking, "native_*", ...)`. Seven are POSITIVE spies that wrap the real kernel and inspect calls (`test_chunked_{categorical,date_shift,bucket_perturb}_admission.py`, `test_chunked_group_key_admission.py:569,610`, `test_chunked_group_key_parity.py:287`, `test_chunked_nondet_categorical_parity.py:341`). One is a NEGATIVE sentinel that must never run (`test_dispatch_faker.py:1374-1391`, `_fail_hash`).
- Malformed-binding tests: `test_shadow_date_shift.py` builds bindings through `_binding(**overrides)` (`:966`) and `dataclasses.replace(n.execution, **changes)` (`:750`), and a parametrized table (`:1115-1135`) asserts exact `AssertionError` messages for `key_binding=None`, `date_shift_date_format=None`, `date_shift_min_days=None`, and so on. `:1003` asserts `needs_index_kernel` is False when `date_shift_date_format=None`. The missing-`column` test (`:987-995`) passes a deliberately unusable kernel (`object()`) and relies on the guard firing BEFORE the kernel call (`_shadow_operators.py:341-347`).
- Helper homes: `sample_faker_array` and `_resolve_truncate_keep` live in `_chunk_masking.py:51-159`, imported by `_shadow_operators.py:25`, `_shadow_bindings.py:29`, `_chunked_entry.py:50`, re-exported by `_dispatch.py:52` for tests, and imported by `tests/native/test_sample_faker_array.py:17`.
- Production readers of the eleven fields, outside `_shadow_bindings.py` and `_shadow_operators.py` (full `grep` of `src/`, round 2): `physical/_plan.py:165-167` (`needs_index_kernel`), `physical/_shadow_coordinator.py:340,360` (group_key sibling feed), and `_unified_slice_resident_types.py:58` (`binding.group_key_group_by`, called from unified admission at `_unified_slice_admission.py:493`, before execution). No other reader exists; the `mask.categorical_deterministic` strings in `native/_draw_site_providers.py` and `_determinism_protocol.py` are draw-site ids, not field reads.
- No platform or CLI code reads `ExecutionBinding` fields (Codex static search, rounds 1 and 2).
- `test_shadow_date_shift.py:596` patches `_shadow_coordinator.run_operator`; the coordinator keeps calling `run_operator` by that name.
- Sentry: `test_production_execution_modules_are_byte_identical_to_origin_main` allowlists the execution modules a branch may change; new modules need entries. `native/` must not import `physical/` (`test_physical_seam_disconnection.py:64-72`).

## 3. Decisions

**3a. Parameter objects (E2a).** New module `src/decoy_engine/execution/native/_operator_params.py`. One frozen dataclass per operator, holding resolved values only (no raw config, no key material):

| Class | Fields |
|---|---|
| `PassthroughParams` | none |
| `RedactParams` | `redact_with: Any` |
| `TruncateParams` | `length: int` (coerced as today), `keep: str`, `mask_char: Any` |
| `HashParams` | `namespace: str \| None`, `truncate: Any` |
| `FakerParams` | `namespace: str \| None` |
| `CategoricalParams` | `prepared: PreparedCategorical` (unchanged class: categories, cdf, positional), `namespace: str \| None` |
| `BucketPerturbParams` | `bucket: str`, `date_format: str`, `namespace: str \| None` |
| `GroupKeyParams` | `group_by: str`, `length: int`, `prefix: str`, `namespace: str` (synthesized `f"group_key/{target}"`) |
| `DateShiftParams` | `date_format: str`, `min_days: int`, `max_days: int`, `namespace: str \| None` |

`OperatorParams` is the union. One resolver:

```python
def resolve_operator_params(
    strategy: str,
    *,
    target: str,
    provider_config: Mapping[str, Any],
    namespace: str | None,
    prepared_categorical: PreparedCategorical | None = None,
) -> OperatorParams
```

It holds every default in §2.1, each exactly once. It does not validate or decline: categorical takes the `PreparedCategorical` its caller already has (unified: from `prepare_categorical`; chunked: from `_prepared_categoricals`) and raises `AssertionError` only if none was given, which is a wiring bug. Values are byte-identical to today's: same defaults, same coercions, same `str()` of prefix, same `.get` vs `[]` (date_format stays `cfg["date_format"]`).

`_resolve_truncate_keep` moves into `_operator_params.py` (its only real caller becomes the resolver). `_chunk_masking` and `_dispatch` keep importing the name from its new home so the `_dispatch` test re-export still resolves.

**3b. `ExecutionBinding` carries the parameter object.** Replace the eleven per-operator fields (`categorical_deterministic`, `categorical_categories`, `categorical_cdf`, `bucket_perturb_bucket`, `bucket_perturb_date_format`, `group_key_group_by`, `group_key_length`, `group_key_prefix`, `date_shift_date_format`, `date_shift_min_days`, `date_shift_max_days`) with one `params: OperatorParams | None = None` field (Fowler: Introduce Parameter Object; Replace Type Code with Subclasses).

- Marker predicates stay FIELD-SENSITIVE, so every partial binding behaves as today:
  - `needs_index_kernel` = `pool_binding is not None` OR (`params` is `CategoricalParams` and not `prepared.positional`) OR (`params` is `BucketPerturbParams` and `params.bucket is not None`) OR (`params` is `DateShiftParams` and `params.date_format is not None`). This mirrors `_plan.py:164-167` field for field.
  - "Bound group_key node" (coordinator `:340,360`, resident types `:58`) = `params` is `GroupKeyParams` and `params.group_by is not None`. A small helper on `ExecutionBinding`, e.g. `group_key_sibling` returning that name or None, is the one place this is computed, and all three readers call it.
  - `categorical_deterministic` becomes "`params` is a `CategoricalParams` whose `prepared.positional` is False".
- `_unified_slice_resident_types.py:58` reads the sibling through that helper. This is the only admission edit and it is representation-only: the missing or invalid sibling, schema-shape and masked-sibling declines are unchanged.
- `resolved_config` stays (evidence and diagnostics read it). `KeyBinding` stays; for every keyed operator (hash, faker, categorical, bucket_perturb, group_key, date_shift) its namespace is taken from `params.namespace`, so the two cannot diverge.
- `_shadow_bindings.execution_binding_for_slice_node` keeps every `return None` guard it has today (key source, namespace presence, faker pool bindability, string categories, date_format presence, int bounds, sibling input-schema rebind), then calls the resolver. For categorical it calls `prepare_categorical(deterministic=True, ...)` and returns None on a reason, which matches today's `# pragma: no cover` branches (admission already ran the same function).

Test edits for 3b, enumerated rather than called mechanical:
- (i) The 9 construction sites pass `params=<XParams>(...)` instead of the per-operator kwargs.
- (ii) The 10 field reads become `binding.params.<field>`.
- (iii) Every malformed-binding override (`_binding(**overrides)`, `dataclasses.replace(n.execution, **changes)`, the parametrized table at `test_shadow_date_shift.py:1115-1135`, and `:1003`) is mapped LITERALLY: a field set to None becomes `params=dataclasses.replace(p, <field>=None)`, never `params=None`. Each case keeps its exact expected outcome and message (for example `:1003` still expects `needs_index_kernel is False` for `date_format=None`, which the field-sensitive predicate gives). A separate new case covers `params=None` for each marker predicate. The builder lists every mapped case in the build record (file:line, old override, new override, unchanged expectation).
- (iv) `test_run_operator_asserts_categorical_determinism` builds its binding with `prepared.positional=True` instead of `categorical_deterministic=False`. Same intent: a position-keyed categorical must never reach the unified operator.

**3c. Kernel step (E2b).** New module `src/decoy_engine/execution/native/_operator_step.py`. `sample_faker_array` moves here from `_chunk_masking.py`; `_chunk_masking`, `_shadow_operators` and `tests/native/test_sample_faker_array.py` import it from the new home (the test file's import line is the only change there). Dependency direction is acyclic: `_operator_params` (leaf: registry-free, imports `_categorical_prepared`, determinism defaults) <- `_operator_step` (imports params and kernels) <- `_chunk_masking`, `physical/_shadow_operators`. Neither new module imports `physical/`.

```python
@dataclass(frozen=True)
class StepResult:
    out: pa.Array
    ran: bool | None          # None: unkeyed transform, no compiled-kernel claim
    format_error_positions: tuple[int, ...] = ()

def run_kernel_step(
    params: OperatorParams,
    source: pa.Array | pa.ChunkedArray,
    *,
    mask_key: bytes | None,
    native_threads: int | None,
    index_kernel: IndexDerivationKernel | None = None,
    raw_hex_kernel: RawHexDerivationKernel | None = None,
    pool: ValuePool | None = None,
    sibling: pa.Table | None = None,
    row_offset: int = 0,
) -> StepResult
```

One `isinstance` dispatch on the params class. It calls the kernels and holds the one `derive_calls` reduction. `ran` per operator, matching today's flag writes exactly:

- passthrough, redact, truncate: `None`.
- hash, faker: `True`.
- categorical: `True`, except a positional zero-row source returns `StepResult(pa.array([], pa.string()), ran=False)` without calling the kernel (moved from `_chunk_masking.py:405-409`).
- bucket_perturb, group_key, date_shift: `sum(derive_calls) > 0`, with each kernel's own `derive_calls` semantics unchanged (§2.2). For group_key that means True for any non-empty sibling, all-null included, and False only for zero rows.

Namespace handling reproduces today's call sites exactly: `params.namespace or ""` for categorical, bucket_perturb and date_shift; the raw value for hash and faker; the synthesized string for group_key. The step's index-kernel `None` guards are `AssertionError`s with `# pragma: no cover`, as today.

The step does NOT catch `CryptoExtensionUnavailableError`, touch evidence, build `RowError`s, cast nulls, or check route preconditions.

**3d. Adapters (E2c).**

Unified, `run_operator` keeps its name, signature and return type. Per batch, in this order (today's exception precedence):
1. Operator-specific binding guards, with today's exact messages. For example, date_shift checks `key_binding`, then that `params` is a `DateShiftParams` with all three fields non-None ("no resolved date_format/min_days/max_days"), then `column` ("no target column"), then `index_kernel`. Categorical checks `key_binding`, then determinism, then params, then `index_kernel`.
2. `run_kernel_step(...)`, inside the existing `try/except CryptoExtensionUnavailableError` for hash and group_key. group_key passes `raw_hex_kernel=None` and `sibling=group_key_sibling`.
3. Evidence writes from §2.2: hash, faker and categorical set `compiled_kernel_executed=True`; the other three OR it with `ran`.
4. `RowError`s from `format_error_positions`, then `actual_operator`, `executed`, `batches_run`.

`_run_date_shift` is removed; its guard order is kept by step 1. `operator_invariants_fail_loud` keeps wrapping the call in the coordinator.

Chunked:
- `params_by_column: dict[str, OperatorParams]` is built ONCE per table in native-route setup in `_chunked_entry.py`, after admission and next to pool resolution. It covers only admitted, configured columns, and categorical entries wrap the `PreparedCategorical` already returned by `_prepared_categoricals` (positional via `prepare_positional_categorical` exactly as today). It is passed into every `_mask_chunk_native` call in place of `categorical_by_column`. `_prepared_categoricals` and the schema-rule input are unchanged.
- Per column, `_mask_chunk_native` keeps `unconfigured`/`stored_index` handling, timing and `counted`, calls `run_kernel_step`, and applies its §2.2 contracts:
  - the raw-hex assertion before the group_key call;
  - group_key's sibling from `raw_chunk` before null casts;
  - the bucket_perturb `pa.nulls` cast;
  - `format_errors`, with the channel-missing `AssertionError`;
  - the evidence and `kernel_idle` writes;
  - `counted=False` for an idle positional categorical.
- `_mask_bucket_perturb`, `_mask_date_shift` and `_mask_group_key` are removed.

**3e. Patch re-pointing.**
- The seven positive spies move to `_operator_step`. Only the module object in `monkeypatch.setattr` and the matching `real = ...` line change. Each keeps or gains an assertion that the spy was called, with the call count it expected before.
- The negative sentinel (`test_dispatch_faker.py:1391`) moves to `_operator_step` with NO added call assertion: it must still never run.
- Because the unified route now calls the same module, a re-pointed spy also intercepts unified calls. The builder verifies each of the eight tests runs only the chunked route; if one does not, it stops and reports rather than editing assertions.

**3f. Sentry and size.**
- Add `native/_operator_params.py` and `native/_operator_step.py` to the byte-identity allowlist (`test_physical_seam_disconnection.py:210-275`) and to the leaf/seam import sentry (neither imports `physical/`).
- Module-size census: a module over 600 gets an exact-count entry. A module that drops to 600 or below has its entry REMOVED, not lowered (`test_module_size.py:207`).

## 4. Design notes

- **Seam choice.** The shared part is "given resolved parameters and a source, call the kernel and say whether it ran". That is the deepest interface available here: route adapters see one call and one small result, and the per-operator knowledge (defaults, coercions, namespace rules, derive-call accounting) sits behind it. A wider merge, one dispatcher owning evidence and errors, was rejected because evidence, error channels, companion handling and categorical preparation differ on purpose (§2.2) and would need route flags inside the shared code.
- **Validation stays where it is.** The resolver resolves; it never declines. Declining is admission's job (and `_prepared_categoricals`' tolerant skip on the chunked side), so moving resolution cannot change which route a table takes.
- **Open-closed check.** After R1b, adding an operator or a variant (C5b-ii's positional Faker) touches one registry entry, one params class with its resolver branch, one step branch, and only the route contracts it genuinely needs. Neither adapter's dispatch grows a branch for a variant with no new route contract.
- **Not done.** No class hierarchy with per-operator objects and virtual methods: one `isinstance` dispatch over frozen dataclasses is shorter and keeps kernel calls greppable.

## 5. Acceptance tests (written first; red-before recorded)

All new tests are under `tests/native/` unless noted. "Baseline" tests are committed BEFORE any source change, pass on unmodified `a71800f3`, and must stay green after.

1. **Baseline: cross-route kernel kwargs (Hypothesis).** For all nine operators (passthrough, redact, truncate, hash, faker, categorical, bucket_perturb, group_key, date_shift; categorical in its deterministic variant only, since the positional variant cannot bind on the unified route and is covered by tests 3, 7 and the existing positional parity suite), generate configs inside each operator's admission rules: truncate lengths with keep/from_end forms; redact value absent or present; hash truncate absent or int; bucket week, month, quarter or absent; date_shift bounds absent, int, or swapped; group_key length absent or a valid even int, and prefix absent, str or None; categorical categories with weights absent or valid; faker over a fixed small pool. Each example runs the same source through the unified `run_operator` and the chunked `_mask_chunk_native` (one chunk) and asserts the recorded kwargs are equal, except `raw_hex_kernel` for group_key. Constraints:
   - (a) Assert the unified binding is non-None. A config that does not bind is a generator bug.
   - (b) Import every candidate call-site module (`_shadow_operators`, `_chunk_masking`, `_operator_step` when present) before patching, capture each original kernel before replacing it, and patch with `raising=False`.
   - (c) Reset recordings inside each example.
   - (d) Assert exactly one kernel call per route per example.
   - (e) Settings: `max_examples=60`, `derandomize=True`.
2. **Baseline: literal kwargs snapshot.** For 3 fixed configs per operator, assert the exact kwargs dict each route passes to the kernel. This catches both routes drifting the same way.
3. **Baseline: namespace boundary characterization.** These are direct calls, since admitted configs always have a namespace and test 1 cannot see this rule. Today they call the chunked helpers (`_mask_bucket_perturb`, `_mask_date_shift`, the categorical branch via `_mask_chunk_native`) with `namespace=None` and assert the kernel receives `""`; for hash they assert the kernel receives `None`. After the change the same assertions run against `run_kernel_step` (the test imports whichever exists). This replaces rev 1's claim that admitted-config mutations prove the `or ""` rule.
4. **Baseline: ran-signal characterization.** Using today's chunked helpers and unified `run_operator`, record "ran" for zero-row, all-null non-empty, and mixed sources for bucket_perturb, date_shift and group_key (group_key all-null non-empty: True; zero rows: False). After the change the same matrix must hold through `StepResult.ran` and through each adapter's evidence.
5. **Existing behavior net.** These must pass, edited only per §3b and §3e: `tests/physical/test_unified_route_evidence.py`, `tests/physical/test_shadow_*.py`, `tests/native/test_chunked_*_{parity,admission}.py`, `tests/native/test_dispatch_faker.py`, `tests/native/test_native_dispatch.py`, `tests/native/test_sample_faker_array.py`.
6. **Resolver single-source (narrow).** In `physical/_shadow_operators.py`, `physical/_shadow_bindings.py` and `native/_chunk_masking.py` only, an AST check confirms that none of these literals and calls appears: `"REDACTED"`, `"month"`, the group_key `16`, `DEFAULT_MIN_DAYS`/`DEFAULT_MAX_DAYS`, an f-string starting `group_key/`, and calls to `_resolve_truncate_keep`. Other modules (admission, schema rule, kernels, oracle, out-of-core) are out of scope.
7. **Step result contract.** `ran` is None for the three unkeyed transforms and True for hash and faker. Categorical is True, except positional on zero rows, which returns a typed empty string array without calling the kernel (spy). The bucket_perturb, date_shift and group_key matrix matches test 4.
8. **Adapter contracts kept.**
   - (a) Unified hash and group_key still raise `ShadowDifference(NATIVE_COMPANION_UNAVAILABLE)` when the kernel raises `CryptoExtensionUnavailableError`. The existing tests `test_shadow_diff_catalog.py:85` and `test_shadow_group_key.py:657,808` pass with no edits beyond §3b.
   - (b) Unified group_key passes `raw_hex_kernel=None`, checked by a spy on `native_group_key` at the step.
   - (c) Chunked faker writes `pool_select_*`, not `compiled_kernel_executed` (existing `test_dispatch_faker.py:347,372`).
   - (d) The missing-`column` date_shift test (`test_shadow_date_shift.py:987-995`) passes unedited, with its unusable kernel.
9. **Unified group_key activation end to end.** A unified-slice run of a group_key job (through the coordinator, not a direct `run_operator` call) asserts that the unified route activated and admitted the node, that the output equals the oracle's, and that unified admission's resident-types check read the sibling. The existing negative admission cases (missing sibling, masked sibling, schema shape) pass unedited. This catches a missed binding reader that direct operator comparisons cannot see.
10. **Marker predicates.** For each predicate (`needs_index_kernel`, the group_key sibling helper), assert the result for a full binding, for each partial-field binding (field set to None), and for `params=None`, matching today's field-based results.
11. **Import seam.** `_operator_params` and `_operator_step` import nothing from `physical/`, enforced by the extended sentry.
12. **Mutation check** (a build step, not a test file). Mutate each resolver default (`"month"` to `"week"`, group_key `16` to `18`, the redact default, a date_shift default, the truncate coercion) and the `or ""` rule in the step. Test 1, 2 or 3 must fail for each mutation. Record the results.

Red-before: tests 6, 7, 10 and 11 fail on `a71800f3` (the modules, helper or params field do not exist, or the literals are still in the adapters). Tests 1 to 4 and 9 are green-before by design.

Every new test also runs under the Python 3.10 mirror (`~/.cache/decoy-ci-mirror-venv`).

## 6. Risk, rollback, gates

| Risk | Mitigation |
|---|---|
| A re-pointed spy silently stops intercepting | Positive spies assert call counts (§3e). The negative sentinel stays negative. |
| Moving resolution changes which route a table takes | The resolver never declines. Admission and `_prepared_categoricals` are untouched (§1, §4). |
| Guard order changes which exception a malformed binding raises | §3d step 1 fixes the order. Malformed-binding cases keep their exact messages (§3b iii). The missing-column test is unedited (test 8d). |
| Evidence semantics merged by accident | The step returns `ran`; each adapter writes its own flags (§3d). The exact-evidence suite pins it. |
| Import cycle from moved helpers | Fixed direction (§3c); the import sentry covers it. |
| A binding reader is missed | Full `src/` grep recorded in §2.3; test 9 exercises unified admission end to end; mypy flags any attribute that no longer exists. |
| Module size | New modules stay under 600; census entries are removed at 600 or below. |

Rollback: revert the merge commit. No data, config or API surface changes.

Gates: Codex plan gate on this plan; Sonnet build; dennis; Codex final gate; ci-mirror; then merge under the standing full-green authority.

## 7. Plan-gate history

- Rev 1: initial.
- Codex round 1, REVISE (3 HIGH, 4 MEDIUM). Folded in rev 2:
  - H1: categorical namespace is now a `CategoricalParams` field.
  - H2: `_prepared_categoricals` is untouched; params are built once per table in native setup; the resolver never declines.
  - H3: group_key all-null is True, matching its kernel; added the ran-signal characterization (test 4).
  - M1: the moved helpers have explicit homes and an acyclic direction.
  - M2: positive and negative spies are separated; malformed-binding mapping is enumerated; guard precedence is fixed.
  - M3: baseline test constraints added; namespace rule is tested by direct characterization (test 3).
  - M4: the literal sentry is narrowed to the three adapter modules.
  - Census entries at 600 or below are removed.
- Codex round 2, REVISE (1 HIGH, 1 MEDIUM). Folded in rev 3:
  - H: the missed reader `_unified_slice_resident_types.py:58` is now in scope through one helper; an end-to-end unified group_key test was added (test 9).
  - M: marker predicates are field-sensitive and malformed-binding mapping is literal; `params=None` cases added (test 10).
  - Test 1 now enumerates all nine operators.
