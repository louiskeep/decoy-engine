Status: plan (revision 1, author = Opus). Codex plan gate: pending.
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
- Any change to admission, preflight, or the schema rule.

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
| Timing / bookkeeping | `timed_strategy` per node | `perf_counter` per column per chunk; `counted`; `unconfigured`, `stored_index` |
| Namespace guard | `KeyBinding` required (asserted) | `col_seed.namespace`, with `or ""` applied for categorical, bucket_perturb, date_shift only (not hash) |

### 2.3 Test surface

- `ExecutionBinding(...)` is constructed in 9 test sites (`tests/physical/test_shadow_{faker_lifecycle,date_shift,coordinator x2,diff_catalog,bucket_perturb,categorical,group_key}.py`, `test_unified_route_evidence.py`) and in `_shadow_bindings.py`. The per-operator fields are read in 10 test lines.
- 8 tests spy on kernel names in `_chunk_masking` via `monkeypatch.setattr(_chunk_masking, "native_*", spy)` (`test_chunked_{categorical,date_shift,bucket_perturb}_admission.py`, `test_chunked_group_key_admission.py:569,610`, `test_chunked_group_key_parity.py:287`, `test_dispatch_faker.py:1391`, `test_chunked_nondet_categorical_parity.py:341`).
- `test_shadow_date_shift.py:596` patches `_shadow_coordinator.run_operator`; the coordinator keeps calling `run_operator` by that name.
- Sentry: `test_production_execution_modules_are_byte_identical_to_origin_main` allowlists the execution modules a branch may change; new modules need entries. `native/` must not import `physical/` (`test_physical_seam_disconnection.py:64-72`).

## 3. Decisions

**3a. Parameter objects (E2a).** New module `src/decoy_engine/execution/native/_operator_params.py`. One frozen dataclass per operator, holding resolved values only (no raw config, no key material):

| Class | Fields |
|---|---|
| `PassthroughParams` | none |
| `RedactParams` | `redact_with: Any` |
| `TruncateParams` | `length: int` (coerced as today), `keep: str` (`_resolve_truncate_keep`), `mask_char: Any` |
| `HashParams` | `namespace: str \| None`, `truncate: Any` |
| `FakerParams` | `namespace: str \| None` |
| `CategoricalParams` | reuse `PreparedCategorical` as is (categories, cdf, positional); `namespace` passed separately (see 3c) |
| `BucketPerturbParams` | `bucket: str`, `date_format: str`, `namespace: str \| None` |
| `GroupKeyParams` | `group_by: str`, `length: int`, `prefix: str`, `namespace: str` (synthesized `f"group_key/{target}"`) |
| `DateShiftParams` | `date_format: str`, `min_days: int`, `max_days: int`, `namespace: str \| None` |

`OperatorParams` is the union. One resolver, `resolve_operator_params(strategy, *, target, provider_config, namespace, deterministic) -> OperatorParams`, holds every default listed in §2.1, each exactly once. Categorical calls `prepare_categorical` (or `prepare_positional_categorical` for the positional variant) and raises `AssertionError` if it returns a reason, because admission already ran the same function. A config admission would reject is a wiring bug, not a decline.

Values are byte-identical to today's: same defaults, same coercions, same `str()` of prefix, same `.get` vs `[]` (date_format stays `cfg["date_format"]`; a missing key still raises `KeyError` at resolve time instead of first chunk, which admission makes unreachable).

**3b. `ExecutionBinding` carries the parameter object.** Replace the eleven per-operator fields (`categorical_deterministic`, `categorical_categories`, `categorical_cdf`, `bucket_perturb_bucket`, `bucket_perturb_date_format`, `group_key_group_by`, `group_key_length`, `group_key_prefix`, `date_shift_date_format`, `date_shift_min_days`, `date_shift_max_days`) with one `params: OperatorParams | None = None` field (Fowler: Introduce Parameter Object; Replace Type Code with Subclasses). `needs_index_kernel` and the coordinator's two marker reads (`bucket_perturb_bucket`, `group_key_group_by`) become `isinstance` checks on `params`. `categorical_deterministic` becomes "`params` is a `PreparedCategorical` with `positional=False`"; the unified adapter's determinism assertion checks exactly that, and `test_run_operator_asserts_categorical_determinism` builds its binding with `positional=True` params instead of `categorical_deterministic=False` (same intent: a position-keyed categorical must never reach the unified operator). `resolved_config` stays (evidence and diagnostics read it). `KeyBinding` stays; its namespace is set from `params.namespace` for keyed operators, so the two cannot diverge.

`_shadow_bindings.execution_binding_for_slice_node` calls `resolve_operator_params` and keeps its own `return None` guards (key source, faker pool bindability, sibling input-schema rebind). Its per-operator default code is deleted.

The 9 test construction sites change their keyword arguments to `params=<XParams>(...)` and nothing else; the 10 field reads change to `binding.params.<field>`. No assertion changes.

**3c. Kernel step (E2b).** New module `src/decoy_engine/execution/native/_operator_step.py`:

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

One `match`/`isinstance` dispatch on the params class. It imports the kernels (`native_passthrough`, `native_redact`, `native_truncate`, `native_keyed_hash`, `sample_faker_array`, `native_categorical`, `native_categorical_positional`, `native_bucket_perturb`, `native_group_key`, `native_date_shift`) and holds the one `derive_calls` reduction. `ran` per operator, matching today's flag writes exactly:

- passthrough, redact, truncate: `None`.
- hash, faker: `True`.
- categorical: `True`, except a positional zero-row source returns `StepResult(pa.array([], pa.string()), ran=False)` without calling the kernel (moved from `_chunk_masking.py:405-409`).
- bucket_perturb, group_key, date_shift: `sum(derive_calls) > 0`.

Namespace handling reproduces today's call sites exactly: `namespace or ""` for categorical, bucket_perturb, date_shift; the raw value for hash and faker; the synthesized string for group_key. The index-kernel `None` guards live here once (`AssertionError`, `# pragma: no cover`, as today).

The step does NOT catch `CryptoExtensionUnavailableError`, does not touch evidence, does not build `RowError`s, and does not cast nulls.

**3d. Adapters (E2c).**
- `run_operator` becomes: resolve inputs from the binding, call `run_kernel_step` inside the existing try/except for hash and group_key, apply the evidence writes from §2.2, build `RowError`s from `format_error_positions` (still requiring `column`), and increment `actual_operator` / `executed` / `batches_run`. It keeps its name, signature and return type. `_run_date_shift` is removed (its body is the step plus the RowError build). The categorical determinism assertion stays in the adapter.
- `_mask_chunk_native` builds `params_by_column` once per run (next to `prepare_chunked_categoricals`, which it replaces for categorical: the resolver returns the same `PreparedCategorical`), then per column calls `run_kernel_step` and applies its §2.2 contracts. `_mask_bucket_perturb`, `_mask_date_shift`, `_mask_group_key` are removed; the bucket_perturb null cast and the raw-hex assertion stay inline in the adapter.
- `operator_invariants_fail_loud` continues to wrap the unified call; the chunked route stays unwrapped.

**3e. Spy re-pointing.** The 8 `_chunk_masking` spies move to `_operator_step` (the kernels' new call site). Only the module object in `monkeypatch.setattr` and the matching `real = ...` line change. Because the unified route now calls the same module, each spy also intercepts unified calls; each of these 8 tests runs only the chunked route (verified in DEVELOP; any that also run the unified route are flagged, not edited further).

**3f. Sentry and size.** Add `native/_operator_params.py` and `native/_operator_step.py` to the byte-identity allowlist and the module-size census if over 600 (expected about 150 and 200 LOC). `_chunk_masking.py`, `_shadow_operators.py` and `_shadow_bindings.py` shrink; their census entries (if any) are lowered to the exact new count. Neither new module imports `physical/`.

## 4. Design notes

- **Seam choice.** The shared part is "given resolved parameters and a source, call the kernel and say whether it ran". That is the deepest interface available: route adapters see one call and one small result, and the per-operator knowledge (defaults, coercions, namespace rules, derive-call accounting) is hidden behind it. A wider merge (one dispatcher owning evidence and errors) was rejected because evidence, error channels and companion handling differ on purpose (§2.2) and would need route flags inside the shared code.
- **Why reuse `PreparedCategorical`.** It already is the typed parameter object for categorical, and admission already runs `prepare_categorical`; making the unified binding use it removes the third copy of category/CDF validation.
- **Open-closed check.** After R1b, adding an operator (or a variant such as C5b-ii's positional Faker) touches: one registry entry (R1), one params class plus its resolver branch, one step branch, and any route contract it genuinely needs. Neither adapter's dispatch grows a branch for a variant that has no new route contract.
- **Not done.** No strategy-pattern class hierarchy with per-operator objects and virtual methods: a single `match` over frozen dataclasses is shorter and keeps the kernel calls greppable.

## 5. Acceptance tests (written first; red-before recorded)

All new tests under `tests/native/` unless noted. "Baseline" tests are written and committed BEFORE any source change and must pass on unmodified `a71800f3` (they pin today's behavior); they must stay green after.

1. **Baseline: kernel-argument equivalence across routes (Hypothesis).** For each of the nine operators, generate admitted configs (strategies drawn inside each operator's admission rules: valid truncate lengths and keep/from_end forms, redact values including absent, hash truncate absent/int, bucket in week/month/quarter or absent, date_shift bounds absent/int incl. swapped, group_key length absent/even int and prefix absent/str/None, categorical categories and weights incl. absent, faker with a fixed small pool). Run the same source through the unified `run_operator` (binding from `execution_binding_for_slice_node`) and the chunked `_mask_chunk_native` (one chunk), spying on every kernel name in EVERY module that might hold it (`_shadow_operators`, `_chunk_masking`, and after the change `_operator_step`; `raising=False`). Assert the recorded kwargs are equal across routes, except the documented route differences (`raw_hex_kernel` for group_key). Settings: `max_examples=60`, `derandomize=True` so CI is stable.
2. **Baseline: literal kwargs snapshot.** For 3 fixed configs per operator, assert the exact kwargs dict passed to the kernel (literal values). Catches both routes drifting the same way.
3. **Baseline: route outputs and evidence unchanged.** Already covered by the existing parity and evidence suites (`tests/physical/test_unified_route_evidence.py`, `tests/native/test_chunked_*_parity.py`, `test_chunked_*_admission.py`); no new test, but they are named in the build record as the behavior net and must pass unedited apart from §3b/§3e mechanical changes.
4. **Resolver single-source.** `resolve_operator_params` is the only place the literals `"REDACTED"`, `"month"`, `16` (group_key length), `DEFAULT_MIN_DAYS`/`DEFAULT_MAX_DAYS` and the `f"group_key/{...}"` format appear in `src/decoy_engine/execution/` outside oracle strategy modules (AST/text sentry with an explicit allowlist of the oracle handler files).
5. **Step result contract.** Per operator: `ran` is `None` for the three unkeyed transforms, `True` for hash/faker/categorical, and `False` for bucket_perturb/date_shift/group_key on an all-null source and `True` on a non-null one; a positional categorical on a zero-row source returns a typed empty string array and does not call the kernel (spy).
6. **Adapter contracts kept.** (a) Unified hash and group_key still raise `ShadowDifference(NATIVE_COMPANION_UNAVAILABLE)` when the step raises `CryptoExtensionUnavailableError` (existing tests `test_shadow_diff_catalog.py:85`, `test_shadow_group_key.py:808,657` must pass unedited). (b) Unified group_key still passes `raw_hex_kernel=None` (spy on the step's call to `native_group_key`). (c) Chunked faker still writes `pool_select_*`, not `compiled_kernel_executed` (existing `test_dispatch_faker.py:347,372`).
7. **Import seam.** `_operator_params` and `_operator_step` import nothing from `physical/` (extend the existing leaf/seam sentry).
8. **Mutation check (build step, not a test file).** Mutate each default in the resolver (e.g. `"month"` to `"week"`, `16` to `18`) and the `or ""` namespace rule; tests 1 or 2 must fail for each. Record results.

Red-before for 4, 5, 7: they fail on `a71800f3` because the modules do not exist. Tests 1 and 2 are green-before by design (they pin behavior).

Every new test also runs under the Python 3.10 mirror (`~/.cache/decoy-ci-mirror-venv`).

## 6. Risk, rollback, gates

| Risk | Mitigation |
|---|---|
| A spy re-point silently stops intercepting | Each re-pointed test asserts the spy was called (most already do); the builder adds `assert calls` where missing. |
| Default resolution moves from per-chunk to once per run, changing when a bad config raises | Admission runs the same validation first; resolver raises `AssertionError` on a reason (wiring bug). Test 1 covers admitted configs only. |
| Evidence semantics merged by accident (categorical always-compiled vs positional idle; faker pool_select vs compiled) | Step returns `ran`; each adapter writes its own flags (§3d). Existing exact-evidence tests (`test_unified_route_evidence.py:228-535`) pin it. |
| `ExecutionBinding` shape change breaks a platform or CLI caller | Grep decoy-platform and CLI for the eleven field names before the change; record the result (expected: none, the type is internal to `execution/physical`). |
| Module size | New modules under 600; shrinking modules get exact census counts. |

Rollback: revert the merge commit. No data, config or API surface changes.

Gates: Codex plan gate on this plan; Sonnet build; dennis; Codex final gate; ci-mirror; then merge under the standing full-green authority.

## 7. Plan-gate history

- Rev 1: initial.
