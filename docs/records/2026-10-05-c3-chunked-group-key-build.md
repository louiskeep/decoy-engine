# C3 chunked dispatcher, group_key native: build record

Status: record

Date: 2026-10-05. Plan: `docs/plans/2026-10-05-c3-chunked-group-key.md` revision 5 (Codex plan-gate rounds 1 to 3, Fable confirming gate GO). Branch `feat/c3-chunked-group-key` off engine main `b0bcfa74`. Built tests-first. Awaiting the dennis and Codex build gates and Cam's merge call.

## What shipped

A native-admissible `group_key` runs on the native chunked route through the existing `native_group_key` kernel, reused from full-frame. No new Rust. The chunked veto set (`CHUNKED_ROUTE_VETOED_STRATEGIES`) is empty. Native chunked output is byte-and-type-identical to the oracle chunked leg.

Native-admissible means: `group_by` names a sibling column the first chunk carries as `string`, `int64` or `bool`, no other node masks that sibling, no `when:`, not an FK key. That domain is the one where the oracle's raw-value cache cannot collide, so the result does not depend on chunk boundaries. Everything else stays on the oracle leg, reproducibly: a wider sibling, a masked sibling, a self-anchor, a missing or broken raw-hex kernel.

## Change set (plan section 4)

| Plan item | Change |
|---|---|
| 1. Veto lift | `_requirements.py`: the set is `frozenset()`. The three refusal sites stay as defensive consumers, pinned by tests that monkeypatch a non-empty set. |
| 2. Raw-hex preflight | `_dispatch.py`: `NativePreflight.raw_hex_kernel`, a third probe after crypto and index, one code `raw_hex_extension_unavailable` for all four loader failures, threaded `_run_chunked` to `_native_route` to `_mask_chunk_native`. The bare early return carries the field. `native_group_key` gains `derive_calls` and resolves the kernel before the empty short-circuit. |
| Shared caller | `physical/_shadow_operators.py` passes `derive_calls` and sets `compiled_kernel_executed` only when a row was derived. Full-frame empty is `False`, populated `True`, and empty with the companion absent still declines `native_companion_unavailable`. |
| 3. Masking branch | `_chunk_masking.py`: `_mask_group_key`, reading `raw_chunk.select([group_by])`, synthesized namespace `group_key/<target>`, `str(prefix)`. `_native_route` iterates the raw chunk, derives the cast chunk with the guard, passes both. Every other branch, `normalize_chunk` and `_native_chunk_result` keep the cast chunk. |
| 4. Order gate | New `_chunked_group_key_gate.py`: masked sibling `group_key_masked_sibling_not_native_chunked_route:<target>:<sibling>`, self-anchor `group_key_self_anchor_not_native_chunked_route:<target>`. Called from `_static_route_decision`. |
| 5. Real-type gate | `_real_type_admission.py`: `group_key_sibling_type_rejection`, reasons `group_key_sibling_type_not_native:<col>:<group_by>:<type>` and `group_key_sibling_missing_not_native_chunked_route:<col>:<group_by>` (a clean decline, no KeyError). |
| 6. Output-type pin | `_chunked_schema_rule.py`: `group_key_pinned_columns`, keyed on the SIBLING type, called inside `build_schema_rule` so both construction sites get it. |
| 7. Evidence | `_chunked_evidence.py`: `group_key` joins `_COMPANION_STRATEGIES`; branch counter counted when idle; `derive_calls` gives the truthful compiled signal. |
| 8, 9. Fixtures, seams | One `force_oracle` helper repointed to the numeric-categories categorical stand-in (with `deterministic` and a namespace); `_b8_support.py` pins `FORCE_REASON`. Three veto-set assertions equal `frozenset()`. |
| 10. Stale prose | Veto rationale, `_chunked_group_key.py` docstring (per-row purity scoped to the collision-free domain), refusal-site comments, `_group_key_kernel.py` scope note. |
| 11. Docs | CHANGELOG, compatibility-contract, this record. |

## Two additions the plan did not list

1. **Resident sibling type in the static plan.** `compile_native_plan` gains an optional `resident_sources`, and `_static_route_decision` and `plan_column_backends` pass the first chunk's group_by siblings through it (`sibling_resident_sources`). Without it the plan compiler reads the sibling's type from the profile, whose coarse label calls an `int64` column holding nulls `double`, so the static check rejected exactly the int64-with-nulls case the plan requires to run native. This is the same resident-Arrow-authoritative mechanism the full-frame route uses. A side effect: a wider sibling is usually rejected by the static plan (`fallback_policy_not_native`) before the real-type gate sees it, so the gate's type check is defense in depth, tested by itself.
2. **No new per-chunk drift check.** The plan asked for a group_key-specific check that accepts the first chunk's sibling type or a raw null chunk. The shared guard `validate_chunk_schema` already does exactly that on both legs (type drift raises `native_chunk_schema_drift`, a null-typed chunk passes), so nothing was added; a test pins both halves.

## Red before, green after

Same file set both times (`tests/native`, `tests/physical`, `tests/parity/native`, `tests/sentry`, the auto-chunk, group_key, b6 and multi-table unit files), companion venv `/home/cam/.cache/decoy-native-venv`.

| State | Result |
|---|---|
| Baseline, HEAD `56c38a45` | 9974 passed, 2 skipped, 59 xfailed, 0 failed |
| Red, tests at `a14b0057`, no implementation | 245 failed, 9990 passed |
| Green, implementation, companion present | 10255 passed, 2 skipped, 59 xfailed, 0 failed |
| Green, companion absent (`.venv`, pyarrow 24) | 7992 passed, 2253 skipped (companion-needing tests), 59 xfailed, 0 failed |

`rg "group_key_not_native_chunked_route" tests`: 21 hits in 16 files before, 0 after. The new files hold 61 admission and 208 parity tests; `test_shadow_group_key.py` gained 9.

## Fixture classification (16 files)

Forced-oracle fixtures moved to the categorical stand-in: `_chunked_entry_support.py`, `test_chunked_bucket_perturb_admission.py`, `test_chunked_bucket_perturb_parity.py`, `test_chunked_categorical_admission.py`, `test_chunked_categorical_parity.py`, `test_chunked_date_shift_admission.py`, `test_chunked_date_shift_types_errors.py`, `test_chunked_entry_evidence.py`, `test_chunked_entry_side_channels.py`, `test_chunked_entry_values_schema.py`, `test_chunked_nondet_categorical_admission.py`, `test_auto_chunk_dispatcher.py`, `test_auto_chunk_routing.py`.

Genuine group_key coverage flipped: `test_phase3_eligibility.py` (admitted), `tests/physical/test_shadow_group_key.py` (chunked route admits, masked sibling declines), `_auto_chunk_strategies.py` (the group_key fixture is now a native key, companion-needing, string-output, planned `rust_companion`). Also adjusted: `test_chunked_entry_rev9_read.py` (the native leg never converts the sibling through the pandas adapter), `test_auto_chunk_output_contract.py` (companion-absent reason is `raw_hex_extension_unavailable`), the module-size census and the physical-seam allow-list.

## Mutation (hand-applied, 22 mutants, all killed)

Order gate: masked sibling admitted, self-anchor admitted. Sibling input: target column instead of the sibling, cast chunk instead of the raw chunk, raw chunk not threaded. Derivation inputs: `col_seed.namespace`, wrong mask key, raw (non-`str`) prefix. Kernel contract: short-circuit before the kernel load, derive spy never recorded, kernel not threaded, probe skipped, wrong downgrade reason. Evidence: always `rust_companion`, idle never recorded, companion list without group_key, full-frame always compiled. Gates: real-type gate bypassed, missing-sibling check removed, pin dropped, pin keyed on the target type, veto set non-empty.

## Coverage of the changed units

Every changed statement is covered. File-level line plus branch coverage under the native and physical suites: `_chunk_masking.py` 95.7%, `_chunked_entry.py` 98.8%, `_chunked_schema_rule.py` 99.3%, `_real_type_admission.py` 97.8%, `_chunked_evidence.py` 94.8%, `_dispatch.py` 98.3%, `_chunked_group_key_gate.py` 93.4% before the gate unit tests that close its two missed lines.

## Lint and types

`ruff format --check src tests` and `ruff check src tests` clean. `mypy src/decoy_engine testflight`: no issues in 511 files.
