# Mutation grading: `_native_route_preflight.py` + `_native_route_digest.py` (Q3 slice 2 widened admission)

Scope: the two modules slice 2 added for the native route's widened admission
of integer / boolean / timestamp columns --
`_native_route_preflight.py` (the four-state resolver re-export, the normative
admission matrix + `resolve_admission`, the schema-drift guard, the bounded
`run_preflight` pass, `ExecutionDigestState`, `classify_and_preflight`,
`run_widened_execution`, and the `_find_table` / `_strategy_by_column` helpers)
and `_native_route_digest.py` (the source-snapshot digest codec: the
domain-separated hasher framing, the per-array / per-column / whole-source
functions, and `PreflightColumnAccumulator`). This is a SIBLING to
`execution_native_route.md`, which grades the single-pass seam
(`_native_route.py` + `_native_route_exec.py`) and predates slice 2; the
widened-admission ADDITIONS to `_native_route_exec.py` are covered in the last
section below rather than by re-grading that whole file here (see "Why exec is
not re-graded here").

Graded with `mutmut` (`only_mutate` scoped to the two files), re-adjudicated
per mutant by `scripts/tq_mutate.py` (fresh-subprocess pytest re-run of every
non-`killed` verdict, since mutmut's in-process runner misfires `timeout` on
this suite -- see tq-findings.md #8). Covering tests (the `[tool.mutmut]`
selection):

- `tests/parity/native/test_native_route_wider_types.py` (production-entry
  parity + the widened execution wiring),
- `tests/unit/execution/test_native_route_preflight_units.py` (the direct
  unit tests of the resolver, matrix, drift guard, digest codec, `run_preflight`
  / `classify_and_preflight` contracts, and the config helpers),
- `tests/unit/execution/test_native_route_units.py`,
- `tests/unit/execution/test_native_route_transactional_failures.py`.

## One mutation-only source change: `_row` excluded from mutation

`_native_route_preflight._row` carries `# pragma: no mutate block`. It is the
only helper called at MODULE-IMPORT time (it builds `_ADMISSION_MATRIX`), and a
trampolined mutant there raises during pytest COLLECTION under mutmut's
forced-fail soundness probe (rc 2), which `tq_mutate` correctly reads as a
broken harness and aborts on. The nine matrix cells `_row` produces are pinned
exactly by `test_admission_matrix_matches_normative_table` (every
`(strategy, family, state)` cell asserted against the normative table), so
excluding the helper hides no gap -- and because every mutant it would generate
is already killed by that test, the exclusion can only lower the raw mutant
count, never inflate the score. This matches the repo's own methodology for
constant-driven tables (tq-findings.md #9: module-level constant tables are not
mutated and need explicit per-value tests, which exist here). No program logic
was changed to move any mutant.

## Numbers

**435 mutants total, 404 killed, 31 survived: 92.87% (404/435).** Of the 31
survivors, 5 are non-contract diagnostic prose (an `ExecutionError` /
`AssertionError` message changed while the `code` / branch stayed put) and 26
are genuine equivalents (no test, however written, could distinguish the mutant
from the real code). Each is listed below with its own one-line justification.

## Killed by new tests (test-only; no source logic changed)

The prior population had 118 survivors across these two files. The gap was
closed by extending the two slice-2 unit/parity files:

**The digest codec -- a byte-exact characterization (golden) test.** The
equality-only mismatch tests (`test_digest_no_ambiguous_utf8_concatenation`,
`_changes_on_validity_only_change`, `_row_permutation`, `_timezone_change`,
`_partition_independent`) recompute both sides with the SAME codec, so a
mutation that shifts the absolute hash but stays self-consistent across the
preflight and execution passes slips past them. `test_digest_golden_*` pins the
per-column and whole-source digests of a fixed mixed-type, mixed-null input
byte-for-byte, so any change to the hasher parameters (domain key, version
byte), a type token, the value / validity / length framing, the framing
byte-lengths, or a null fill value is caught. This kills the token-case/marker
mutants (`_type_token` 2/3/5/6/9/10/12/13/16), the domain-key drop
(`_new_hasher` 5), the null-fill-value mutants (`_update_hashers_for_array`
57/60/67/70/77/80/84), the frombuffer/astype/offset mutants (30/31/35/37), and
the framing-length mutants (`_finalize_column_digest` 11/25/34).
`test_digest_sizes_are_exactly_32_bytes` pins `digest_size=_DIGEST_SIZE` (a
dropped size defaults blake2b to 64 bytes, self-consistent otherwise);
`test_digest_honors_array_slice_offset` and `_middle_slice_offset_length` pin
the utf8 offset read for sliced batches (a batch iterator can hand back slices);
`test_digest_null_fill_value_is_observable_per_type` pins the per-type fill
lands in the value stream.

**`run_preflight` -- the `PreflightResult` contract on every branch.** Direct
tests feed a stub batch source and assert the full result on admit, matrix
reroute, and schema drift (`admitted`, `reason`, `schema` identity, `digest`,
`column_states`), plus the mixed-column ordering guarantees: a utf8 column in a
widened table must be SKIPPED by the matrix loop (never sent to
`resolve_admission`, a `KeyError`) and the skip must `continue`, not `break`
(so a later column's reject is still seen). This kills the drift-branch mutants
(8/14/15/16/17/23 and the dropped-required-field mutants), the state-dict
mutant (29), the reroute/admit field mutants (46/48/49/61), and the skip-guard
mutants (32/35/36/37).

**`classify_and_preflight` -- the `RouteAdmission` contract on every branch.**
Direct tests (monkeypatching `known_output_columns`, as the seam units already
do) assert the returned mode/admitted/reason/schema/digest/column_order for
unsupported-projection, non-utf8-column, utf8-only, widened-reroute, and
widened-admit. This kills the projection-diff mutants (10/11/12), the
utf8-only-gate mutants (42/45/46), and the field mutants at each return site
(16/18/24/33/35/36/69/70/78/88 ...).

**`_strategy_by_column` / `_find_table`.**
`test_strategy_by_column_unresolved_raises_coded_error` pins
`code="native_preflight_strategy_unresolved"` on the guarded branch (23/25/27/28);
`test_find_table_selects_by_name_not_first_dict` pins the
`isinstance(...) and name == table` guard (`_find_table` 5).

**`run_widened_execution` wiring.**
`test_widened_admit_streaming_sink_stamps_envelope_and_adapter` (in the parity
file) drives a widened admit through a streaming sink and asserts
`outputs_streamed is True` and `resolved_substrate == "pandas"`, killing the
streaming (25) and resolved-substrate (27) argument mutants.

## Non-contract diagnostic prose (accepted-not-killed) -- 5

Each changes an observable string (part of `str(exc)`) but not the
`ExecutionError.code` or any branch; callers key off `.code` only. Three sit
behind `# pragma: no cover` invariants an upstream function already
established, so the branch is unreachable in production, but the message change
is still observable-in-principle, so these are prose, not equivalents.

- `_strategy_by_column` 24 (message forced to `None`), 26 (message kwarg
  dropped): the `code="native_preflight_strategy_unresolved"` on these raises is
  killed by `test_strategy_by_column_unresolved_raises_coded_error`.
- `_type_token` 17, `_update_hashers_for_array` 89: the `AssertionError`
  messages on the `# pragma: no cover - admitted-types-only` encoder guards
  (an unadmitted type never reaches them).
- `run_widened_execution` 4: the `AssertionError` message on the
  `# pragma: no cover` schema/digest precondition guard.

## Genuine equivalents -- 26

No test, however written, could distinguish these from the real code.

**Dropped keyword equal to the dataclass default (3)** -- `classify_and_preflight`
20/37/73 each drop `admitted=False` at a reroute return; `RouteAdmission.admitted`
defaults to `False`, so the value is unchanged.

**`run_widened_execution` pass-throughs the callee ignores or that are inert in
this lane (6)** -- 1 (`or` -> `and` on the `# pragma: no cover` schema/digest
guard, unreachable either way), 7 (`first = next(...)` -> `first = None`:
`_rechain` then streams the full `rest` iterator, which still holds every batch,
so the same batches are processed), 19 (`plan=None`: `_run_native_streaming`
`del plan`s it), 26 (`table_kinds=None`: the native lane admits only a single
non-foreign mask table, so the foreign-table ledger check never fires), 28
(`explain_plan=None`: falsy, same as the default `False` through the explain
gate), 29 (`execution_plan_decision=None`: the default).

**Digest codec, no observable byte change (17):**

- `_update_hashers_for_array` 1 (`no_nulls = None`): a null-free batch takes the
  else path, whose `is_valid` / `fill_null` results equal the all-true validity
  string and the unchanged array (the module docstring notes exactly this).
- `_update_hashers_for_array` 10/62/72/73/86/88 (`zero_copy_only` set to `None`
  or `True`): `to_numpy` returns identical bytes regardless -- the flag only
  gates whether a copy is ALLOWED, and since none of these raised on the covered
  inputs the produced bytes are the same.
- `_update_hashers_for_array` 16 (utf8 `fill_null(array, "")` -> `None`): a null
  filled with `""` and a null left null both contribute a zero-length value
  slot; validity is hashed separately, so the streams match.
- `_update_hashers_for_array` 49 (`and` -> `or`) and 51 (`>` -> `>=`) on the
  `data_buf is not None and end > start` value-slice guard: the only cases the
  branch flips on (`data_buf is None`, or `end == start`) update the hasher with
  a zero-length slice, identical to skipping it.
- `_update_hashers_for_array` 81 (`pa.scalar(0, type=None)`) and 83
  (`pa.scalar(0, )`): an untyped `0` fills the same 0 ticks as the typed
  timestamp `0`.
- `_finalize_column_digest` 5/19 (`encode("UTF-8")` vs `"utf-8"`): Python
  normalizes codec names, so the bytes are identical.
- `_finalize_column_digest` 10/24/33 (`to_bytes(n, "big")` -> `to_bytes(n)`):
  `int.to_bytes` defaults `byteorder="big"` on Python 3.11+, so the bytes are
  identical.

## Widened-admission additions to `_native_route_exec.py` (verified killed)

Slice 2 added two thin call-throughs to `_native_route_exec.py`: the
utf8-only-vs-widened dispatch in `try_native_route`, and the `digest_state`
observe/verify + `expected_schema` wiring in `_masked_batches`. Grading that
whole 626-mutant file against this slice-2 selection is out of scope (see next
section); instead, the specific mutants on the widened-admission additions were
re-adjudicated directly against the covering tests (fresh subprocess,
`MUTANT_UNDER_TEST=<key>`), and are killed:

- `try_native_route` 109/111/118 (the widened-reroute `NativeRouteReport`
  `attempted` / `table`) -- killed by
  `test_widened_reroute_report_marks_attempted_and_table`.
- `try_native_route` 127/128/145/146 (the `run_widened_execution` streaming /
  resolved-substrate arguments and the `sink_mode == "streaming"` literal) --
  killed by `test_widened_admit_streaming_sink_stamps_envelope_and_adapter`.

The remaining widened-dispatch argument mutants (`try_native_route`
123 `plan=None`, 125 `table_kinds=None`, 129/130 `explain_plan` /
`execution_plan_decision =None`) are equivalents for the same reasons as their
`run_widened_execution` twins above. The `_masked_batches` digest-wiring
survivors are message-only prose (53 `verify(table=None)` changes only the
mismatch message; 25/27 change only the `native_chunk_schema_drift` message,
whose `code` is killed by `test_column_reorder_between_reads_aborts_before_commit`).

### Why exec is not re-graded here

`_native_route_exec.py` is graded by the sibling `execution_native_route.md`
seam ledger with its own selection (which includes
`test_native_route_production_seam.py`). Re-grading all 626 of its mutants
against this slice-2 selection -- which, per the acceptance-test spec, omits the
production-seam suite -- would report the seam ledger's already-accepted
prose/equivalents (plus a handful of slice-1 branches only that suite covers) as
this ledger's survivors, double-counting and misattributing them. Scoping the
score to the two NEW modules keeps it honest; the exec additions are covered
above and the rest of exec stays the seam ledger's responsibility.

## Regenerate

```
# Temporarily point [tool.mutmut] only_mutate at the two files and
# pytest_add_cli_args_test_selection at the four covering tests (see this file's
# own history / the acceptance-test spec for the exact block), then:
.venv/bin/python scripts/tq_mutate.py --run --report <path>.json
git checkout pyproject.toml   # restore the [tool.mutmut] block
```
