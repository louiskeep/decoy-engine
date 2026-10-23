# Engine correctness: §WHEN-NUMERIC crash + confirm §MEM-EST-DTYPES closed

Status: BUILT 2026-10-22 (branch `claude/engine-correctness-when-numeric`).
§WHEN-NUMERIC: new `check_when_string_output_on_numeric_unsupported` compile
check + registration + `checks_passed`/`checks_skipped` wiring; 23 new unit tests;
the three pinned `checks_passed` contract tests updated for the new entry.
§MEM-EST-DTYPES: one explicit e2e regression confirms it closed. All plan +
byte-estimate suites green (the sole failure, `test_c8_i_when_grammar`'s
reserved-name list, is a pre-existing numexpr-version artifact that reproduces
on origin/main). ruff + mypy clean. Dev loop, no cross-model review (owner
instruction). Follow-ups: update the decoy-platform pre-audit-triage doc to mark
§MEM-EST-DTYPES confirmed-closed; other string-output strategies (fpe/text/
categorical) under when: on numeric if shown to crash. Roadmap reconcile at merge.
Date: 2026-10-22
Risk: R2 (adds a fail-closed compile-time validation that changes which configs
are accepted; touches masking semantics)

Rules consulted: 00-universal.md, development-loop.md, bugfix.md, feature-dev.md,
testing.md, verification.md, security.md, api-and-compatibility.md (pre-GA hard
reject, no shim).

This slice covers two items from the pre-release engine-correctness cluster
(decoy-platform `docs/backlog/pre-audit-triage-2026-10-20.md`): a live HIGH
crasher (§WHEN-NUMERIC) and confirming a named crasher already closed
(§MEM-EST-DTYPES, the triage asked for "a 1-test repro first" before closing).

## §WHEN-NUMERIC (the fix)

### Problem (reproduced on main)

A `when:` row gate plus a string-output masking strategy on a numeric (int or
float) column produces a column where matched rows hold a string and unmatched
rows hold their numbers; one Arrow column cannot hold both, so the pandas route
crashes at Arrow reconstruction. Reproduced on `origin/main`:

- `redact` + `when:` on int64 / float64 → raw `pyarrow.ArrowInvalid`
  ("Could not convert 'X' with type str … Conversion failed for column id with
  type object"), plus a pandas incompatible-dtype FutureWarning. **This is the
  live crash.**
- `truncate` + `when:` on numeric → string output, same class of crash (its
  output is unconditionally string).
- `hash` is NOT in scope: it already raises clean coded errors
  (`gated_hash_on_integer_unsupported` for int, `float_canonicalization_unsupported`
  for float), so it does not raw-crash. Left as-is.

### Definition of done

A config with a `when:` gate and a string-output strategy (`redact` with a
string replacement, or `truncate`) on a numeric source column is rejected at
plan-compile with a clear coded `PlanCompileError`
(`when_string_output_on_numeric_unsupported`) naming the table/column/strategy,
instead of crashing mid-run. No valid config is newly rejected:

- `redact` with a NUMERIC replacement (e.g. `redact_with: 0`) stays numeric-typed
  and does not crash → NOT rejected.
- a non-numeric (string/etc.) source column → NOT rejected (the mixed-type
  problem is specific to a numeric source).
- no `when:` → NOT rejected (whole-column masking changes the column's type
  uniformly, which is fine).
- `hash` → NOT touched (its own guards stand).

### Design

Profile-aware compile check `check_when_string_output_on_numeric_unsupported(
config, profile)` in `plan/_checks.py`, modelled on
`check_null_bearing_int_unsupported`: build a `(table, column) -> is_numeric`
lookup from the profile (`_is_integer_dtype` OR a new `_is_float_dtype`), then
reject a config column with a non-empty `when:` whose resolved output is
unconditionally string on a numeric source. Registered in `_compile.py`'s
profile-aware branch next to the null-bearing-int check; profile-dependent, so
it is `checks_skipped` under `no_profile=True` (same as its sibling), and the
existing execution-time behavior backs that case up. Output-string resolution:
`truncate` always; `redact` when its replacement (`redact_with`, default
"REDACTED") is a string.

### Acceptance tests

- redact(+string replacement)+when on int64 and float64 → `PlanCompileError
  when_string_output_on_numeric_unsupported` at compile (not a run crash).
- truncate+when on int64/float64 → rejected.
- redact with a numeric `redact_with` + when on numeric → ALLOWED (no crash, runs).
- redact/truncate+when on a STRING column → ALLOWED.
- redact/truncate with NO when on a numeric column → ALLOWED.
- hash+when on numeric → still its own existing coded error (unchanged).
- fpe, categorical (string categories), text_redact, text_mask, code_set, bucket_perturb
  +when on numeric → rejected; numeric-category categorical and numeric-token
  text_redact → allowed. A gated faker string provider or bucketize on numeric with a partial gate → execution
  error `gated_string_output_on_numeric_unsupported`; a numeric-output gated strategy is
  not guarded.
- `no_profile=True` → the check is skipped (listed in checks_skipped).

### Non-goals

The first cut of this check named only truncate and redact, on the belief that
the other string-output strategies "have their own type constraints". That was
wrong: a gated `faker` string provider and a gated `categorical` with string
categories on an int64 column both raise the same `pyarrow.ArrowInvalid`
(reproduced), as do fpe, text_redact, text_mask, code_set and bucket_perturb. The
shipped check therefore covers every statically-typed string-output strategy at
compile, resolved through the engine's `masked_output_type` rather than a second
list, so numeric-output configs (numeric categorical categories, a non-string
`text_redact` token, a numeric `redact_with`) are not rejected. `faker` cannot be
typed at compile (a provider's return type is only known by running it) and `bucketize`
emits range labels that `masked_output_type` does not type, so those, and any other
pandas-only string emitter, are caught at execution instead. One guard in
`run_with_when_gate` (`execution/_gated_string_guard.py`) checks every gated write: it
raises `gated_string_output_on_numeric_unsupported` when the source column is numeric, the
gated subset's output is string, and the gate leaves non-null rows unmasked. A gate that
selects every non-null row turns the column uniformly to strings, which works; the target
is cast to object explicitly first. `hash` keeps its own execution guards. Out by design:
`date_shift` and `geo_generalize` (not typed by `masked_output_type`; the execution guard
still catches them if they emit strings on a numeric column) and strategies whose output
stays numeric.
The "cast unmatched to string" alternative (roadmap
option 1) is deliberately not taken: reject-at-validation is the triage's choice
and avoids silently restyping a numeric column.

## §MEM-EST-DTYPES (confirm closed)

The named example in the roadmap. The byte-estimate router was hardened by engine
#189 (`_mem_estimate_arrow.classify_column`): a dictionary/categorical column is
`Unpriceable` (routes bounded) and a tz-aware timestamp prices as
`datetime64[ns]` (8 bytes), so neither crashes. Existing coverage is extensive
(`test_byte_estimate_arrow_types_e2e.py`, `test_mem_estimate_arrow_types.py`:
472 passing). This slice adds ONE explicit, named end-to-end regression —
category + tz columns together through `run_pipeline` with byte-estimate ON,
asserting no crash and parity with byte-estimate OFF — as the repro-of-record the
triage required before the finding can close.

Note: built under the dev loop with NO cross-model review per owner instruction
(2026-10-22); REVIEW is a self-review against this plan.
