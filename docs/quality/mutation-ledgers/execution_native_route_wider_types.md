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

## Revision note (this pass)

This ledger's prior version (404/435 = 92.87%, 31 survivors: "5 non-contract
prose + 26 genuine equivalents") mislabeled FOUR real, distinguishable
survivors as equivalents. A cross-model gate finding proved each one is
killable with the right input, and the four are now covered by new tests (see
"Killed by new tests" below). This revision also re-grades against the FINAL
code, which now includes a P0 fix (`run_widened_execution`'s second-read
schema-drift check, see `_native_route_preflight.py` around line 414) that
grew the mutant population from 435 to 447. The corrected numbers below are
graded fresh against that final code, not patched onto the old count.

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

**447 mutants total, 418 killed, 29 survived, as graded on this repo's test
venv (Python 3.13): 93.51% (418/447).** Of the 29 survivors:

- **7 are non-contract diagnostic prose** (an `ExecutionError` /
  `AssertionError` message changed while the `.code` / branch stayed put).
- **19 are genuine equivalents** (no test, however written, could distinguish
  the mutant from the real code on ANY supported interpreter).
- **3 are killed on Python 3.10** (this repo's `requires-python` floor) **but
  not on the 3.13 venv this grade ran on** -- verified directly (see "Killed
  only on the Python 3.10 floor" below), NOT genuine equivalents. Counting
  these as killed (the honest floor-interpreter grade) gives **421/447 =
  94.18%**. The 93.51% figure above is the number this specific grading run
  produced and is reported as such rather than blended with the 3.10 proof;
  neither figure is inflated by mislabeling.

## Killed by new tests (test-only; no source logic changed)

The prior population had 118 survivors across these two files before the first
mutation pass; that gap was closed by extending the two slice-2 unit/parity
files, described in the "byte-exact characterization", "PreflightResult
contract", "RouteAdmission contract", "`_strategy_by_column` / `_find_table`",
and "`run_widened_execution` wiring" paragraphs below. This revision adds four
more kills, closing the four mislabeled-equivalent gap the cross-model gate
found:

**`table_kinds` / `explain_plan` / `execution_plan_decision` nulled in
`run_widened_execution`.** These three parameters flow straight into the
committed `ExecutionResult` (`table_kinds=table_kinds` on the result, and
`quality_metrics["execution_plan"]` when `explain_plan and
execution_plan_decision is not None`), so nulling any of them is externally
observable -- the earlier "the native lane admits only a single non-foreign
mask table" / "falsy, same as the default" reasoning conflated "the current
tests don't check it" with "no test could". `test_widened_admit_threads_
table_kinds_and_execution_plan_telemetry` (in the parity file) drives a
widened admit with `explain_plan=True` and asserts `result.table_kinds ==
{_TABLE: "mask"}` and `result.quality_metrics["execution_plan"]["mode"] ==
"pandas_fallback"` (plus `reason` and `rejections`), killing the three
argument-nulled mutants on `run_widened_execution`'s call into
`_run_native_streaming`.

**UTF-8 null-fill payload replaced with `None` in the digest codec.** The
codec forces a null utf8 slot's payload to zero-length (`pc.fill_null(array,
"")`) before hashing, precisely so a null contributes nothing beyond its
already-hashed validity byte. Mutating the fill value to `None` makes
`pc.fill_null` a no-op (`pc.fill_null(arr, None)` returns `arr` unchanged,
confirmed directly against pyarrow 24), so the RAW offsets/data pass through
instead of being normalized -- observable only when a null slot's underlying
buffer holds a nonzero-length garbage span, which Arrow's spec permits (only
the validity bitmap is authoritative; a null slot's offsets/data are
otherwise unconstrained) and which pyarrow's own array builder never happens
to produce, so no pre-existing test could see it.
`test_digest_utf8_null_payload_is_zeroed_regardless_of_underlying_garbage`
hand-builds such an array via `pa.Array.from_buffers` (validity marks index 1
null; the offsets span 2 garbage bytes `"XX"` behind it) and asserts its
digest equals a clean pyarrow-built null's digest -- true on real code (both
zero the null payload), false on the mutant (confirmed by hand-patching the
source and re-running: the two digests differ at byte 0).

**`zero_copy_only=True` on the boolean value branch.** PyArrow bit-packs
boolean arrays (1 bit per value vs. numpy's 1 byte), so
`to_numpy(zero_copy_only=True)` raises `ArrowInvalid` unconditionally for
`pa.bool_()` -- confirmed directly against pyarrow 24, null-bearing or not.
This mutant is distinct from its int/timestamp siblings (where the flag is a
true no-op, see "Digest codec, no observable byte change" below): those types
ARE zero-copy-compatible once null-filled into a contiguous buffer, but
boolean never is. `test_digest_boolean_column_to_numpy_never_zero_copy_only`
accumulates a null-free and a null-bearing bool array and asserts no
exception; hand-patching the flag to `True` on the bool branch reproduces the
`ArrowInvalid` and fails both this test and the pre-existing
`test_digest_golden_per_column_bytes` (whose `b_nb` golden entry already
exercises a null-bearing bool column, so it was already capable of catching
this -- the earlier ledger simply mis-triaged it).

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

**The P0 fix's second-read schema-drift check.** `run_widened_execution`'s new
`drift = schema_drift_reason(classification.schema, second_read_schema)` /
`if drift is not None: raise ExecutionError(code="native_chunk_schema_drift",
...)` block (added to close a Codex-final P0 on an empty widened source) is
exercised by the PRE-EXISTING
`test_empty_widened_source_schema_drift_aborts_before_commit` (5 parametrized
variants: reorder/rename/drop/add/type_change) and
`test_column_reorder_between_reads_aborts_before_commit`. Hand-flipping the
guard's `is not None` to `is None` reproduces all 5 parametrized failures
(`DID NOT RAISE ExecutionError`), confirming the branch itself is fully
covered; only the raise's `message=` string (non-contract, see below) escapes.

## Non-contract diagnostic prose (accepted-not-killed) -- 7

Each changes an observable string (part of `str(exc)`) but not the
`ExecutionError.code` or any branch; callers key off `.code` only. Some sit
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
- `run_widened_execution` 14 (message forced to `None`), 16 (message kwarg
  dropped): the NEW P0 second-read drift check's
  `code="native_chunk_schema_drift"` is killed by the 5-variant
  `test_empty_widened_source_schema_drift_aborts_before_commit` and by
  `test_column_reorder_between_reads_aborts_before_commit` (both assert
  `excinfo.value.code`, never the message text).

## Genuine equivalents -- 19

No test, however written, could distinguish these from the real code, on any
supported interpreter. Each item below was individually re-verified against
pyarrow 24.0.0 / pandas 2.3.3 for this revision (not carried over unchecked).

**Dropped keyword equal to the dataclass default (3)** -- `classify_and_preflight`
20/37/73 each drop `admitted=False` at a reroute return; `RouteAdmission.admitted`
defaults to `False`, so the value is unchanged.

**`run_widened_execution` pass-throughs the callee ignores or that are inert in
this lane (3)** -- 1 (`or` -> `and` on the `# pragma: no cover` schema/digest
guard: both operands are always False in every real invocation, since a caller
only reaches this function with `classification.admitted` already True, which
requires schema and digest to be set together -- unreachable either way), 19
(`first = next(execution_batches, None)` -> `first = None`: the generator is
never advanced, so `_rechain(None, rest)` streams the FULL untouched `rest`
iterator -- still every batch, in order), 31 (`plan=None`: `_run_native_
streaming`'s first line is `del plan`, unused after that).

**Digest codec, no observable byte change (13):**

- `_update_hashers_for_array` 1 (`no_nulls = None`): a null-free batch takes the
  else path, whose `is_valid` / `fill_null` results equal the all-true validity
  string and the unchanged array (the module docstring notes exactly this;
  re-verified: `pc.is_valid` on a null-free array returns an all-True boolean
  array whose `.tobytes()` equals `b"\x01" * len(array)`).
- `_update_hashers_for_array` 10/62/72/86 (`zero_copy_only` set to `None`):
  `None` is falsy, identical to the real `False` at the C level (verified
  directly: `arr.to_numpy(zero_copy_only=None)` behaves exactly like
  `zero_copy_only=False` for a validity array, a bool array, an int array, and
  a timestamp-cast-to-int64 array -- no raise, same bytes).
- `_update_hashers_for_array` 73/88 (`zero_copy_only` set to `True` on the
  INTEGER/TIMESTAMP branches, not boolean): re-verified directly against
  pyarrow 24 -- a fixed-width, null-filled (hence contiguous, offset-0) int64
  or timestamp-cast-to-int64 array IS always zero-copy convertible, so the
  flag never raises and produces byte-identical output. This is the boolean
  mutant's mirror-image case, not the same claim: boolean is bit-packed and
  NEVER zero-copyable (see the newly-killed mutant above), while fixed-width
  numeric types always are.
- `_update_hashers_for_array` 81 (`pa.scalar(0, type=None)`) and 83
  (`pa.scalar(0, )`): re-verified directly -- `pc.fill_null` auto-casts an
  untyped scalar `0` to the target array's own type (timestamp included), so
  the filled bytes are identical to the explicitly-typed scalar in both cases.
- `_update_hashers_for_array` 49 (`and` -> `or`) and 51 (`>` -> `>=`) on the
  `data_buf is not None and end > start` value-slice guard: the only cases the
  branch flips on (`data_buf is None`, or `end == start`) update the hasher
  with a zero-length slice, identical to skipping it (`memoryview(buf)[a:a]`
  is always `b""`).
- `_finalize_column_digest` 5/19 (`encode("UTF-8")` vs `"utf-8"`): Python
  normalizes codec names case-insensitively (re-verified: identical output for
  both ASCII and non-ASCII names).

## Killed only on the Python 3.10 floor -- 3 (NOT genuine equivalents)

`_finalize_column_digest` 10/24/33 each drop the `"big"` argument from an
`int.to_bytes(...)` call (the name-length, type-token-length, and row-count
framing prefixes). The prior ledger filed these as equivalents ("`int.to_bytes`
defaults `byteorder="big"` on Python 3.11+, so the bytes are identical") --
true as far as it goes, but it silently assumed the grading interpreter IS
3.11+. This repo's `pyproject.toml` declares `requires-python = ">=3.10"`, and
on 3.10 `int.to_bytes` still REQUIRES `byteorder` positionally; omitting it
raises `TypeError: to_bytes() missing required argument 'byteorder' (pos 2)`
(confirmed directly: `python3.10 -c '(5).to_bytes(4)'`). That is a real,
distinguishing failure a test on the floor interpreter observes -- these are
not "no test could ever tell the difference" equivalents.

Verified directly against the FINAL mutant tree (not simulated): the
`mutants/` output from this grading run, re-executed under a Python 3.10
interpreter with the SAME `test_digest_golden_per_column_bytes` /
`test_digest_golden_combined_bytes` selection tq_mutate already uses, fails
both tests with exactly that `TypeError` for all three mutant keys
(`x__finalize_column_digest__mutmut_10/24/33`) -- a genuine kill, not a harness
break (`rc=1`, a real test failure). On THIS repo's test venv (3.13, where
`int.to_bytes` defaults to `byteorder="big"` since the 3.11 language change),
the produced bytes are provably identical with or without the argument
(re-verified: `(300).to_bytes(2) == (300).to_bytes(2, "big")`), so no
assertion on this interpreter can distinguish them -- the golden test already
pins the byte value and still cannot catch it here, and no new test can either.

These 3 are reported as SURVIVED in the 93.51% headline number (that is what
this grading run, on this venv, actually produced), but are listed here
separately from "genuine equivalents" because they are not: grading the same
selection on Python 3.10 kills them. 418 + 3 = 421 killed of 447 = **94.18%**
is the honest floor-interpreter figure; neither number is inflated by
mislabeling the other.

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
