Status: record

# T4 eligibility: measurement, fill, and adjudication

- **Plan:** `docs/plans/2026-08-29-native-efficiency-test-plan.md`, batch T4 (section 4) and the
  method in section 3.
- **Scope:** `src/decoy_engine/execution/native/_plan.py` (`native_route_eligibility`,
  `_column_rejection`, `_native_rejection`, `_fk_group_rejections`, `compile_native_plan`, and
  `NativeExecutionPlan.output_schema_for`) and `_requirements.py` (`NATIVE_KERNEL_STRATEGIES`,
  `native_kernel_rejection`, `hash_config_rejection`, `truncate_config_rejection`,
  `redact_config_rejection`, `resolve_input_arrow_type`, `_DTYPE_TO_ARROW`, `requirements_for`).
  Bar: kill every mutant that changes an admission verdict (accepted/rejected), a coded reason, or
  which route a table takes.
- **Branch:** `docs/native-efficiency-test-plan`, worktree `.claude/worktrees/native-test-plan`.
- **Harness reused, not re-derived:** `scripts/native-testing/python_mutation_pilot.py` (T0).
  Neither module carries a crypto surface itself (`_kernels_keyed.py` owns that, graded in T3), so
  both ran without `--readjudicate-killed`, matching T0/T3's precedent for non-crypto modules.
- **Remediation (Codex gate, this revision):** the first pass graded both modules against only
  `test_native_plan.py` + `test_native_plan_config_aware.py`. That selection is complete for
  `_plan.py` (confirmed: its tally is bit-for-bit identical with or without the extra files below),
  but `_requirements.py` has two MORE guard files that were missed --
  `tests/native/test_requirements.py` (direct unit tests of `requirements_for` and its helpers) and
  `tests/native/test_native_dispatch.py` (drives `compile_native_plan`/`native_route_eligibility`
  through the dispatch layer). Omitting them inflated `_requirements.py`'s survivor count (73 where
  the true figure is 40) and produced one wrong adjudication: `requirements_for` mutant 54 was
  called "equivalent/unconsumed field," but it is a real fail-closed-path violation (`_required_
  prepasses` fed a `None` config eagerly raises `AttributeError` for a `date_shift` node instead of
  the intended graceful non-native fallback) that `test_requirements.py`'s own `date_shift` tests
  already kill once included. This revision re-measures both modules with the COMPLETE selection
  and re-audits every survivor against it; see the AFTER tables below for the corrected figures.

## Method: measure first

Ran branch coverage and the mutation pilot against the test selection that actually guards each
module before writing any test, per section 3 rule 1: `tests/native/test_native_plan.py` +
`tests/native/test_native_plan_config_aware.py` for the first pass (later found incomplete for
`_requirements.py` -- see Remediation above), corrected in this revision to the complete
four-file selection for both modules: those two files plus `tests/native/test_requirements.py`
and `tests/native/test_native_dispatch.py`.

## BEFORE: coverage

```
coverage run --branch -m pytest -q tests/native/test_native_plan.py tests/native/test_native_plan_config_aware.py
coverage report --include=*/execution/native/_plan.py,*/execution/native/_requirements.py -m
```

| Module | Stmts | Branch | Cover | Missing |
|---|---:|---:|---:|---|
| `_plan.py` | 139 | 58 | 92% | 111, 114, 116->115, 230->238, 232, 269, 278-279, 324, 328 |
| `_requirements.py` | 144 | 58 | 90% | 191, 205, 228->225, 230, 237, 241, 250, 254, 258, 261, 352 |

## BEFORE: mutation

Run with the two-file selection, before the incomplete-selection gap was found (see Remediation
above; `_plan.py`'s figures here are unaffected by that gap and reproduce identically under the
complete selection, confirmed below):

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_plan.py \
  --tests tests/native/test_native_plan.py tests/native/test_native_plan_config_aware.py --timeout 60

python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_requirements.py \
  --tests tests/native/test_native_plan.py tests/native/test_native_plan_config_aware.py --timeout 60
```

| Module | Total mutants | Killed | Survived |
|---|---:|---:|---:|
| `_plan.py` | 250 | 208 | 42 |
| `_requirements.py` | 279 | 181 | 91 (+7 no-tests, resolved to survived by the standalone rerun) |

`_requirements.py`'s BEFORE numbers above under-count kills relative to what the module's full
guard suite actually catches: `test_requirements.py` and `test_native_dispatch.py` pre-date this
batch and already kill some of these mutants on their own, they simply were not in the selection
being measured. The AFTER section below re-measures under the corrected, complete selection, which
is the number this batch's fill is actually graded against.

Reading every survivor's mutant body (`mutants/src/.../_plan.py` and `_requirements.py`) against
what actually consumes each field sorted the raw survivors into three groups:

1. **Demonstrated admission-verdict gaps** (real: change accepted/rejected, a coded reason
   string, or a route field `_dispatch.py` reads). These got a test.
2. **Message-text-only or default-literal-only** survivors (the value never reaches a boolean
   the platform branches on). Adjudicated equivalent, matching T2/T3's precedent.
3. **Unconsumed-field** survivors: `NodeRequirements.required_input_columns`,
   `.required_prepasses`, `.required_state_tables`, `.diagnostic_reducers`, `.lowering_id`, and
   `NativePlanNode.capabilities`/`.draw_family`/`.key_source` are read by no code outside
   `_plan.py`/`_requirements.py` themselves (confirmed by grep across the tree) -- they are
   inert planning metadata the module docstring already flags as reserved for a later phase, not
   part of today's admission verdict or route. A mutant that nulls one of these fields is
   equivalent for THIS batch's bar, whatever it might mean once a later phase starts reading them.

One structural finding shaped the fill: `NativeExecutionPlan.output_schema_for` is a method on a
`@dataclass(frozen=True)` class, the exact shape plan section 3.6 names as unreliably mutated --
mutmut generated **zero** mutants for it (confirmed: `grep -c "def .*output_schema_for"
mutants/.../_plan.py` returns 1, the trampoline dispatcher only). Its `continue`/`return None`
branches are graded by three direct, targeted tests instead of a mutation score, per the plan's
prescribed workaround.

## Fill: gaps closed

All additions are new tests in `tests/native/test_native_plan_config_aware.py`; no production
code changed (see Production changes below for the one exception considered and rejected).

- **`generate_columns` coded rejection** (`test_generate_columns_flag_is_rejected_with_exact_code`):
  no existing config ever set a truthy `generate_columns`, so the gate, its dict key, and its
  literal rejection string were all unexercised.
- **Config defaults with an absent key** (`test_table_present_with_no_columns_key_is_accepted`,
  `test_config_with_no_tables_key_is_treated_as_no_table`): every existing config always sets
  `"columns"`/`"tables"`, so `.get(key, ())`'s default arm never ran.
- **Missing-name placeholder** (`test_column_missing_both_name_and_strategy_uses_placeholder_name`):
  every existing config column sets `"name"`, so the `"?"` fallback never ran.
- **Exact-string rejections** (`test_output_type_indeterminate_rejection_embeds_exact_name_and_strategy`,
  `test_no_native_kernel_rejection_embeds_exact_name_and_strategy`,
  `test_unclassified_strategy_string_is_rejected_with_exact_code`): every prior assertion on these
  three coded reasons was a substring check (`"output_type_indeterminate" in r`), which cannot
  catch the embedded name/strategy being swapped for `None` or a wrong value. Pinning the full
  tuple closes that.
- **`output_schema_for`'s three branches** (`test_output_schema_for_excludes_table_with_any_non_native_node`,
  `test_output_schema_for_skips_nodes_on_a_different_table`): the merge-to-`None`-on-any-
  indeterminate-node rule and the per-table node filter, both previously untested (the tool-excluded
  finding above).
- **`compile_native_plan` field fidelity** (`test_compile_native_plan_node_fields_match_the_underlying_work_node`):
  no test compared `NativePlanNode.columns`/`.strategy` against the `WorkNode` they are derived
  from -- these two fields are what `_dispatch.py` actually routes on
  (`column = node.columns[0]`, `scalar_columns.append((column, node.strategy))`), so a null value
  here is a real route-tag bug, not cosmetic. The same test's capabilities assertion
  (`plan.nodes[0].capabilities == capabilities_for("hash")`) additionally proves a scalar node
  resolves its OWN strategy's capabilities rather than a composite/fk-group placeholder's --
  `_plan.py`'s `_resolved_strategy` has an inverted-condition mutant that silently swaps them for
  every non-composite node without ever raising.
- **Type-preserving output schema** (`test_passthrough_output_schema_preserves_input_arrow_type`,
  `test_hash_output_schema_is_string_typed_with_exact_field_name`): every existing test used
  `hash`/`redact`/`truncate` (none of which preserve the input type), so `_output_arrow_schema`'s
  `passthrough`/`shuffle` branch, and the exact field name/type on either branch, were unexercised.
- **`resolve_input_arrow_type` robustness** (`test_resolve_input_arrow_type_with_no_profile_is_none_not_a_crash`,
  `..._skips_non_matching_tables`, `..._skips_non_matching_columns`,
  `..._column_absent_from_matching_table_returns_none`): a public function
  (`_requirements.__all__`); every existing profile has exactly one table and one column, so the
  loop's `continue` arms and the `profile=None` fail-closed path never ran.
- **Kernel-availability gate, exact route tag** (`test_capability_friendly_kernel_less_strategy_is_exactly_python_only`,
  `test_output_type_indeterminate_node_fallback_policy_is_exactly_python_only`): every existing
  `compile_native_plan` assertion checks `!= "native"` or `all(... == "native")`, never the exact
  non-native literal, so a typo'd `"python_only"` return, or `requirements_for` silently dropping
  `kernel_reason` before it reaches `_fallback_policy`, had nothing to catch it.
- **`_native_rejection`'s boolean chain, by construction** (`test_native_rejection_boolean_corners_via_synthetic_strategy`,
  4 cases): every strategy actually registered in `_capabilities.py` happens to make the OR-chain's
  three terms redundant with each other (every row-local entry has `is_global=False`; every
  non-row-local entry already has `is_global` or `needs_global_row_identity` True), so an
  `and`-typo'd mutant of that chain reaches the identical verdict for every LIVE strategy. A
  monkeypatched synthetic `StrategyCapabilities` entry (`_CAPS["__t4_native_rejection_probe__"]`)
  proves the boolean logic itself, independent of what the registry happens to contain today.
- **FK-group rejection wiring** (`test_fk_group_rejection_wiring_when_group_capabilities_are_non_native`):
  `<group>`'s capabilities are always native-ready today (locked by
  `test_fk_group_capabilities_are_native_ready_today`), so `_fk_group_rejections`' own reject
  branch, and the `native_route_eligibility` wiring around it (the table-absent-but-profile-given
  path, and threading the right table name into the FK scan), had no live input to exercise them.
  Monkeypatching `_CAPS["<group>"]` to a hypothetical non-native shape -- the same "if these
  capabilities ever change" framing the existing precedent test already uses -- proves the wiring.
  Two relationships (one for an unrelated table, one for the queried table) additionally proves a
  non-matching relationship is skipped, not treated as a reason to stop scanning.

## AFTER: coverage

Measured with the same complete four-file selection as the AFTER mutation run below (the extra
two files raise both modules' coverage over the two-file measurement: `_plan.py` 98%->99%,
`_requirements.py` 96%->98%, since `test_requirements.py`/`test_native_dispatch.py` exercise a few
more branches directly):

| Module | Stmts | Branch | Cover | Missing |
|---|---:|---:|---:|---|
| `_plan.py` | 139 | 58 | 99% | 116->115 |
| `_requirements.py` | 144 | 58 | 98% | 191, 352 |

Both remaining misses map onto adjudicated fields below: `_plan.py`'s `116->115` is
`output_schema_for`'s field-dedup branch (unreachable-by-contract: no valid `WorkNode` list
produces two nodes covering the same column on one table); `_requirements.py`'s line 191 is
`_required_input_columns`'s sibling-column append (an unconsumed field) and line 352 is
`_config_gate_rejection`'s empty-`node.columns` guard (unreachable-by-contract: every admitted
`WorkNode` kind has at least one column by construction).

## AFTER: mutation

Re-measured with the CORRECT, complete four-file selection for both modules (`test_native_plan.py
test_native_plan_config_aware.py test_requirements.py test_native_dispatch.py`):

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_plan.py \
  --tests tests/native/test_native_plan.py tests/native/test_native_plan_config_aware.py \
          tests/native/test_requirements.py tests/native/test_native_dispatch.py --timeout 60

python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_requirements.py \
  --tests tests/native/test_native_plan.py tests/native/test_native_plan_config_aware.py \
          tests/native/test_requirements.py tests/native/test_native_dispatch.py --timeout 60
```

| Module | Total mutants | Killed | Survived | LOGIC score |
|---|---:|---:|---:|---:|
| `_plan.py` | 250 | 242 | 8 | 96.80% |
| `_requirements.py` | 279 | 239 | 40 | 85.66% |

`_plan.py`'s tally is bit-for-bit identical to the two-file measurement (242/8/96.80%): the two
extra guard files add no new coverage of `_plan.py` specifically. `_requirements.py`'s corrected
tally (239 killed, up from 206; 40 survived, down from 73) reflects `test_requirements.py` and
`test_native_dispatch.py` already killing 33 mutants this batch's own fill gets no credit for --
including `requirements_for` mutant 54, previously misadjudicated equivalent (see Remediation
above and the corrected adjudication below).

Every survivor identified as a demonstrated gap above is gone. The remaining 48 survivors (8 + 40)
are the ones adjudicated equivalent/unreachable/tool-excluded below; none changes an admission
verdict, a coded reason, or a route field.

## Five-field adjudication

### `_plan.py` (250 mutants)

| Field | Count |
|---|---:|
| (a) Branch coverage | 99% |
| (b) Killed | 242 |
| (c) Equivalent (reason below) | 8 |
| (d) Unreachable-by-contract | 0 |
| (e) Tool-excluded | `output_schema_for` (0 mutants generated; graded by 3 direct tests) |

**Equivalent mutants (8), with reason:**

- `compile_native_plan` mutant passing `decoy_engine_version=None` to the inner `compile_plan`
  call: that value is stored only on the compiled `Plan.engine_version` field, read only by
  `_serialize.py`/`_manifest.py` for provenance -- never by any node-classification code this
  batch grades. `NativeExecutionPlan.engine_version` itself is set from the outer `engine_version`
  parameter directly, unaffected.
- `compile_native_plan` mutant passing `plan=None` to `requirements_for`: `requirements_for`'s own
  docstring documents `plan` as accepted for future resolution needs and "not read for the scalar
  node kinds today" -- confirmed by grep, no branch in either module reads it.
- `compile_native_plan` mutants nulling `NativePlanNode.draw_family` / `.key_source` (2 mutants):
  neither field is read anywhere outside `_capabilities.py`'s own tests (which construct
  `StrategyCapabilities` directly, not through a compiled plan).
- `_resolved_strategy` mutants breaking the `"composite"` / `"composite_fk_group"` match strings
  (ALL FOUR match-string survivors: two on `"composite"`, two on `"composite_fk_group"`):
  `build_work_list` (`_runner.py` lines 109/120) already hard-codes `strategy="<composite>"` /
  `strategy="<group>"` directly on the `WorkNode` for these two kinds, so the function's fallback
  branch (`return node.strategy`) returns the identical value the special-cased branch would.
  Breaking the match string is behaviorally unobservable for these two kinds specifically. This
  does NOT hold for an INVERTED first `if` condition, which also fires wrongly for every OTHER
  kind -- that distinct mutant is a real, separately-KILLED gap (closed by the new
  capabilities-equality test), not a survivor counted here.

### `_requirements.py` (279 mutants)

| Field | Count |
|---|---:|
| (a) Branch coverage | 98% |
| (b) Killed | 239 |
| (c) Equivalent (reason below) | 37 |
| (d) Unreachable-by-contract | 3 |
| (e) Tool-excluded | 0 |

**Killed, corrected (1):** `requirements_for` mutant 54 (`_required_prepasses(node, caps, None)`,
forcing `cfg=None`) was adjudicated equivalent in the first pass on the theory that
`required_prepasses` is an unconsumed field. That is true of the RESULT, but not of this mutant's
actual effect: for a `date_shift` node, `_required_prepasses` evaluates
`cfg.get("date_format")` eagerly (the `and` does not short-circuit past it), so `cfg=None` raises
`AttributeError` before any field is even constructed -- a planning CRASH, not a wrong value in an
unread field. This is a fail-closed-path violation (plan section 3, bar item: "kill every mutant
that changes ... a fail-closed path"), and `tests/native/test_requirements.py::
test_date_shift_without_format_needs_format_detect_prepass` (a `date_shift` node routed through
the real, non-`None` `_config_dict(node)`) already kills it -- it just was not in the measured
selection the first time. No new test needed; the complete selection already carries the kill.

**Equivalent mutants (37), by shape:**

- **Unconsumed `NodeRequirements` fields, message-only or dead-key-string shape** (20 mutants:
  `_required_input_columns` 14, `_required_prepasses` 6): `required_input_columns` and
  `required_prepasses` are read by no code outside `_requirements.py`/`_plan.py` themselves
  (confirmed by grep across `src/decoy_engine/` in this repo AND across `decoy-platform/**/*.py`,
  which has zero references to `NodeRequirements`, `NativePlanNode`, or any of its field names at
  all -- the native planning boundary is not wired into the platform yet). `_plan.py` copies
  `required_input_columns` into `NativePlanNode.input_projection`
  (`src/decoy_engine/execution/native/_plan.py:163`), but THAT field is itself read by nothing
  except a same-batch structural-consistency assertion in `test_native_plan.py` (`node.
  input_projection == node.requirements.required_input_columns`), which checks the copy matches
  its source, not that either value is behaviorally meaningful. `test_requirements.py` DOES assert
  directly on `req.required_input_columns` and `req.required_prepasses` for several strategies
  (its own unit-test contract for `requirements_for`'s helpers), which is why the survivor count
  here is far smaller than the raw mutant count in these two functions -- but the 20 that still
  survive even that direct testing are all mutations of the `group_by`/`order_by`/`anchor`/
  `reference_column` sibling-column lookup (dict keys, case, or a `None`-shortcut) and the
  `whole_column_pass`/`format_detect` prepass triggers for input combinations no live strategy or
  existing test constructs. None of the 20 changes an accept/reject verdict, a coded reason, or
  `fallback_policy` -- confirmed by reading each mutant body: none touches `kernel_reason`,
  `config_reason`, or any capability-derived boolean.
- **`_resolve_strategy_name`'s composite/fk-group match strings** (4 mutants): identical reasoning
  to `_plan.py`'s parallel function above -- `build_work_list` already hard-codes
  `strategy="<composite>"` / `"<group>"` directly on the `WorkNode` for these two kinds
  (`_runner.py` lines 109/120), so the function's fallback branch (`return node.strategy`) returns
  the identical value the special-cased branch would.
- **`native_kernel_rejection`'s `"<composite>"` exemption string** (2 mutants): a composite node's
  `fallback_policy` is already forced non-native by `caps.output_type_is_static == False` (checked
  first, in `_fallback_policy`), so whether this specific exemption also matches does not change
  the final verdict for the one node kind it can affect. (The parallel `"<group>"` exemption IS
  verdict-relevant -- `<group>`'s capabilities pass every other native-ready check -- and its own
  mutants are correctly killed by the existing `test_eligibility_with_profile_agrees_on_fk_group`.)
- **`redact_config_rejection`'s default `redact_with` literal** (2 mutants): the gate only tests
  `isinstance(redact_with, str)`, true for any string default regardless of its content.
- **`truncate_config_rejection`'s synthetic-config dict keys** (5 mutants): `check_truncate_config`
  reads the table/column `"name"` keys only to build its error MESSAGE
  (`src/decoy_engine/plan/_checks_truncate.py` lines 64/70, both via `.get(key, "?")`), never to
  decide whether to raise or which `.code` to raise with; `truncate_config_rejection`'s return
  value is built from `exc.code` and its own `name` parameter, never from the synthetic dict.
- **`_config_gate_rejection`'s redact-branch `name=None` argument** (1 mutant): `redact_config_rejection`
  embeds `name` only in its rejection message; `_fallback_policy` tests `config_reason is None`,
  never its text.
- **`requirements_for`'s message-only arguments** (3 mutants: `column_name = None`,
  `native_kernel_rejection(None, strategy_name)`, and the `"?"` no-columns fallback literal): all
  three feed only the `name` slot of `no_native_kernel`'s message, never observed by
  `_fallback_policy`'s boolean gate.

**Unreachable-by-contract (3):** `_config_gate_rejection`'s truncate branch (match-string mutants
14/15/16). `compile_plan` calls `check_truncate_config` on the FULL config before `build_work_list`
ever runs (`src/decoy_engine/plan/_compile.py` lines 252/555), so any truncate column reaching
`requirements_for` has already passed the identical check `truncate_config_rejection` re-runs on a
synthetic single-column config. No config admitted past `compile_plan` can make this branch
observably differ from skipping it: an admitted input either never reaches this code path
(invalid truncate config: `compile_plan` raises first) or always returns `None` regardless (valid
truncate config: the re-run check also passes). This is the plan's field (d) exactly -- a branch
no admitted input can reach a different outcome through -- not equivalent-by-coincidence.

## Property tests added, and evidence each is non-vacuous

- **Admitted-set-==-four sweep:** unchanged and still green
  (`test_admitted_set_is_exactly_the_four_native_kernels`, pre-existing from Task 2.6). Verified
  it still fails on demand: temporarily widening `NATIVE_KERNEL_STRATEGIES` to include `"fpe"` (by
  hand, reverted) flips the assertion (`accepted != {"passthrough", "redact", "truncate",
  "hash"}`), confirming the sentry still bites.
- **Compiler-vs-eligibility agreement:** unchanged and still green
  (`test_eligibility_agrees_with_compiler_on_config_gates`, pre-existing). The new
  `test_compile_native_plan_node_fields_match_the_underlying_work_node` extends the same principle
  to fields the prior agreement test didn't reach (`.columns`/`.strategy`/`.capabilities`); proven
  non-vacuous by direct reproduction: monkeypatching `requirements_for` to a hand-built variant of
  `x_requirements_for__mutmut_11`-shaped fault matching the real mutant bodies (see Method note
  below) demonstrably flips `fallback_policy`/`.capabilities` and fails the assertion.
- **Rust-type-correspondence sweep** (`test_hash_over_admitted_types_is_accepted`, pre-existing,
  unchanged): parametrized over every signed/unsigned integer width in both the numpy and
  pandas-nullable label forms, `bool`/`boolean`, and a tz-aware timestamp; paired with
  `test_hash_over_float_input_is_rejected` / `test_hash_over_naive_timestamp_is_rejected` for the
  two excluded shapes. Confirmed non-vacuous by the mutation run itself: every mutant of
  `_ADMITTED_NATIVE_HASH_TYPES`/`is_admitted_native_hash_type`/`_DTYPE_TO_ARROW` was already killed
  before this batch (none appear in either survivor list above), meaning a mutant widening the
  admitted set to admit float or narrowing it to reject an admitted int width is already caught.
- **`_native_rejection` boolean-corner property** (new,
  `test_native_rejection_boolean_corners_via_synthetic_strategy`): proven non-vacuous by the
  mutation rerun -- `_native_rejection` mutants 2 and 3 (an `or`-chain corrupted to `and` at two
  different positions) are absent from the AFTER survivor list, where they were present BEFORE.

## Method note: verifying an intended kill empirically, not just by inspection

One planned test (asserting `fallback_policy` for a kernel-less capability-friendly strategy)
initially looked, by inspection of the mutant's source diff, like it should also kill
`requirements_for` mutant 11. Direct reproduction (monkeypatching the `_requirements` module's
`requirements_for` name to the exact mutant body, then calling `compile_native_plan` through it)
showed otherwise: mutant 11 replaces the message-only `name` argument, not the `strategy` argument,
so it stays equivalent. The lesson generalizes: for anything non-obvious, reproduce the mutant's
effect directly (patch the real function, run the real code path) rather than trust a read of the
generated diff alone -- mutmut's numbering does not always match the shape a quick read suggests.

## Production changes

None. Every demonstrated gap closed with a test; the two production-code candidates considered
(hardening `_config_gate_rejection`'s dead truncate branch, and reading the currently-unconsumed
`NodeRequirements` planning fields from somewhere) were rejected because neither is reachable by
any admitted input today, and speculatively wiring up an unread field is scope creep the plan's
"fill only demonstrated gaps" rule forbids.

## Gates

- `ruff check` on the changed test file and both graded source files: clean.
- `ruff format --check` on the same: clean (3 files already formatted).
- `mypy src/decoy_engine testflight`: clean (428 source files).
- `pytest tests/native/ -q`: 411 passed, 1 skipped (pre-existing: the shared KAT fixture file is
  not generated in this environment).
- Phase 0 totality/drift sentries (`test_native_plan.py::test_eligibility_total_over_live_mask_registry`,
  `test_native_plan_config_aware.py::test_admitted_set_is_exactly_the_four_native_kernels`,
  `test_config_aware_gates_stay_total_over_live_registry`): unchanged, still green.

## Bar

Kill every mutant that changes an admission verdict, a coded reason, or a route field: met on both
modules under the complete, correct guard-test selection. `_plan.py` closed 6 demonstrated gaps
(compile_native_plan field fidelity plus the 5 eligibility-path gaps); its remaining 8 survivors
are equivalent by construction. `_requirements.py` closed 2 demonstrated gaps (the kernel-
availability bypass in `requirements_for`, closed by the same test that pins `fallback_policy`'s
exact route tag) plus the coverage-only gaps (type-preserving schema, `resolve_input_arrow_type`
robustness), and one further real gap (`requirements_for` mutant 54, a fail-closed-path violation)
turned out already closed by the pre-existing `test_requirements.py` once the guard selection was
corrected to include it; its remaining 40 survivors are equivalent (37) or unreachable-by-contract
(3), none of which changes an admission verdict.
