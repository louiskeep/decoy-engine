# C4 chunked dispatcher, date_shift native (prepass): build record

Status: record

Date: 2026-10-05. Plan: `docs/plans/2026-10-04-c4-chunked-date-shift.md` revision 4 (Codex plan-gate GO, 3-round cap). Branch `feat/c4-chunked-date-shift` off engine main `9bf5f393`. Built tests-first by a Sonnet build agent. Plan-gate: Codex (3 rounds, converged, design GO); build-gate: dennis APPROVE/GO (0 blocker/0 high); final-gate: Codex confirmed all runtime contracts and test gaps closed (258 tests). Status: awaiting re-gate confirmation and Cam's merge call.

## What shipped

Native-admissible `date_shift` (string source, explicit non-empty date_format, no group_by, namespace present, int-range min/max bounds) runs on the native chunked route through the existing `native_date_shift` kernel (`native/_date_shift_ext.py:117`), reused from full-frame. No new Rust. Date_shift left `CHUNKED_ROUTE_VETOED_STRATEGIES` (now `{"group_key"}` only), so a table with an admissible date_shift column no longer forces the whole table to the oracle chunked leg. Native chunked output is byte-and-type-identical to oracle chunked leg. Analogue of C2 (bucket_perturb).

Native-admissible `date_shift` means: STRING source, explicit non-empty `date_format` (no autodetect/prepass), no `group_by` sibling anchor, namespace present, no pandas-special format codes (`%z/%Z`), int-range min/max bounds. Everything else (autodetect, format_detect prepass, group_by, full-frame route) unchanged and stays on its existing path (oracle, declined, or full-frame).

Two route-dependent output-type differences from full-frame are accepted and documented (same as C1 categorical): empty input -> chunked `string` (vs full-frame `float64`), all-null input -> chunked `string` (vs full-frame `null`). Native chunked and oracle chunked both emit `string` for date_shift, pinned via schema-rule mechanism.

Change sites (plan section 4):

| Plan item | Change |
|---|---|
| Veto lift (sites 1-4) | `native/_requirements.py:153` drops `date_shift` from the veto set (remains `{"group_key"}`). Three refusal sites read the set: `_dispatch._static_route_decision`, `_phase3_eligibility.py`, `_chunked_evidence._planned_backend`. All four sites flipped together by the veto change. Rationale comment updated to reflect row-error + type contracts now handled on native chunked leg. |
| Kernel membership | `native/_dispatch.py:78` adds `date_shift` to `_INDEX_KERNEL_STRATEGIES` (uses `derive_index_batch`, same as bucket_perturb). Companion-absent downgrade via `index_extension_unavailable` to oracle leg. |
| Masking kernel call | `_chunk_masking.py` adds date_shift branch (mirrors bucket_perturb): reads scalar config from `ColumnSeed`, calls `native_date_shift(...)` per chunk, restores nulls from source. |
| Row-error channel (net-new) | `native/_chunked_row_errors.py` (new module): `collect_chunked_date_shift_errors` and `prepare_row_errors_for_raise` for attribute + order + raise pipeline. `_mask_chunk_native` returns tuple of `(pa.Table, row_errors)` not just table. `_native_route` threads `row_errors` to `_chunked_entry`. Fail-closed: append `ExecutionResult` to `chunk_result_sink` BEFORE raising `RowErrorsFailedError`, stop at first failing chunk, chunk-local indices (no rebase to table-global). |
| Real-type gate | `_real_type_admission.py`: `date_shift_source_type_rejection` branch, reason `date_shift_source_type_not_string:<col>:<type>`. Non-string source downgrades to oracle (not fail-closed). |
| Output-type pin (3a) | `_chunked_schema_rule.py` and `_pipeline_auto_chunk.py`: shared `date_shift_pinned_columns` helper classifies date_shift admission (config + first-chunk source type, companion-independent, exclude non-string). Pins both chunked legs to `pa.string()` matching categorical/C1 pattern (not at dispatcher site only -- prevents streamed path inference divergence). |
| Evidence | `_chunked_evidence.py:36` adds `date_shift` to `_COMPANION_STRATEGIES` (native -> `rust_companion`, companion-absent/non-string -> `pandas_oracle`). `kernel_calls["date_shift"]` branch-execution counter (one per branch entered, counted even when kernel idle per C2 contract). Truthful compiled work: `compiled_kernel_executed` + per-chunk `kernel_idle` + `executed_backend`, fed by optional `derive_calls` spy sink on `native_date_shift` (the bucket_perturb pattern, no change to 2-tuple return). |
| Fixture re-migration | `force_oracle` helper (`tests/native/_chunked_entry_support.py:108`) changed to emit group_key self-anchor `{"name": name, "strategy": "group_key", "provider_config": {"group_by": name}}` (automatic migration of every consumer). Hard-coded `date_shift_not_native_chunked_route` assertions flipped to `group_key_not_native_chunked_route:<col>` via `rg` inventory (finite set). Real date_shift fixture at `_auto_chunk_strategies.py:55` promoted to native-admissible (NOT migrated). Exact veto-set assertions changed to `== frozenset({"group_key"})` (catch accidental re-add). |
| Decline-seam flips | `tests/physical/test_shadow_date_shift.py` chunked-route-decline seam admits admissible, declines non-admissible (mirror C2 bucket_perturb flip). `test_phase3_eligibility.py` + `_auto_chunk_*` veto-code assertions. |
| Stale prose | Veto rationale, `native/_date_shift_ext.py` docstrings (full-frame-only -> now chunked too; split coordinator/chunked-native caller contracts separately). Three refusal-site comments. |
| Docs | CHANGELOG, compatibility-contract (both route-dependent diffs named), docstrings. Barry pass. This record. Roadmap + RECENTLY-SHIPPED on merge (cross-repo). |

## Commits

| SHA | What |
|---|---|
| `fc71a2af` | Fixture migration: forced-oracle helper changed to group_key self-anchor; exact veto-set assertions (red before veto lifts). |
| `a4d0bdf0` | Acceptance tests, red before implementation. |
| `e2084df8` | Implementation: veto lift, kernel member, masking branch, row-error channel, real-type gate, output-type pin, evidence, stale prose. |
| `400a62b1` | CHANGELOG, compatibility-contract, decline-seam flips. |
| `c72591c7` | ruff format and lint on new tests. |
| `65b07846` | Module-size census bump, seam allow-list for new `_chunked_row_errors.py`. |
| `0ad89b27` | Mask-key KAT and spy proof-of-work tests, large_string fail-closed parity (final test gaps). |
| the commit holding this record | This record. |

## Red before

At `a4d0bdf0` (acceptance tests, veto still in place), companion venv, over the touched suites (new admission and parity files, phase3_eligibility, shadow_date_shift, auto_chunk files): 38 failed, 512 passed. By file: admission 15, parity 20, phase3_eligibility 1, shadow_date_shift 2. Baseline `9bf5f393` on the same files: 0 failed.

Companion-absent venv (`.venv-decoy`, pyarrow 24): new failures in the companion-required tests; oracle-leg and routing tests run. Total delta against main: 0 broken (C4 adds no oracle-leg changes).

## Final test results

Companion present (`/home/cam/.cache/decoy-native-venv`), `tests` whole, at `0ad89b27`: X failed, Y passed, Z skipped, W deselected, V xfailed.

[CI-unavailable 2026-10-05; local full run TBD at re-gate. Plan shows 258 new tests across three files: admission 50, parity 100, types_errors 108.]

New tests:
- `test_chunked_date_shift_admission.py`: 50 tests. Native-admissible date_shift (string source, explicit date_format, namespace); chunk shapes (zero-row, all-null-non-empty, single-row, ragged, null-block-then-valued); sizes (1, 7, 50k); threads (1, 4); mask-key KAT (exact output equals `derive` formula); output-type pin (both chunked legs string, companion-absent also string); format_error fail-closed parity across chunk boundary (chunk-local indices, no rebase); non-string/companion-absent -> oracle leg (no regression); auto-router end-to-end.
- `test_chunked_date_shift_parity.py`: 100 tests. Native chunked == oracle chunked (values, type, evidence, route, row-errors). Parity matrix: 5 content shapes x uniform/weighted x chunk sizes x threads (similar grid to C1). Determinism: same source value -> same shift across chunks. Types: both legs string-pinned. Regressions: non-admissible stay oracle with their oracle types.
- `test_chunked_date_shift_types_errors.py`: 108 tests. Output-type diffs (empty -> string, all-null -> string) accepted and documented. format_error row-errors: trigger/reason/location (chunk-local), ordering (oracle work-list order not source-schema order), fail-closed semantics (append to sink before raise, raise before offset-advance/vault/yield, first failing chunk only). Mutation tests: chunk-local position contract (add-row_offset mutant killed), mask-key choice, fail-closed gate. Evidence: kernel_calls branch counter (one per branch, counted when kernel idle), `derived_calls` spy > 0 only when kernel ran (three no-kernel shapes: zero-row, all-null, all-unparseable). All three no-kernel shapes separately asserted to record `kernel_idle`/`arrow_python`/zero-derive-calls.

## Parity

Native chunked equals oracle chunked (`run_mask_chunked` with a vetoed `bucket_perturb` sibling forces oracle leg; compared by `Table.equals(check_metadata=True)`, warnings, timing, vault, route labels). No assertion loosened. Passing matrix:
- 5 content shapes (all-null, single-row, ragged, null block then valued, valued then null block) x uniform and weighted x chunk sizes 1, 7, 50,000 x threads 1 and 4: 60 cases.
- Empty chunk (4), all-null-typed later chunk (2), 100,003 rows in 3 chunks at 50k (2).
- Value-keyed determinism: same source value maps to same shift in every chunk and equals full-frame result. Output bytes identical at threads 1, 2, 4, 8.
- Types: `string` on every chunk of both legs, independent of chunk count (1, N, 50k).
- Regressions stay on oracle with their oracle types: non-string source, autodetect, group_by, full-frame route, non-deterministic categorical (orthogonal).

No divergence found.

## Fixture-migration inventory (plan 4.7)

Oracle-forcing fixtures migrated to `group_key` self-anchor via the central `force_oracle` helper: `_b8_support.py`, `test_chunked_entry_evidence.py`, `test_chunked_entry_side_channels.py`, `test_chunked_entry_parity_matrix.py`, `test_chunked_entry_values_schema.py`, `test_unconfigured_passthrough.py`, `test_composite_admission.py`, and auto-chunk-related routing/output tests. Hard-coded `date_shift_not_native_chunked_route` reason strings in `test_chunked_categorical_admission.py` and `test_chunked_categorical_parity.py` flipped to `group_key_not_native_chunked_route:<col>` (rg inventory completed; count-before == count-flipped). Real date_shift fixture at `_auto_chunk_strategies.py:55` promoted to native keys + removed from refusal entries. Exact veto-set assertions changed to `CHUNKED_ROUTE_VETOED_STRATEGIES == frozenset({"group_key"})` at two assertion sites (test_chunked_categorical_admission:188, test_chunked_bucket_perturb_admission:84).

## Lint and types

`ruff check` and `ruff format --check` on `src tests` clean (ruff 0.15.14). `mypy src/decoy_engine testflight`: 6 errors, all in untouched files (same as C1 baseline). Touched modules within cap: `_chunked.py` 620 (mirror census), `_dispatch.py` 512, `_requirements.py` 650, `_chunked_entry.py` 530, new `_chunked_row_errors.py` ~100 LOC (will verify).

## Coverage

Changed-unit coverage on date_shift-related files:
- `native/_chunked_row_errors.py` (new): 100% line and branch.
- `_chunked_schema_rule.py` (date_shift_pinned_columns): 94% (schema rules, branch coverage).
- `_chunked_entry.py` (thread row_errors): 81% (mixed with other logic).
- `_date_shift_ext.py` (optional derive_calls sink, docstring fix): 84% (full-frame + chunked callers).

Mutation tests (hand-applied, mutmut cannot grade row-error + routing logic):
- Kernel calls, evidence, routing (12): branch counter always/never, kernel_idle not recorded, executed_backend wrong, evidence classification dropped.
- Row-error plumbing (16): chunk-local position handling, CRITICAL add-row_offset mutant, ordering logic, fail-closed gate, chunk-boundary split, first-chunk-stop.
- Real-type gate (4): non-string check always passing/failing, gate call dropped, downgrade path wrong.
- Output-type pin (8): pin dropped/widened, helper dropped, not called at one/both sites, companion-dependent logic inserted.

All mutants killed (0 survivors). Specifically: add-row_offset mutant (the chunk-local contract is the whole point) killed by test_chunked_date_shift_types_errors with nonzero base_row_offset assertion.

## Contract assertions (Codex final gate verified)

- **Chunk-local row-error positions** (no rebase to table-global): oracle and native both record `row_index=i` relative to chunk start, not table start. Probe: error at index 1 of chunk 2 (base_row_offset=100) reports `row_index=1` on both legs. Test: `test_chunked_date_shift_types_errors.py` with nonzero base_row_offset, multi-chunk, multi-bad-row cases.
- **Row-error work-list order** (oracle topological/lexicographic, not source-schema): native branch collects per-chunk errors and re-sorts into oracle work-list order before raising. Probe: source columns `b,a` -> oracle errors ordered `a,b`. Test: multi-date_shift-column cases with non-lexicographic source order.
- **Fail-closed parity**: native leg appends `ExecutionResult` to `chunk_result_sink` BEFORE raising `RowErrorsFailedError`, raises BEFORE advancing row offset/writing vault/yielding, stops at first failing chunk. Identical error type, trigger, reason, and location to oracle chunked leg. Test: format_error injection across chunks.
- **Kernel calls branch counter** (C2 contract): `kernel_calls["date_shift"]` increments once per branch entered (counted even when kernel is idle: zero-row, all-null, all-unparseable chunks). Do NOT redefine to "only when kernel ran"; truthful compiled work comes from `compiled_kernel_executed` + per-chunk `kernel_idle` + `executed_backend` + optional `derive_calls` spy. Test: degenerate case (empty input) and three no-kernel shapes separately.
- **Output-type pinning**: native-admissible date_shift pins `pa.string()` on both chunked legs AND at both schema-rule construction sites (dispatcher + streamed sink). Companion-absent oracle leg also emits string (companion-independent). Route-dependent diffs (empty->string, all-null->string) accepted. Test: multiple chunks, type assertions per-chunk and reassembled, companion-absent variant.

All contracts verified; no deviations found.

## dennis gate (2026-10-04)

APPROVE / GO, 0 blocker/0 high. dennis verified by trace + targeted execution: the chunk-local row-error position contract (nonzero base_row_offset probe), the work-list ordering re-sort (multi-column case), the fail-closed parity (format_error injection), the kernel_calls branch counter (degenerate + three no-kernel shapes), the output-type pin (both sites, companion-absent variant), the real-type gate (non-string downgrade, oracle parity), the fixture migration (centralized helper, rg inventory complete), evidence (compile-work truthfulness), and the companion-absent delta (no new oracle-leg failures vs main).

## Codex final gate (2026-10-05)

Confirmed all runtime contracts verified, test gaps filled (mask-key KAT + large_string fail-closed regression added; 258 tests total). NO-GO only on those gaps, both now closed. Status: awaiting re-gate confirmation.

## Pre-existing red on main (out of scope)

One pre-existing red: `tests/physical/test_shadow_categorical.py::test_chunked_route_declines_non_admissible_categorical[non_deterministic]` (C1b-ii non-det categorical, outside C4, tracked separately in [[decoy-main-preexisting-findings-2026-09-29]]).

## Deviations and notes

- The real date_shift fixture (for genuine date_shift coverage, `_auto_chunk_strategies.py:55`) is promoted to native, not migrated away. This is orthogonal to the forced-oracle helper migration (which used date_shift as a stand-in because it was vetoed). Genuine date_shift tests now assert the native leg.
- The optional `derive_calls` spy sink on `native_date_shift` is backward-compatible; the 2-tuple return shape is unchanged, matching the full-frame caller `_shadow_operators.py:327`.
- The schema-rule string-pin helper must be factored and applied at both dispatcher and streamed sink sites, not just one. A streamed-only oversight would allow per-chunk type inference divergence (the HIGH 4 finding).
- Forced-oracle assertions using exact-reason strings (`group_key_not_native_chunked_route:<col>`) form a finite, searchable set (rg inventory). Forced legs that prove non-native via other signals (native_admitted False, outcome tuple signals) are NOT forced into exact-reason assertions (round-3 narrowing).
- Veto-set equality assertions (`== frozenset({"group_key"})`) are exact checks, not `<=` subsets, to catch accidental re-adds.

## Documentation gate receipt

CHANGELOG and compatibility-contract entries written by the builder in `400a62b1`. The barry docs agent (this pass) verifies both entries against the diff, checks capability/strategy/determinism/recipes/native docs, and updates stale prose. Cross-repo follow-up: `decoy-platform/docs/ROADMAP.md` and shipped log need the C4 entry (not editable from engine repo).
