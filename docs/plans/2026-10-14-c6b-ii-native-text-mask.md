Status update (2026-10-15): slices a+b merged in engine #259. Slice c passed the final cross-model gate at code HEAD `c0567094`. Merge is pending. The owner authorized autonomous merge after GO, superseding the historical HOLD instructions below.

Slice c includes `ssn` as an accepted build deviation. Its detector emits exactly nine digits, so its domain size is `10^9`, above the `10^6` FF1 floor. Native encryption also covers eligible `us_phone` and `us_zip` ZIP+4 spans. ZIP5, checksum detectors, and non-ASCII cells retain the Python path. The implementation reuses the existing Rust kernel and adds no Rust code.

Final-gate evidence reported by the reviewing Codex session: 2,413 tests passed and five skipped. Independent companion probes produced 1,620 exact ciphertext matches and 720 expected declines. Eight error/order cases also matched the oracle. Dennis approved `46b859de` with one LOW finding. Commit `c0567094` fixes that finding with exception fallback and a regression test. The documentation pass records these results without a new throughput claim. Post-merge CI and roadmap/shipped-log updates remain pending.

The original plan and its historical grounding follow.

Status: built
Shipped: commit 14c459c1 (PR #259, slices a+b) and commit 1b2628e6 (PR #260, slice c)
Prior status (kept because it records a hold): plan (Phase C/D, C6b-ii: native text_mask splice on the Rust paths. rev 3 (Codex rev2 REVISE folded: 0 BLOCKER/0 HIGH, 2 MED editorial -- status-map consistency for sub-floor/ZIP5/replace_with_token, and the `extra_spans` leaf-API correction; design validated by Codex with 20 companion probes). rev 2 folded Codex rev1 REVISE: 2 HIGH + 4 MED -- SEQ execution+warning seam, slice-b merged-span eligibility, decline-map precision, slice-c FPE keying, unproven cache/dominance claim removed, _sequential_native.py size budget). Grounds on the merged C6c-ii text_redact Rust span-detection kernel + the C6b-i ARROW_PYTHON text_mask admission. Decomposed a/b/c; slice a is the foundation + parity harness. HOLD merge for Cam per standing rule; no further Cam decision needed to build -- the native/Python boundary is a capability fact, not a product call.)

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `autonomous-operation.md`, `architecture.md`, `documentation.md`; engine `CLAUDE.md` (established methodology; module-size ratchet; internal boundary). Base: engine `origin/main` @ `9ef26c9f`.

# C6b-ii: a native (Rust) splice for text_mask, reusing the text_redact span-detection kernel

## 0. Why + the measurement that scoped it

The 2026-10-14 GAP1 bench (13 native-eligible columns + 1 `text_mask` column, single chunked table) measured the `text_mask` column's own throughput at ~25k rows/s (`wall_C` 3.93s / 100k), extrapolating to ~393s for one column at 10M -- ~65% of the 600s bar. (Neither `mask_cell` nor `iter_spans` caches detection, so ~25k rows/s is the real rate for that text, not a cache artifact; an earlier "cache-inflated" note was wrong. Whether detection dominates the per-cell cost is a HYPOTHESIS slice a must measure, not assume -- see §7 perf.) The GAP1 "mixed lane" was shelved because it cannot touch that floor: it only stops the slow column from poisoning its native siblings, and GAP6 (routing fitting FK jobs to the SEQ/bounded path) already does that -- on the SEQ route `text_mask` runs as a per-column Python op while its siblings run native. The remaining lever is the `text_mask` column's own throughput. This plan raises it by moving detection + the eligible part of the splice into the Rust fast path.

Honest boundary up front: `text_mask` is NOT fully native-izable. The `faker` span strategy (and NER-derived spans), `date_shift` span text, `fpe` checksum modes (pan/npi), and non-ASCII cells stay Python by capability; the sub-floor HMAC synthesis (and the `replace_with_token` unmatched policy) stay Python for now but are native-able later (DEFER, §2) -- they are scope choices, not capability limits. This plan targets the native-eligible majority (regex detection + redact/passthrough splice + plain-digit fpe), not 100%.

## 1. What already exists to build ON (do not rebuild)

- **Rust detection kernel (merged, C6c-ii):** `decoy-engine-native/src/text_redact.rs` -- 8 lookaround-free detectors (`SUPPORTED_IDS`, `CATALOG_VERSION = 1`), ASCII-only eligibility (`is_eligible`: pure ASCII AND no bytes 0x1c-0x1f -- both conditions, not merely "ASCII"), `candidates_for_cell` -> `(detector_idx, start, end)`, five ported validators (Luhn, IBAN mod-97, IPv4, ICD-10, NPI). PyO3: `text_redact_candidates`, `text_redact_catalog_version`, `text_redact_supported_ids`. Uses the `regex` crate (no `fancy-regex`, deliberate).
- **Python seam (merged):** `src/decoy_engine/execution/native/_text_redact_kernel.py` -- `load_text_redact_kernel()` (returns None on companion absence or catalog skew -> full Python), `merge_text_redact_spans()` replays `iter_spans` order EXACTLY (extras -> requested built-ins including duplicate IDs with Rust+Python detectors interleaved -> custom -> stable leftmost-longest `(start, -(end-start))`) and builds `Span(det_id, start, end, matched_text)` -- so the merged list ALREADY contains Python-detected lookaround spans (ssn/us_zip/street_address) alongside the Rust-detected ones, and span TEXT is available. `requested_rust_ids()`. Per-cell routing inside the merge: ASCII-safe cell uses Rust for the 8 + Python for the 3 lookaround; non-ASCII cell -> fully Python. (Codex verified this ordering against the installed companion with 20 equality probes incl. empty selections, extras, zero-length custom matches, repeated IDs -- all passed. NOTE: `merge_text_redact_spans` exposes a `custom` path; `mask_cell` does NOT accept/forward `custom` (custom regex patterns) today -- do not introduce custom-pattern behavior through the broader merge API. BUT `mask_cell` DOES accept and forward `extra_spans` (NER-injected / directly-supplied spans, Codex-probed) -- the splice extraction MUST preserve the `extra_spans` parameter.)
- **Rust FF1 crypto (merged):** `decoy-engine-native/src/fpe.rs` -- `fpe` crate, AES-256, `derive(...)` with `FF1_KEY_LABEL`, proven vs the Python oracle by a KAT gate. Has `hmac`/`sha2`/`hkdf`. Its plain-digit path is the ssn / us_phone / us_zip shape. CAUTION (Codex): `fpe.rs` has NO checksum parameter and NO checksum-decline branch; its `validate_luhn` branch is NOT the checksum-mode contract. Checksum configs are rejected today by Python admission + the compiled wrapper, so slice c must ENFORCE the checksum decline at the new span seam itself (the Rust FF1 call never sees a checksum mode).
- **text_mask ARROW_PYTHON admission (merged, C6b-i):** `_operator_registry.py:174-189` (`OperatorSpec(strategy="text_mask", operator_id="native_text_mask", shape="kernel", planned_backend=ARROW_PYTHON, routed_diagnostics=reduce_warning:text_mask_sub_floor_span_handled)`); `native/_kernels_scalar.py:198-262` `native_text_mask` (runs `mask_cell` per cell today, returns `(values, notices)`, threads one `sub_floor_notices` dict, re-raises fail-closed); `native/_text_mask_route.py:35-69` (shared `text_mask_fail_closed_error` + `text_mask_sub_floor_warning`; the warning detail string is built at `:27-31`); `native/_chunk_masking.py` (chunked); `_capabilities.py:206-213` (row-local, static-output, warning `text_mask_sub_floor_span_handled`). Config rejections: `native/_operator_config_rejections.py:297-309` `text_mask_config_rejection` declines ONLY a truthy `ner` (`text_mask_ner_not_native:<col>`); a non-string token / malformed detectors are deliberately NOT declined (the oracle normalizes them). Admission is STRING-ONLY, so the handler's `str()` coercion of non-string cells cannot reach an admitted native call.
- **text_mask leaf (the oracle to match byte-for-byte):** `src/decoy_engine/transforms/text_mask.py` (722 LOC -- OVER the 700 hard max, legacy, MAY ONLY SHRINK). `mask_cell` (606-722) calls `iter_spans` (693) then splices: unmatched gaps -> `_apply_unmatched` (580; default policy `redact` tokenizes every gap; `replace_with_token` writes a constant `[UNMATCHED]`; empty gap returned as-is), spans -> `_mask_span` (538). `_mask_span` dispatches FPE at :557 BEFORE computing `_span_key` -- so FPE does NOT consume `_span_key`. Span strategies: `_mask_fpe` (399; key `derive(mask_key, "text.{id}", FF1_KEY_LABEL)`, tweak `build_ff1_tweak(FF1_TWEAK_SCOPE_TEXT_SPAN, id)`, `fpe_encrypt_value(preserve_separators=True, checksum=...)`; `_FPE_CONFIG` (216): ssn/us_phone/us_zip/fax_number = digits, pan = luhn, npi = npi), `_mask_faker` (473; Python Faker seeded per span, reseeded per span, byte-identical), `_mask_date_shift` (508; strptime over `_COMMON_FORMATS`), redact -> token, passthrough -> text. `_span_key` (240) = HMAC-SHA256(mask_key, matched_text), UTF-8 `errors="replace"` -- used by faker/date_shift ONLY. Sub-floor (an fpe span below the FF1 min domain, or checksum-invalid): `_apply_sub_floor_span_policy` (353) + `_synthetic_span_value` (294) = HMAC counter-mode + `checksums.calc_check_digit`. `DETECTOR_DEFAULTS` (175-212).
- **SEQ op + execution pattern to mirror:** `_sequential_native_ops.py` -- `text_redact_op` (~448), `TEXT_REDACT` constant (87), `SEQ_TEXT_REDACT_DISPOSITION_KEY` (94), `_load_text_redact` (423), `_text_redact_admit` (430); `NativeOp` needs `strategy, disposition_key, load_kernel, admit, apply` + optional `fail_closed, warn, key_admit`. `_sequential_native.py` (645 LOC) -- `text_redact_column` (~434-455) via `self._admitted(...)`; `_OPS` (175-183) has NO `text_mask` entry, so `prepare_table` (`_OPS.get(...) is None -> continue`, ~250) runs the pandas `TextMaskHandler` per column. CRITICAL (Codex HIGH-1): `prepare_table` only RECORDS admission; EXECUTION is driven by the handler seam -- `TextRedactHandler` explicitly calls `seq.text_redact_column` at `_strategies/_text_redact.py:128`. `TextMaskHandler` has no equivalent call today; registering the op is necessary but NOT sufficient. text_mask FK keys get a reporting-only `REASON_KEY_NOT_INJECTIVE` decline in `prepare_table` at `_sequential_native.py:254`; do NOT add text_mask to `_KEY_STRATEGIES`.
- **Templates:** `docs/plans/2026-10-10-c6b-i-text-mask-arrow.md` (names C6b-ii); `docs/plans/2026-10-14-ooc-text-redact-native.md` (OOC wiring, decline reasons, None-vs-() trap).

## 2. Status map: native now vs deferred vs Python-required (a capability + scope fact)

Three statuses, not "native/impossible" (Codex MED-3): **NOW** = implemented by this plan; **DEFER** = native-able but out of this plan's scope (do not implement, do not claim impossible); **PY** = Python-required by capability.

| Case | Status | Reason / fallback |
|---|---|---|
| Detection, 8 lookaround-free detectors, ASCII cell | NOW | reuse Rust kernel |
| Detection, 3 lookaround detectors (ssn, us_zip, street_address) | NOW (via Python in the merge) | `merge_text_redact_spans` already runs the Python detector and interleaves -- the splice consumes MERGED spans, so these ARE covered |
| Non-ASCII cell (any) | PY | whole cell -> Python (`is_eligible` false: non-ASCII or a 0x1c-0x1f control) |
| Span strategy `redact` / `passthrough` | NOW (slice b) | Rust splice over merged span indices + token |
| Unmatched policy `redact` (constant token per gap) / `passthrough` | NOW (slice b) | Rust gap tokenization matching `_apply_unmatched` |
| Unmatched policy `replace_with_token` (constant `[UNMATCHED]` per gap) | DEFER (Python now) | native-able (constant gap string) but out of slice-b scope; a config using it falls to the slice-a Python splice |
| Span strategy `fpe`, plain-digit, clears FF1 floor (ssn; us_phone; us_zip ZIP+4) | NOW (slice c) | Rust FF1 (`fpe.rs`), checksum decline enforced at the seam |
| Span strategy `fpe`, us_zip ZIP5 (below the 6-digit FF1 floor) | DEFER (Python now) | the sub-floor path (`_apply_sub_floor_span_policy`), same deferred row as sub-floor below -- Codex probe: ZIP5 raised `fpe.unencryptable_domain`, ZIP+4 encrypted |
| Span strategy `fpe`, checksum (pan=luhn, npi) | PY | Rust FF1 has no checksum contract; decline at the seam -> Python `_mask_fpe` |
| Sub-floor span (fpe below FF1 min domain / checksum-invalid) | DEFER (native-able later; Python now) | `_apply_sub_floor_span_policy` + `calc_check_digit`; matching diagnostics are the work |
| `fax_number` fpe | PY | `fax_number` is in `_FPE_CONFIG` but ABSENT from `_SPAN_DETECTORS` -- unreachable via built-in detector SELECTION; reachable only via the leaf `extra_spans` API (NER-injected / directly supplied), which declines to Python. Not a native target, but the splice extraction must keep the leaf `extra_spans` path working. |
| Span strategy `faker` (incl. NER person_name/location defaults) | PY | no Rust Faker; `_mask_faker` (per-span `_span_key` seed) |
| Span strategy `date_shift` | PY | strptime; `_mask_date_shift` (per-span `_span_key`) |
| NER spans (`ner:` config) | PY | declined at admission today (`text_mask_ner_not_native`) -- unchanged |

Granularity: the decline is PER SPAN, not per cell -- a cell can have a Rust-spliced redact span and a Python fpe-checksum span. A cell is fully native only when every span AND the unmatched policy are native-eligible AND the cell is ASCII; otherwise that cell takes the Python splice (slice a) or a per-span hybrid (slice c). The build picks the coarsest granularity that is byte-exact and still wins; do not claim per-span hybrid in slice a/b.

## 3. Decomposition (slices, highest value-per-risk first -- each gates independently)

### Slice a -- native detection reuse + the SEQ execution seam + parity harness (foundation)
Two parts, both required for slice a to actually run natively:

(a-detection) `native_text_mask` (`_kernels_scalar.py`) obtains spans from `load_text_redact_kernel()` + `merge_text_redact_spans()` instead of the Python `iter_spans` inside `mask_cell`; the Python per-span `_mask_span` splice is unchanged. Needs a `mask_cell`-with-precomputed-spans entry point in a NEW module (`transforms/_text_mask_splice.py`): extract the splice body (`_apply_unmatched` + `_mask_span` loop) so `mask_cell` delegates, SHRINKING `text_mask.py` (allowed/desired). Preserve: early returns, passthrough logging, effective-map/config precedence, DISTINCT gap-token vs span-token, the leaf `extra_spans` parameter (NER-injected / directly supplied spans), and no circular import between the leaf and the extracted helper. Companion-absent / catalog-skew / non-ASCII cell -> existing full-Python path (byte-identical).

(a-seq) The SEQ EXECUTION seam (Codex HIGH-1), not just registration:
- Add a `TEXT_MASK` `NativeOp` to `_sequential_native_ops.py` (mirroring `text_redact_op`) + an `_OPS` entry + a `TEXT_MASK` constant + a disposition key.
- Add `text_mask_column(...)` to `_sequential_native.py` AND a concrete call to it from `TextMaskHandler` (mirroring `_text_redact.py:128` `seq.text_redact_column`). Registration alone does not execute; `prepare_table` only records the decision.
- Transport the resolved text_mask config + the mask key into the op; assign outputs positionally.
- WARNING TRANSPORT: `native_text_mask` returns `(values, notices)` but `NativeResult` carries values + errors, not notices. Add an explicit return channel for the actual per-detector notice COUNTS, and build the aggregate `text_mask_sub_floor_span_handled` warning from those counts WITHOUT rerunning masking (reuse `_text_mask_route.py:27-31`'s detail builder). Preserve the `FpeUnencryptableError -> StrategyError` translation.
- Key columns: text_mask FK keys get the reporting-only `REASON_KEY_NOT_INJECTIVE` decline in `prepare_table:254`; do NOT add text_mask to `_KEY_STRATEGIES` (payload columns reach `_decide`; keys deliberately do not).

Gain: detection moves to Rust for the 8 detectors on ASCII cells; splice still Python. Whether that is a meaningful route-level win is measured (§7), and slice a is kept separate ONLY if its measured gain justifies landing it (there is no correctness reason to fold it into b).

### Slice b -- native redact/passthrough splice (biggest clean win)
Eligibility (Codex HIGH-2 fix): resolve `detectors=None` to the COMPLETE detector set first; the config is slice-b-eligible when every configured detector's strategy resolves to `redact`/`passthrough` AND `unmatched_span_policy` in {redact, passthrough}. The Rust splice operates on the RESOLVED MERGED spans (from `merge_text_redact_spans`, which INCLUDES Python-detected lookaround spans), not on raw `candidates_for_cell` output -- otherwise an all-redact config over a lookaround detector would silently miss spans. Counterexample to cover: detectors `["ssn"]`, strategy `redact`, unmatched `passthrough`, input `123-45-6789` -- Python redacts it; a raw-Rust-candidate splice would not (ssn is lookaround). The Rust splice takes (cell, merged spans, gap/token policy) -> spliced string: token per span, gap tokenization per `_apply_unmatched`. A config with any fpe/faker/date_shift span, or an unmatched policy outside the set, falls to slice a's Python splice.

### Slice c -- native fpe spans, plain-digit non-checksum (extends to fpe-preserve configs)
Rust FF1 (`fpe.rs`) for `fpe` spans on ssn, us_phone, and us_zip (ZIP+4). Exact recipe (Codex MED-4), no ambiguity: original `mask_key` -> `derive(mask_key, f"text.{detector_id}", FF1_KEY_LABEL)`; text-span-scope tweak `build_ff1_tweak(FF1_TWEAK_SCOPE_TEXT_SPAN, detector_id)`; digits charset; `preserve_separators=True`; `validate_luhn=False`. NO matched-text HMAC enters FF1 (`_span_key` is faker/date_shift only). The seam ENFORCES the checksum decline (pan/npi never reach Rust FF1). Decline to the Python per-span path, within the same cell, for: checksum detectors (pan/npi), us_zip ZIP5 and any below-FF1-floor span, and sub-floor/checksum-invalid cases. This is the first per-span hybrid; it is sound as long as Python handles each declined span exactly once, preserving notice counts and exception order. Land a and b first.

### Route coverage (within each slice)
Slice a lands unified + chunked (`_kernels_scalar.native_text_mask`) AND the SEQ route (the a-seq seam above). OOC (`out_of_core/_mask_group_c.py` `_text_mask_array`) already runs ARROW_PYTHON; a native OOC twin follows `2026-10-14-ooc-text-redact-native.md` and MAY be a slice-a follow-on (a-ooc) rather than block the in-core win -- each slice states which routes it lands and keeps the others byte-identical until their twin lands.

## 4. Behavior / parity contract

- **Target: BYTE-IDENTICAL to the Python oracle** for every eligible cell (text_mask is NFC-agnostic: fpe source-text-exact preserving separators, redact a constant token, span text exact). There is NO `text_mask` entry in `tests/parity/SEMANTIC_DIFFERENCES.md`; this plan must NOT introduce one -- if a case cannot be byte-exact, it DECLINES to Python. (Codex found no unavoidable divergence and no entry needed. If one genuinely emerges, STOP and escalate; do not add an entry without Cam.)
- **Detection order preserved exactly** by `merge_text_redact_spans` (verified). `CATALOG_VERSION` skew -> full-Python fallback.
- **None-vs-() / []-vs-None:** resolved `detectors` is `None` for empty/absent selection (= all); the kernel treats `()` as zero detectors; the leaf treats explicit `[]` as no detectors. NEVER substitute `()` for `None`. (`_strategies/_text_mask.py:61-65` does `[...] or None`.)
- **Sub-floor warning** counts field-for-field identical to `_text_mask_route.py:27-31` (per-column full-frame, per oracle-equivalent chunk on chunked); built from the transported counts, not a rerun.
- **Fail-closed** on `FpeUnencryptableError` -> `StrategyError` exactly as the handler.
- **Raw-value isolation:** `matched_text` never logged / never in evidence (sentry: `tests/unit/transforms/test_text_mask.py`).
- **Fallback transparency:** companion-absent, catalog-skew, non-ASCII cell, and every declined span/config -> byte-identical to today's Python path.

## 5. Acceptance matrix (per slice; a negative control on every parity claim)

- **T-a1 detection reuse byte-identical:** text_mask over the 8 Rust detectors on ASCII cells, native (companion) == Python oracle byte-for-byte, across redact/fpe/faker/date_shift span strategies (splice still Python). Negative control: an injected real diff fails.
- **T-a2 fallback paths byte-identical:** companion-absent (loader -> None), catalog-skew (stub version bump), non-ASCII cell, a 0x1c-0x1f control cell, and a lookaround-detector span cell -- each == Python oracle; assert the Python path was taken. Plus a leaf `extra_spans` regression (e.g. an injected `fax_number` span) still masks correctly after the splice extraction.
- **T-a3 detector-set resolution:** `detectors=None` (all) vs explicit full list vs single-detector list vs explicit `[]` -- correct set each; a mutant substituting `()` for `None` is killed.
- **T-a4 SEQ route EXECUTES (not just registers):** a table with native-eligible columns + a `text_mask` column -- assert the SEQ op actually RAN (the native call fired, not the pandas handler), output byte-identical, sub-floor warning counts identical (via the transported-count path, assert no re-mask), fail-closed raises `StrategyError`; a `text_mask` FK key column declines via `REASON_KEY_NOT_INJECTIVE` (assert reason, assert text_mask not in `_KEY_STRATEGIES`).
- **T-b1 native redact splice:** all-redact, all-passthrough, and mixed redact+passthrough with unmatched redact/passthrough -> fully-Rust cell == oracle, incl. empty-gap (returned as-is), zero-span cell (whole cell = gap-policy output), and the `["ssn"]`+redact+passthrough lookaround counterexample (merged-span splice catches it). A config with one fpe span falls to the slice-a Python splice (assert). Negative control.
- **T-c1 native fpe spans:** ssn, us_phone, and us_zip-ZIP+4 fpe -> Rust FF1 == Python `_mask_fpe` byte-for-byte (same derive/tweak/charset/separators, `validate_luhn=False`, no `_span_key` in FF1); pan/npi (checksum), us_zip-ZIP5, and a sub-floor case decline to Python within the same cell and still match, each declined span handled exactly once (notice counts + exception order preserved).
- **T-common parity sweep:** realistic free-text cells (mixed detectors, overlapping/back-to-back spans, zero-length matches, unicode-adjacent-ASCII, empty string, all-gap, all-span) native == oracle on every route the slice landed.

## 6. Failure modes (reproduce, do not smooth)

- SEQ op registered but never executed (HIGH-1) -> T-a4 asserts the native call fired.
- Slice-b splice over raw Rust candidates missing a Python-detected span (HIGH-2) -> T-b1 ssn counterexample.
- A declined span/config silently diverging instead of falling to Python -> negative controls on T-a1/T-b1/T-c1.
- Detection order drift -> T-common overlapping/back-to-back/zero-length fixtures.
- `()` vs `None` vs `[]` detector-set bug -> T-a3.
- Sub-floor warning count drift / a rerun to compute it -> T-a4.
- Double-HMAC or wrong FF1 key on an fpe span -> T-c1 byte-equality.
- A key column masked by a non-injective strategy breaking FK RI -> T-a4 key-decline.
- Catalog-version skew NOT falling back -> T-a2.
- Over-growing `text_mask.py` or crossing the cap on `_sequential_native.py` -> size gate.

## 7. Verification + gates

- abi-3 companion venv (`~/.cache/decoy-d1a-abi3-venv/bin/python`) for native/parity; ci-mirror otherwise. One test process via `~/bin/pytest-one`; `PYTHONPATH=<worktree>/src:<worktree>`. Rust change -> rebuild the companion into the abi-3 venv before parity (document the exact command).
- Per-slice: its T-matrix green + the full existing text_mask/text_redact suites green (no masking test weakened). Coverage + mutation on changed lines (the `()`-for-`None` mutant, a detection-order mutant, and -- slice b/c -- a splice/FF1 mutant must be killed; name them).
- Module size: NO growth of `transforms/text_mask.py` (over the 700 hard max; may only shrink -- the splice extraction IS the shrink). `_sequential_native.py` is 645 LOC (55 under the hard max): budget it explicitly, extract cohesive helpers if the a-seq seam would cross 700, add a census entry at the exact LOC if it crosses 600. `_sequential_native_ops.py` (563L) and `_mask_group_c.py` (540L): census if a slice crosses 600, keep under 700. Avoid a circular import between `text_mask.py` and `_text_mask_splice.py`.
- Perf witness (Codex MED-5; narrowed, no 10M rerun): detection-dominance is a HYPOTHESIS -- benchmark detection separately (warmup + repeated measurements, BOTH repeated-value and high-cardinality text) and compare whole routes, before/after rows/s per landed route (unified/chunked/SEQ) on a redact-heavy config. Report the numbers; do NOT extrapolate a 10M claim. If slice a's measured route gain does not justify a standalone landing, fold it into slice b.
- Gate chain per slice: Opus VERIFY -> dennis -> Codex final. Build freely; HOLD merge for Cam (Rust + hot-path masking change). Post diff + before/after rows/s to Slack; flag before any merge-to-main.
- Established-methodology note in each new module docstring (regex crate for detection; FF1/FPE per the existing `fpe.rs` pattern).

## 8. Done when (per slice)

- [ ] Slice's T-matrix green (byte-identical native==oracle on landed routes; every decline falls to Python and matches; for slice a: SEQ op actually EXECUTES, key-decline, sub-floor counts via transport, fail-closed).
- [ ] Full text_mask + text_redact suites green; no masking test weakened; changed-lines coverage + mutation-proven (named mutants killed).
- [ ] `text_mask.py` not grown; `_sequential_native.py` under the cap; new modules within size policy; census updated if crossed; no circular import.
- [ ] Perf witness recorded (detection-vs-route, before/after rows/s per route, both cardinalities); diff + results to Slack; HELD for Cam's merge go.
- [ ] On merge: main CI green (CI + nightly testflight), ROADMAP + RECENTLY-SHIPPED updated (C6b-ii slice closed), graphify refresh (subject to the keyless blocker), Slack recap.

cam
