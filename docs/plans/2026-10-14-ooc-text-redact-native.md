Status: plan (Phase D / OOC text_redact native. DRAFT rev 1, pre Codex plan-gate. Wire the existing compiled text_redact kernel onto the out-of-core route.)

# text_redact native on the out-of-core route

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`; engine `CLAUDE.md`. Grounding: the 2026-10-14 bottleneck audit (OOC text_redact runs per-cell Python) + the just-shipped OOC fpe-native slice (#255, same wiring shape) + the text_redact SEQ slice (#254, same kernel + admission + the detectors None-vs-() rule). Base: engine `origin/main` @ `c81f397a` (post OOC fpe).

## 0. Why + scope
text_redact is ADMITTED on the out-of-core (OOC) route today (`_GROUP_B_SUPPORTED_STRATEGIES`) but masks per-cell in Python: `out_of_core/_mask_group_b.py:218` `text_redact_array(values, *, plan)` runs the oracle's `iter_spans` + `_splice` per cell. The compiled text_redact kernel already exists and is byte-parity-proven on the chunked, full-frame and SEQ routes (`native/_kernels_scalar.py:137` `native_text_redact(array, *, detectors, token, label_token)`: Rust detection for the 8 lookaround-free detectors on ASCII-safe cells, Python replay for lookaround / non-ASCII / companion-absent, byte-identical to the oracle). This slice swaps the OOC text_redact backend to that kernel, inside the already-admitted path. NO new Rust, no `_compat.py` change. Mirrors the OOC fpe-native slice (#255); SIMPLER than fpe — text_redact has no fail-closed row-error channel (it passes through unchanged on a bad config), so there is no `fpe_fail_closed_error` analogue.

**In scope:** text_redact PAYLOAD columns on the OOC route take the compiled kernel when eligible; the existing Python `text_redact_array` stays as reference and declined-fallback.
**Out of scope / DECLINE to the existing Python `text_redact_array` (byte-identical):** `ner:` (truthy) — spaCy per-cell, the documented permanent exception (`text_redact_ner_not_native`); a non-string `token` (`text_redact_token_not_string`); malformed `detectors` (`text_redact_detectors_malformed`); a non-exact-`string` source (`text_redact_source_type_not_string`); and no companion. These are the SEQ/chunked `text_redact_config_rejection` gates (`native/_operator_config_rejections.py:279`) + the exact-string source check. text_redact is PAYLOAD-only (never an FK key).

## 1. Risk
R2 (privacy-relevant masking). Output must be byte-identical to the oracle. Same compiled kernel the other routes prove, called per OOC batch; detection is per-value with no cross-batch state. Acceptance compares OOC-native to the full-frame oracle (and to the OOC Python path with the kernel hidden) on the strict parity contract + a consumed-output witness.

## 2. The code change (mirror the OOC fpe-native slice `_fpe_native.py`)
- **NEW `out_of_core/_text_redact_native.py`** (~150-180 LOC; template `out_of_core/_fpe_native.py`):
  - `decline_reason(column, schema, seed) -> tuple[ResolvedConfig | None, str | None]`: `text_redact_config_rejection(column, cfg)` (ner / non-string-token / malformed-detectors) + exact-`string` source check. On eligible, resolve the config ONCE: `token`, `label_token`, and `detectors` via `_text_redact_detector_ids(cfg.get("detectors"))` — **CRITICAL (text_redact SEQ #254 lesson): the resolved `detectors` is `None` for an empty/absent selection (= ALL detectors); the kernel treats `()` as ZERO. Transport the resolved value verbatim; never substitute `()`.**
  - `TextRedactColumnDecision` / `entry()` (native_calls-gated, exactly like `FpeColumnDecision`); `TableTextRedactPlan.gate()` with the pinned-type drift + missing-column abort copied from `_fpe_native.py` (`out_of_core_text_redact_source_type_drift` / `_column_missing`).
  - a local presence-marker `BoundTextRedactKernel` (text_redact kernel load is companion-gated; `native_text_redact` self-loads, so the bound is the companion-present gate, matching fpe's circular-import-avoiding local holder).
  - `native_text_redact_array(values, *, native, detectors, token, label_token) -> pa.Array`: calls `native_text_redact(values, detectors=detectors, token=token, label_token=label_token)` and returns its `pa.Array`. NO fail-closed channel (text_redact never raises per-row; it passes through). Keep a result-shape guard (one value per input row, `string`).
- **`out_of_core/_hash_run.py`**: a `_text_redact_kernel()` presence loader (parallel to `_fpe_kernel()`); a `text_redact_seeds` block in `prepare_table` excluding `skip_columns`; carry `TableTextRedactPlan` on `TableHashPlan` with `text_redact_for(column, batch)`; a `TEXT_REDACT_DISPOSITION_KEY = "out_of_core_text_redact"` in `metrics()`.
- **`out_of_core/_mask.py`**: add a `text_redact_native` param to `mask_column`, dispatch to `native_text_redact_array` at the text_redact branch when present, else the existing `text_redact_array(values, plan=seed)`; compute `text_redact_native = hash_plan.text_redact_for(...)` in `mask_batch` (parallel to the fpe dispatch).
- **No change** to `_compat.py` (already admitted), `_mask_group_b.py` (`text_redact_array` stays reference + declined path), or the drivers.

## 3. Acceptance matrix (mirror the OOC fpe suite, minus fail-closed)
Each cell compares OOC-native vs the full-frame oracle (and vs the OOC Python path, kernel hidden) on the strict parity contract. Reuse the fpe/categorical native-suite + group-B parity/routing shape.
- **T1 native byte-parity**: a text_redact payload column on an OOC FK job is byte-identical to the oracle, across row counts / batch boundaries, incl. nulls / empty / all-null; Rust-eligible detectors (email/phone/pan/iban/ipv4/icd10/npi/url), lookaround detectors (ssn/us_zip/street_address, replayed per-cell in Python inside the kernel), and a mixed ASCII/non-ASCII column (per-cell domain split). Output type `string`. Consumed-output witness (marking kernel + a negative control that discards the native result and fails), native_calls-gated — NOT disposition-alone.
- **T2 config transport (the None-vs-() trap)**: absent / null / empty detector selection with a payload that contains PII all detectors catch → masks ALL (resolved `None` = all), not nothing; a real detector subset; custom and empty token; `label_token=True`. Spy the compiled candidate call to prove the resolved detector set reached the kernel.
- **T3 declines (route to Python `text_redact_array`, byte-identical, coded reason)**: `ner:true` → `text_redact_ner_not_native`; non-string token → `text_redact_token_not_string`; malformed detectors → `text_redact_detectors_malformed`; non-string / large_string source; no companion. Assert the disposition reports the Python backend + reason, `native_calls == 0`.
- **T4 type-pin / drift + missing-column abort** on BOTH drivers; sink abort-not-commit.
- **T5 routing**: `run_pipeline` dispatches a large text_redact-payload FK job to OOC, byte-identical to full-frame (extend the group-B routing suite); native witness under companion, Python path under ci-mirror.
- **T6 mixed job + payload-only**: text_redact native beside hash + fpe + categorical native on one OOC job; each reports its own disposition, no cross-contamination; text_redact stays payload-only (off the FK key surface via skip_columns).

## 4. Failure modes (reproduce, do not smooth)
- Passing `()` for an empty detector selection → masks nothing instead of all → T2 (the highest-risk defect, the #254 trap).
- A lookaround / non-ASCII cell diverging from the oracle → T1 (the per-cell Python replay must be exact; the shipped kernel's own contract, re-asserted on OOC).
- NER / non-string token silently going native → T3 (must decline to the Python path).
- Drift / missing column silently mis-masking → T4.

## 5. Verification + gates
- abi-3 `~/.cache/decoy-d1a-abi3-venv/bin/python` (companion); ci-mirror `~/.cache/decoy-ci-mirror-venv/bin/python` (no companion, exercises the decline/Python path); one test process via `~/bin/pytest-one`; PYTHONPATH `<worktree>/src:<worktree>`.
- T1-T6 pass in abi-3 (witness non-vacuous); declines + companion-absent in ci-mirror. Full existing OOC suite stays green (group-B parity/routing + fpe/categorical/bucket/hash native); the SEQ/chunked/full-frame text_redact suites stay green (the shared kernel is untouched).
- Coverage + mutation on every changed production line; prove surviving mutants equivalent by injection.
- Module size: new `_text_redact_native.py` (~150-180) + small `_hash_run.py` / `_mask.py` edits stay under the 600 goal / 700 max; confirm post-build.
- Gate chain: Opus VERIFY (diff + both venvs + equivalence proofs) → dennis → Codex final. Merge under the standing Rust rule (dennis GO + Codex GO + CI green + text_redact parity contract + faster on the OOC text_redact step + within memory ceiling). Flag Slack before merge.

## 6. Done when
- [ ] T1-T6 green (native byte-parity + consumed-output witness + string-type pin; config transport incl. None-vs-() with a candidate-call spy; declines incl. NER/token/detectors/source/no-companion; type-pin drift + missing column both drivers; routing; mixed-job isolation payload-only); full OOC + SEQ/chunked/full-frame text_redact suites green both venvs.
- [ ] Changed production lines coverage+mutation-proven; modules within size policy; sentries green (incl. the physical-seam allowlist entry for the new module).
- [ ] ROADMAP + RECENTLY-SHIPPED updated; graphify refresh (subject to the keyless-refresh blocker — note if deferred); Slack recap in plain language.

cam
