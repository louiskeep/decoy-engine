Status: plan (Phase C/D, C6b-ii: native text_mask splice on the Rust paths. rev 1, pre-Codex-gate. Grounds on the merged C6c-ii text_redact Rust span-detection kernel + the C6b-i ARROW_PYTHON text_mask admission. Decomposed a/b/c; slice a is the foundation + parity harness. HOLD merge for Cam per standing rule; no further Cam decision needed to build -- the native/Python boundary is a capability fact, not a product call.)

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`, `documentation.md`; engine `CLAUDE.md` (established methodology; module-size ratchet; internal boundary). Base: engine `origin/main` @ `9ef26c9f`.

# C6b-ii: a native (Rust) splice for text_mask, reusing the text_redact span-detection kernel

## 0. Why + the measurement that scoped it

The 2026-10-14 GAP1 bench (13 native-eligible columns + 1 `text_mask` column, single chunked table) measured the `text_mask` column's own throughput at ~25k rows/s (`wall_C` 3.93s / 100k), extrapolating to ~393s for one column at 10M -- ~65% of the 600s bar, and cache-inflated (few distinct values hit the detector cache; realistic high-cardinality free text runs slower). The GAP1 "mixed lane" was shelved because it cannot touch that floor: it only stops the slow column from poisoning its native siblings, and GAP6 (routing fitting FK jobs to the SEQ/bounded path) already does that -- on the SEQ route `text_mask` runs as a per-column Python op while its siblings run native. The remaining lever is the `text_mask` column's own throughput. This plan raises it by moving detection + the eligible part of the splice into the Rust fast path.

Honest boundary up front: `text_mask` is NOT fully native-izable. The `faker` span strategy (and NER-derived spans), `date_shift` span text, `fpe` checksum modes (pan/npi), the sub-floor HMAC synthesis, and non-ASCII cells all stay Python by capability, documented in the "Decline map" below. This plan targets the native-eligible majority (regex detection + redact/passthrough splice + plain-digit fpe), not 100%.

## 1. What already exists to build ON (do not rebuild)

- **Rust detection kernel (merged, C6c-ii):** `decoy-engine-native/src/text_redact.rs` -- 8 lookaround-free detectors (`SUPPORTED_IDS`, `CATALOG_VERSION = 1`), ASCII-only eligibility (`is_eligible`: pure ASCII, no bytes 0x1c-0x1f), `candidates_for_cell` → `(detector_idx, start, end)`, five ported validators (Luhn, IBAN mod-97, IPv4, ICD-10, NPI). PyO3: `text_redact_candidates`, `text_redact_catalog_version`, `text_redact_supported_ids`. Uses the `regex` crate (no `fancy-regex`, deliberate).
- **Python seam (merged):** `src/decoy_engine/execution/native/_text_redact_kernel.py` -- `load_text_redact_kernel()` (returns None on companion absence or catalog skew → full Python), `merge_text_redact_spans()` replays `iter_spans` order EXACTLY (extras → built-ins in request order with Rust+Python detectors interleaved → custom → stable leftmost-longest sort) and builds `Span(det_id, start, end, matched_text)` -- so span TEXT is already available to key `text_mask` spans. `requested_rust_ids()`. Per-cell routing: ASCII-safe cell uses Rust for the 8, Python for the 3 lookaround detectors; non-ASCII cell → fully Python.
- **Rust FF1 crypto (merged):** `decoy-engine-native/src/fpe.rs` -- `fpe` crate, AES-256, `derive(...)` with `FF1_KEY_LABEL`, proven vs the Python oracle by a KAT gate. Has `hmac`/`sha2`/`hkdf`. DECLINES checksum modes to Python (C6a §3g). Its plain-digit path is exactly the ssn / us_phone / us_zip shape.
- **text_mask ARROW_PYTHON admission (merged, C6b-i):** `_operator_registry.py:174-189` (`OperatorSpec(strategy="text_mask", operator_id="native_text_mask", shape="kernel", planned_backend=ARROW_PYTHON, routed_diagnostics=reduce_warning:text_mask_sub_floor_span_handled)`); `native/_kernels_scalar.py:198-262` `native_text_mask` (runs `mask_cell` per cell today, threads one `sub_floor_notices` dict, re-raises fail-closed); `native/_text_mask_route.py:35-69` (shared `text_mask_fail_closed_error` + `text_mask_sub_floor_warning`); `native/_chunk_masking.py` (chunked); `_capabilities.py:206-213` (row-local, static-output, warning `text_mask_sub_floor_span_handled`). Config rejections: `native/_operator_config_rejections.py:297-309` `text_mask_config_rejection` declines ONLY a truthy `ner` (`text_mask_ner_not_native:<col>`); a non-string token / malformed detectors are deliberately NOT declined (the oracle normalizes them).
- **text_mask leaf (the oracle to match byte-for-byte):** `src/decoy_engine/transforms/text_mask.py` (722 LOC -- OVER the 700 hard max, legacy, MAY ONLY SHRINK). `mask_cell` (606-722) calls `iter_spans` (693) then splices: unmatched gaps → `_apply_unmatched` (580, default policy `redact` tokenizes every gap), spans → `_mask_span` (538). Span strategies: `_mask_fpe` (399; key `derive(mask_key, "text.{id}", FF1_KEY_LABEL)`, tweak `build_ff1_tweak(FF1_TWEAK_SCOPE_TEXT_SPAN, id)`, `fpe_encrypt_value(preserve_separators=True, checksum=...)`; `_FPE_CONFIG`: ssn/us_phone/us_zip/fax=digits, pan=luhn, npi=npi), `_mask_faker` (473; Python Faker seeded per span), `_mask_date_shift` (508; strptime over `_COMMON_FORMATS`), redact→token, passthrough→text. `_span_key` (240) = HMAC-SHA256(mask_key, matched_text). Sub-floor: `_apply_sub_floor_span_policy` (353) + `_synthetic_span_value` (294) = HMAC counter-mode + `checksums.calc_check_digit`. `DETECTOR_DEFAULTS` (175-212).
- **SEQ op pattern to mirror:** `_sequential_native_ops.py` -- `text_redact_op` (~448), `TEXT_REDACT` constant (87), `SEQ_TEXT_REDACT_DISPOSITION_KEY` (94), `_load_text_redact` (423), `_text_redact_admit` (430); `NativeOp` needs `strategy, disposition_key, load_kernel, admit, apply` + optional `fail_closed, warn, key_admit`. `_sequential_native.py` -- `text_redact_column` (~434-455) via `self._admitted(...)`; `_OPS` (175-183) has NO `text_mask` entry, so `prepare_table` (`_OPS.get(...) is None → continue`, ~250) runs the pandas `TextMaskHandler` per column.
- **Templates:** `docs/plans/2026-10-10-c6b-i-text-mask-arrow.md` (names C6b-ii: "the Rust deterministic-span kernel + the Faker-synthesis/NER-span decline to Rust"); `docs/plans/2026-10-14-ooc-text-redact-native.md` (OOC wiring, decline reasons, the None-vs-() trap).

## 2. Decline map (native vs Python -- a capability fact, honor it, do not smooth)

| Case | Native? | Where it falls back |
|---|---|---|
| Detection, 8 lookaround-free detectors, ASCII cell | Yes (Rust) | reuse kernel |
| Detection, 3 lookaround detectors (ssn, us_zip, street_address) | No (regex crate has no lookaround) | Python detector in the hybrid merge (already how text_redact works) |
| Non-ASCII cell (any) | No | whole cell → Python (`is_eligible` false) |
| Span strategy `redact` / `passthrough` | Yes | Rust splice (indices + token only) |
| Span strategy `fpe`, plain-digit (ssn, us_phone, us_zip, fax) | Yes (slice c) | Rust FF1 (`fpe.rs`, non-checksum) |
| Span strategy `fpe`, checksum (pan=luhn, npi) | No | Python `_mask_fpe` (Rust declines checksum) |
| Span strategy `faker` (incl. NER person_name/location defaults) | No (no Rust Faker) | Python `_mask_faker` |
| Span strategy `date_shift` | No (strptime) | Python `_mask_date_shift` |
| Sub-floor span (fpe below FF1 min domain / checksum-invalid) | No | Python `_apply_sub_floor_span_policy` |
| NER spans (`ner:` config) | No (declined at admission today) | `text_mask_ner_not_native` -- unchanged |

Granularity: the decline is PER SPAN, not per cell -- a cell can have a Rust-spliced redact span and a Python fpe-checksum span. A cell is fully native only when every one of its spans AND the unmatched policy are native-eligible AND the cell is ASCII; otherwise that cell takes the Python splice (slice a/b) or a per-span hybrid (slice c). The build picks the coarsest granularity that is byte-exact and still wins; do not claim per-span hybrid in slice a/b.

## 3. Decomposition (slices, highest value-per-risk first -- each gates independently)

### Slice a -- native detection reuse + parity harness (foundation, LOW risk)
`native_text_mask` (`_kernels_scalar.py`) obtains spans from `load_text_redact_kernel()` + `merge_text_redact_spans()` instead of the Python `iter_spans` inside `mask_cell`; the Python per-span `_mask_span` splice is unchanged. Needs a `mask_cell`-with-precomputed-spans entry point -- put it in a NEW module (e.g. `transforms/_text_mask_splice.py`), because `text_mask.py` is over the size cap and may only shrink; extract the splice body there and have `mask_cell` delegate (this SHRINKS `text_mask.py`, which is allowed and desirable). Companion-absent / catalog-skew / non-ASCII cell → the existing full-Python path (byte-identical). Gain: detection moves to Rust for the 8 detectors on ASCII cells (the dominant cost for redact-heavy scrubbing configs); splice still Python. Establishes the byte-parity harness all later slices reuse.

### Slice b -- native redact/passthrough splice (biggest clean win, MEDIUM risk)
When every configured detector resolves to `redact` or `passthrough` AND `unmatched_span_policy` ∈ {redact, passthrough}, do the ENTIRE splice in Rust: emit token/verbatim per span + gap tokenization per `_apply_unmatched`. A new Rust function on top of `candidates_for_cell` returning the spliced cell for this all-redact/passthrough config. Covers the common free-text scrubbing case. Non-eligible config (any fpe/faker/date_shift span, or an unmatched policy outside the set) falls to slice a's Python splice.

### Slice c -- native fpe spans, plain-digit non-checksum (extends to fpe-preserve configs, MEDIUM-HIGH risk)
Rust FF1 (`fpe.rs`) for `fpe` spans on ssn / us_phone / us_zip / fax (plain-digit, `preserve_separators=True`). The keying must be byte-exact vs Python: `_span_key` = HMAC-SHA256(mask_key, matched_text) (UTF-8 `errors="replace"`), key `derive(mask_key, "text.{id}")`, tweak `build_ff1_tweak(FF1_TWEAK_SCOPE_TEXT_SPAN, id)`. Checksum detectors (pan/npi), sub-floor, and FF1-min-domain cases decline to the Python per-span path (per-span hybrid within the cell). us_zip is lookaround (Python-detected in the merge) but its fpe splice can still be Rust once detected. Slice c is the first per-span hybrid; land a/b first.

### Route coverage (within each slice)
Slice a wires unified + chunked (`_kernels_scalar.native_text_mask`) AND the SEQ route (add a `TEXT_MASK` `NativeOp` to `_sequential_native_ops.py` + `_OPS`/`text_mask_column` in `_sequential_native.py`, mirroring `text_redact_op`: `warn` hook for `text_mask_sub_floor_span_handled`, `fail_closed` for `fpe_unencryptable_domain`, and automatic key-column decline `REASON_KEY_NOT_INJECTIVE` since text_mask is not injective). OOC (`out_of_core/_mask_group_c.py` `_text_mask_array`) already runs ARROW_PYTHON; a native OOC twin follows the `2026-10-14-ooc-text-redact-native.md` pattern and MAY be a slice-a follow-on (a-ooc) rather than block the in-core win -- the build states which routes each slice lands and keeps OOC byte-identical until its twin lands.

## 4. Behavior / parity contract

- **Target: BYTE-IDENTICAL to the Python oracle** for every eligible cell (text_mask is NFC-agnostic: fpe is source-text-exact preserving separators, redact writes a constant token, span text is exact). There is currently NO `text_mask` entry in `tests/parity/SEMANTIC_DIFFERENCES.md`; this plan must NOT introduce one -- if any slice cannot be byte-exact for a case, that case DECLINES to Python rather than diverging. (If a genuinely unavoidable, documented difference emerges, STOP and escalate; do not add a SEMANTIC_DIFFERENCES entry without Cam.)
- **Detection order preserved exactly** by `merge_text_redact_spans` (extras → built-ins in request order → custom → stable `(start, -(end-start))` leftmost-longest). `CATALOG_VERSION` guards pattern/order/supported-set skew → full-Python fallback on mismatch.
- **None-vs-() trap:** resolved `detectors` is `None` for empty/absent selection (= all); the kernel treats `()` as zero detectors. NEVER substitute `()`. (`_strategies/_text_mask.py:61-65` does `[...] or None`.)
- **Sub-floor warning** (`text_mask_sub_floor_span_handled`) counts must be field-for-field identical to `_text_mask_route.py:27-31` (per-column on full-frame, per oracle-equivalent chunk on chunked). The native path threads the same `sub_floor_notices` dict.
- **Fail-closed** on `FpeUnencryptableError` → `StrategyError` exactly as the handler.
- **Raw-value isolation:** `matched_text` never logged / never in evidence (sentry: `tests/unit/transforms/test_text_mask.py`).
- **Fallback transparency:** companion-absent, catalog-skew, non-ASCII cell, and every declined span/config produce output byte-identical to today's Python path.

## 5. Acceptance matrix (per slice; a negative control on every parity claim)

- **T-a1 detection reuse byte-identical:** for a text_mask config over the 8 Rust detectors on ASCII cells, native (companion) output == Python oracle byte-for-byte, across redact/fpe/faker/date_shift span strategies (splice still Python, so all must match). Negative control: an injected real diff fails.
- **T-a2 fallback paths byte-identical:** companion-absent (monkeypatch loader → None), catalog-skew (bump a stub version), non-ASCII cell, a cell with a lookaround-detector span (ssn/us_zip/street_address) -- each == Python oracle; assert the Python path was taken.
- **T-a3 None-vs-() :** `detectors=None` (all) vs an explicit full list vs a single-detector list -- correct detector set each; a mutant substituting `()` for `None` is killed.
- **T-a4 SEQ route:** a table with native-eligible columns + a `text_mask` column now admits `text_mask` to the SEQ native op (assert the op ran, not the pandas handler) and is byte-identical; a key column with `text_mask` declines via `REASON_KEY_NOT_INJECTIVE` (assert reason); sub-floor warning counts identical; fail-closed path raises `StrategyError`.
- **T-b1 native redact splice:** all-redact (and all-passthrough, and mixed redact+passthrough) config with `unmatched_span_policy` redact/passthrough → fully-Rust cell == Python oracle, including empty-gap handling (`_apply_unmatched` returns empty text as-is) and a cell with zero spans (whole cell becomes the gap-policy output). Negative control. A config with one fpe span correctly falls to the slice-a Python splice (assert).
- **T-c1 native fpe spans:** ssn/us_phone/us_zip/fax fpe spans → Rust FF1 == Python `_mask_fpe` byte-for-byte (same key/tweak/separators); pan/npi (checksum) and a sub-floor / below-min-domain case decline to Python within the same cell and still match; `preserve_separators` exact.
- **T-common parity sweep:** a property/fixture sweep of realistic free-text cells (mixed detectors, overlapping candidates, back-to-back spans, unicode-adjacent-ASCII, empty string, all-gap, all-span) native == oracle on every native route landed by the slice.

## 6. Failure modes (reproduce, do not smooth)

- A declined span/config silently diverging instead of falling to Python → T-a1/T-a2/T-b1/T-c1 negative controls.
- Detection order drift (candidate interleave / tie-break) → T-common overlapping/back-to-back fixtures.
- `()` vs `None` detector-set bug → T-a3.
- Sub-floor warning count drift on the native route → T-a4.
- A key column masked by a non-injective strategy breaking FK RI → T-a4 key-decline.
- Catalog-version skew NOT falling back (stale Rust patterns applied) → T-a2.
- Over-growing `text_mask.py` (it is already over the hard max) → size gate; new logic in new modules only.

## 7. Verification + gates

- abi-3 companion venv (`~/.cache/decoy-d1a-abi3-venv/bin/python`) for native/parity; ci-mirror otherwise. One test process via `~/bin/pytest-one`; `PYTHONPATH=<worktree>/src:<worktree>`. Rust change → rebuild the companion into the abi-3 venv before parity runs (the build documents the exact command it used).
- Per-slice: its T-matrix green + the full existing text_mask/text_redact suites green (no masking test weakened). Coverage + mutation on the changed lines (the None-vs-() mutant, a detection-order mutant, and -- slice b/c -- a splice/FF1 mutant must be killed; name them).
- Module size: NO growth of `transforms/text_mask.py` (over the 700 hard max; it may only shrink -- extracting the splice to a new module is the intended shrink). New modules under 600 goal / 700 max; `_sequential_native_ops.py` (563L) and `_mask_group_c.py` (540L) are near goal -- add census entries at the exact LOC if a slice crosses 600, keep under 700.
- Perf witness (narrowed, no 10M rerun): a routing/admission witness that the native text_mask op is selected, plus one small real run per landed route (unified/chunked/SEQ) showing the native arm completes faster than the Python-handler arm on a redact-heavy config (slice a/b) -- report rows/s before/after; do NOT extrapolate a 10M claim in the plan.
- Gate chain per slice: Opus VERIFY → dennis → Codex final. Build freely; HOLD merge for Cam (Rust + hot-path masking change). Post diff + before/after rows/s to Slack; flag before any merge-to-main.
- Established-methodology note in each new module docstring (regex crate for detection; FF1/FPE per the existing fpe.rs pattern; cite the source as the engine CLAUDE.md requires).

## 8. Done when (per slice)

- [ ] Slice's T-matrix green (byte-identical native==oracle on landed routes; every decline falls to Python and matches; SEQ admission + key-decline + sub-floor + fail-closed for slice a).
- [ ] Full text_mask + text_redact suites green; no masking test weakened; changed-lines coverage + mutation-proven (named mutants killed).
- [ ] `text_mask.py` not grown; new modules within size policy; census updated if crossed.
- [ ] Perf witness recorded (before/after rows/s per route); diff + results to Slack; HELD for Cam's merge go.
- [ ] On merge: main CI green (CI + nightly testflight), ROADMAP + RECENTLY-SHIPPED updated (C6b-ii slice closed), graphify refresh (subject to the keyless blocker), Slack recap.

cam
