Status: plan (rev 2, pending Codex plan re-gate)

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, testing, documentation; CLAUDE.md "use established methodology".

# C6b-i: admit text_mask to the native routes as ARROW_PYTHON

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C. Owner decision (Cam, 2026-10-10): C6b ships in two steps mirroring C6c. This is **C6b-i**: admit text_mask to both native routes as an `ARROW_PYTHON` operator so a text_mask column no longer forces its whole table onto the pandas oracle. **No Rust kernel** (that is C6b-ii). Branch `feat/c6b-i-text-mask-arrow` off engine main `af308869`. Risk: **MEDIUM** (routing/admission; no new kernel). Unlike unkeyed text_redact, **text_mask is KEYED and handler-rich**: the native wrapper must reproduce the HANDLER (mask_key provision, handler-built warnings, exception translation), not merely call `mask_cell` (Codex plan-gate round 1).

## 1. Goal and scope

Today a text_mask column has no `OperatorSpec`, so it is never native-admitted and the whole table (every sibling column) is downgraded to the pandas oracle (`_dispatch` atomic whole-table decline). C6b-i registers text_mask as an `ARROW_PYTHON` operator whose wrapper reproduces the shipped `TextMaskHandler` per cell/column (byte-identical values, warnings, errors), so the siblings stay native. Per-cell throughput is unchanged; the win is the table no longer leaving the native route.

**In scope (mirror C6c-i's admission, adapted for a KEYED, handler-rich strategy):**
- `OperatorSpec(strategy="text_mask", operator_id="native_text_mask", shape="kernel", planned_backend=ARROW_PYTHON, required_kernel=None, positive_kernel_evidence=False, unified_resident_types=_STRING_ONLY, full_frame_assembly="null_on_empty")` in `_operator_registry.py` (text_redact `:155-164` is the template; full_frame_assembly=null_on_empty is for UNIFIED assembly only, see 3e).
- A `native_text_mask` wrapper reproducing the handler, not just `mask_cell`: resolve + pass `ctx.mask_key` (text_mask is keyed: `_span_key = HMAC(mask_key, matched_text)`, fpe spans reuse the column fpe key); construct the handler's `text_mask_sub_floor_span_handled` warning + per-detector counts (built by the HANDLER, OUTSIDE `mask_cell`); translate `FpeUnencryptableError`/`FpeChecksumError` into `StrategyError(code=..., strategy="text_mask")` with column context exactly as the handler does. Parallel to `native_text_redact` in `native/_kernels_scalar.py` but with these handler obligations.
- **Warning transport (3b):** extend the shared `StepResult` + BOTH route coordinators (`physical/_shadow_coordinator.py`, chunked `_chunk_masking.py`) to carry text_mask warnings, aggregated to the oracle's per-column scope on the unified route and per-oracle-chunk on the chunked route (the C6a pattern). Declare the capability warning in `native/_capabilities.py:206`-style record AND the matching `routed_diagnostics` obligation in the `OperatorSpec` TOGETHER (else unified admission rejects once the capability is corrected).
- **Chunked string-pin (3e):** chunked output pinned to `pa.string()` via a gated chunked schema rule (as C6c-i pins text_redact on both native + oracle legs); `null_on_empty` for UNIFIED assembly only.
- `text_mask_config_rejection` declining a column per-column with `text_mask_ner_not_native:<col>` when `ner` is TRUTHY (sufficient per Codex: only the `ner` branch loads a model / supplies `extra_spans`; Tier-2 detector IDs alone do not). Do NOT copy text_redact's malformed-detector / non-string-token declines (text_mask normalizes those differently).
- String-source admission split by entrypoint (3d).

**Out of scope:** the Rust deterministic-span kernel + the Faker-synthesis/NER-span decline to Rust (C6b-ii). C6b-i's ARROW_PYTHON path runs the FULL handler in Python, incl. the faker span branch (faker overrides execute unchanged in Python), so only `ner` declines here. No change to `mask_cell`/handler behavior, detectors, or output.

## 2. Established facts (2026-10-10 reads; file:line)
- text_redact ARROW_PYTHON `OperatorSpec` `_operator_registry.py:155-164`; `native_text_redact` ARROW_PYTHON caller `native/_kernels_scalar.py:133-191`; `text_redact_config_rejection` `_operator_config_rejections.py:270` (`text_redact_ner_not_native`).
- text_mask handler `_strategies/_text_mask.py`: keyed (`ctx.mask_key`); reads `provider_config` (`detectors`, `per_detector_strategy`, `unmatched_span_policy`, `token`, `sub_floor_span`, `ner`); builds the `text_mask_sub_floor_span_handled` warning + counts OUTSIDE `mask_cell`; translates `FpeUnencryptableError`/`FpeChecksumError` -> `StrategyError(strategy="text_mask")`; `str()`-coerces non-string cells. Masking leaf = `transforms/text_mask.py::mask_cell`; faker leaf `_mask_faker`.
- `StepResult`/`run_kernel_step` (`native/_operator_step.py:237`) carry an Arrow array + errors, NOT arbitrary warnings; unified warning transport point `_shadow_coordinator.py:406` (the C6a extension site).
- Chunked contract: `native/_chunked_schema_rule.py:89` string-pin set; C6c-i pins chunked text_redact to string (`tests/native/test_c6c_i_text_redact_chunked.py:420` distinguishes chunked-string from full-frame-null).
- Chunked text_mask source guard RAISES `chunked_text_mask_source_dtype_unsupported` for numeric/dict sources (`_chunked_text_mask.py:90`); `large_string` is supported by the chunked oracle but excluded from the native domain; `_chunked_oracle.py:219`.

## 3. Decisions / method
3a. Register `text_mask` as ARROW_PYTHON (OperatorSpec in 1), declare the capability warning + routed_diagnostics obligation together; update the registry-totality + operator-tables snapshot sentries.
3b. `native_text_mask` reproduces the HANDLER: mask_key provision; handler-built sub_floor warning + per-detector counts; FpeUnencryptable/Checksum -> `StrategyError(strategy="text_mask")` with column context. Transport warnings through `StepResult` + both coordinators, oracle-column scope (unified) / oracle-chunk (chunked); warnings ride `ExecutionResult.warnings`, never the output.
3c. Config-rejection: decline TRUTHY `ner` (`text_mask_ner_not_native:<col>`); nothing else. Faker overrides run unchanged in Python on the ARROW_PYTHON path.
3d. Source admission split by entrypoint: unified/full-frame unsupported source types -> oracle fallback; manual chunked numeric/dict -> preserve the existing coded rejection `chunked_text_mask_source_dtype_unsupported`; chunked `large_string` -> decline native, match the chunked oracle; reject later schema drift (incl. a null-typed later chunk).
3e. Chunked output pinned to `pa.string()` via a gated chunked schema rule (both native + oracle legs), matching C6c-i; `null_on_empty` for UNIFIED assembly only. Acknowledge the degenerate chunked-oracle normalization this entails (do not claim all output types are unchanged).
3f. Logs/behavior otherwise unchanged.

## 4. Acceptance tests (written first; never weakened)
Differential = native lane-on (ARROW_PYTHON) vs an explicit lane-off (oracle) on the SAME route; output byte-equal incl. schema + `b"pandas"` metadata; warnings (codes/counts/fields); errors; metrics excl. timings. Admitted cases poison the oracle fallback.
1. **Byte-match both routes**, incl. the keyed branches: default config; `per_detector_strategy` routing a built-in detector to fpe/date_shift/redact AND **to faker** (the disputed branch — repeated values spanning chunk boundaries, namespace-independent span mapping); `unmatched_span_policy` variants; `sub_floor_span` present. **Mask-key provenance:** seed-derived AND secret-backed mask keys (text_mask is keyed, unlike text_redact).
2. **Table stays native:** a text_mask column + sibling hash/categorical/faker columns, BOTH routes — assert actual `native_text_mask`/`arrow_python` execution + sibling native evidence + no whole-table oracle downgrade; poison fallback.
3. **Warnings parity (3b):** `text_mask_sub_floor_span_handled` counts (per-detector + aggregate) across multiple batches, both sub_floor policies, multiple detectors, isolation between columns/runs; unified aggregates to one per-column warning, chunked per-chunk.
4. **Failure parity (3b):** a fail-closed span (e.g. ZIP `12345` default FPE, no sub_floor) raises `StrategyError(code="fpe_unencryptable_domain", strategy="text_mask")` with column context on BOTH routes (not swallowed, not turned into successful masking); absent + invalid sub_floor policy; checksum-failure.
5. **Source admission (3d):** unified unsupported -> oracle fallback; manual chunked numeric/dict -> `chunked_text_mask_source_dtype_unsupported`; chunked `large_string` -> decline native, match chunked oracle; later null-typed chunk -> schema-drift rejection. Admitted empty/all-null tests use explicitly string-typed inputs.
6. **Chunked string-pin (3e):** interleaved valued/empty/all-null chunks -> schema-stable `pa.string()` concatenation, native == oracle leg; full-frame assembly stays null_on_empty.
7. **ner decline (3c):** truthy `ner` -> `text_mask_ner_not_native:<col>`, output == oracle, both routes.
8. **Declines-unchanged / registry-derived; testflight** STOP if a fingerprint moves; **sentries**; a 1M routing record (table-stays-native win).

## 5. Failure modes
| Risk | Closed by |
|---|---|
| Warnings lost (handler-built, not in mask_cell) | 3b transport + capability/routed_diagnostics together; test 3 |
| Chunked schema unstable / lanes differ | 3e string-pin + null_on_empty unified only; test 6 |
| Exception translation bypassed by calling mask_cell | 3b handler reproduction; test 4 |
| Non-string handling contradicts the chunked API | 3d entrypoint split; test 5 |
| Keyed contract (mask_key) not established | 3b mask_key provision; test 1 key-provenance |
| Faker branch assumed-fine but untested | test 1 faker override across chunks |
| A text_mask column still drags its table to the oracle | 3a admission; test 2 |

Rollback: revert the merge; text_mask returns to whole-table oracle decline.

## 6. Review log
- rev 1 (DRAFT): Opus-authored from the C6b framing + C6c-i precedent.
- **Codex plan gate round 1: REVISE** (2 HIGH, 3 MEDIUM; "no fundamental objection"). Rev 2 folds: HIGH-1 warning transport + capability/routed_diagnostics declared together (3a/3b; test 3); HIGH-2 chunked `pa.string()` pin distinct from full-frame null_on_empty (3e; test 6); MEDIUM-3 handler exception translation on both routes (3b; test 4); MEDIUM-4 source-admission split by entrypoint, preserving the existing chunked coded rejection (3d; test 5); MEDIUM-5 the faker-override branch + mask-key provenance (test 1). Also: decline only truthy `ner` (Codex-confirmed sufficient); do NOT copy text_redact's malformed-detector/non-string-token declines. Pending Codex plan re-gate.
