Status: plan (rev 1, DRAFT — not yet plan-gated)

Rules consulted: 00-universal, feature-dev, testing, risk-and-exceptions, development-loop, code-review, documentation; CLAUDE.md "use established methodology".

# C6a: format-preserving encryption (FF1) on the Rust/native execution paths

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C. Owner decision (Cam, 2026-10-07, `decoy-platform/docs/ROADMAP.md`): use the `fpe` crate, byte-matched to the Python oracle; per-row status codes mapped to the oracle's errors; warnings stay in Python. Preceded by the empty-string reference fix (DONE #218, `docs/plans/2026-10-07-fpe-reference-empty.md`). Branch `feat/c6a-fpe-native` off engine main `4a08f570`. Risk: **HIGH** (crypto + compiled companion), mitigated by an established crate, a byte-match-to-oracle gate, and no hand-rolled cipher. The crate choice is NOT reopened here; section 2 confirms it is sound and cites the source pattern per the methodology rule.

## 1. Goal and scope

A column whose strategy is `fpe` runs natively on the chunked and unified routes through a compiled Rust FF1 kernel, with output, per-row errors, the fail-closed kill, warnings and metrics identical to the pandas oracle.

**In scope:**
- A compiled `fpe` kernel in `decoy-engine-native` (encrypt + decrypt), using the `fpe` crate for the raw FF1 Feistel and porting the deployable-profile wrapper (charset resolution, pinned validation order, min-domain floor, printable-ASCII gate, `preserve_separators` reinsertion, `validate_luhn` body+check-digit) from `transforms/fpe.py`.
- Wiring as a native operator through the EXISTING machinery (section 3): registry spec, loader + ABI bump + load-time KAT self-test, preflight probe, `FpeParams`, `run_kernel_step` branch.
- Per-row FPE status codes mapped to the oracle's error taxonomy and the fail-closed `StrategyError` kill.
- Warnings computed in Python (reuse `FpeStrategyHandler._residual_risk_warnings`).
- **Step 0 (precursor, folded in):** align the reference's `pd.NA` raw-list handling so the oracle the kernel is graded against matches the shipped strategy (section 2e, decision 3b).

**Out of scope (declined to the oracle on the native path, with a coded reason, unchanged output):**
- **Checksum modes** (`checksum` set: luhn/npi/iban/vin/isbn13/ean13/gtin). Open question 1; recommended decline-to-oracle for this slice. They depend on python-stdnum (`checksums.py:1-65`); reproducing them in Rust is its own established-methodology survey + slice.
- Custom (non-preset) charsets drawing on the partial-plaintext / out-of-charset edge beyond what the preset charsets exercise, if the spike shows a parity gap; otherwise admitted.
- Any change to the shipped strategy, `unmask.py`, `transforms/_ff1.py`, or `transforms/fpe.py` value semantics. C6a changes no logical output.

## 2. Established facts (survey 2026-10-08; file:line)

**2a. The Python FPE oracle — where FF1 lives.**
- Raw NIST SP 800-38G FF1 Algorithms 5/6: `transforms/_ff1.py` (`encrypt`/`decrypt`, `_ROUNDS=10`, radix bound `FF1_STANDARD_MAX_RADIX=2**16`, `FF1_MIN_DOMAIN=1_000_000`, `min_domain_length`). Source pattern cited in its module docstring: a from-the-standard implementation, AES forward-only via the audited `cryptography` package.
- Deployable-profile wrapper: `transforms/fpe.py`. `_CHARSETS` (digits/alpha/ALPHA/alphanum/ALPHANUM) and `_CHARSET_INDEX`; `resolve_fpe_charset`, `check_charset_unique`, `check_charset_ascii_printable`; tweak wire format `build_ff1_tweak` (`VERSION||SCOPE||uint16 LEN||identity_utf8`, scopes COLUMN=0x01 / JOIN_GROUP=0x02); `_luhn_check_digit`; `_permute` enforces the pinned validation order 1-6 (key size, radix bound, alphabet uniqueness, body length vs `min_domain_length..FF1_MAX_LEN`, tweak length, body-symbol membership) before calling the primitive; `_fpe_value` orchestrates separators/empty-reject/out-of-charset fail-closed.
- Shipped strategy handler: `execution/_strategies/_fpe.py::run`. Null mask `source.isna().to_numpy()`; empty-string-as-missing policy; key `derive(mask_key, namespace, FF1_KEY_LABEL)`; tweak from column or `fpe_join_group`; **fail-closed kill**: the first `FpeUnencryptableError`/`FpeChecksumError` across the column raises `StrategyError` — the job dies, it does not survive with a row error. Residual warnings `_residual_risk_warnings` ride `ExecutionResult.warnings`, never the fingerprint.

**2b. Radix/alphabet, tweak, empty/null, error taxonomy.**
- Radix = `len(charset)`, deployed bound `[2, 64]`. Numerals are the char->index map. Printable-ASCII-only custom charsets so one code point == one symbol.
- Tweak derivation: `build_ff1_tweak`; join-group columns share ciphertext by keying the tweak on the group name.
- Empty string: non-null `""` is missing-data — written back as `""`, never ciphered, excluded from warnings (reference `_crypto_reference.py` `_run`, the #218 fix).
- Null: `source.isna()` in the strategy; `_is_missing` in the oracle (see 2e).
- Error taxonomy (the per-row codes C6a must reproduce), from `_crypto_reference.py::_ROW_ERROR_MESSAGES` + mapping `strategy_code_for_unencryptable`/`strategy_code_for_checksum`: `fpe_unencryptable_value`, `fpe_unencryptable_domain`, `fpe_unencryptable_length`, `fpe_checksum_unsupported`, `fpe_checksum_invalid_source`; plus config-level `fpe.config_invalid`/`FpeConfigError` and `fpe_charset_degenerate`.

**2c. The native execution seam (reuse; do not invent).**
- Operator registry `execution/_operator_registry.py`: one `OperatorSpec` per operator, keyed into `OPERATORS`. Every admission/evidence table is derived from it. FPE has **no spec today**, so fpe columns are not admitted and already run on the pandas oracle.
- Companion kernels + loaders: `native/_crypto_ext.py` (`load_compiled_crypto_kernel`, `_EXPECTED_ABI_VERSION="decoy-native-abi-2"`, load-time KAT self-test, `CryptoExtensionUnavailableError` fail-before-output). The compiled module is `decoy_engine_native._kernel`. `FpeKernel`/`FpeConfig`/`FpeBatchResult`/`FpeRowError` Protocols already exist in `_crypto_ext.py`; `_ReferenceFpe` already implements them. `load_compiled_crypto_kernel` deliberately does NOT load an FPE kernel yet ("it stays reference-only").
- Keyed-hash wiring is the pattern: `native/_kernels_keyed.py::native_keyed_hash` resolves config then forwards to the compiled `derive_batch`; `run_kernel_step` dispatches by `OperatorParams` type and returns `StepResult(out, ran, format_error_positions)`.
- Per-row error propagation (date_shift precedent): `format_error_positions` -> `RowError(trigger="format_error")` on the unified route and chunk-local via the `format_errors` channel on the chunked route, declared by `routed_diagnostics` in the date_shift spec. **FPE differs:** date_shift survives-and-reports; FPE fail-closed-KILLS on the first bad value. FPE therefore needs its own channel (decision 3d), not the date_shift survive channel.
- Preflight companion probe: `native/_dispatch.py` hash/index/group_key branches — each loads its kernel and `_downgrade_to_oracle(decision, "<kernel>_extension_unavailable")` on `CryptoExtensionUnavailableError`.

**2d. Byte-match contract mechanism.** Cross-language KAT corpus already exists for keyed derivation: `vectors/generate_kat.py` emits a JSON fixture consumed by the Rust `tests/kat_derive.rs` and the Python contract test. C6a adds an FPE corpus the same way, generated from the live `reference_fpe()`.

**2e. The `pd.NA` raw-list misalignment (prerequisite).** `_is_missing` (`kernel/_scalar.py:14-32`) returns **False** for `pd.NA` on the raw-list path: `bool(pd.NA != pd.NA)` raises `TypeError` ("boolean value of NA is ambiguous"), caught by `except Exception: return False`; `str(pd.NA)=="<NA>"` then reaches the cipher. The shipped strategy uses `source.isna()` (`_fpe.py`), which treats `pd.NA` as missing. On the Arrow fast path this is masked (`pa.array(from_pandas=True)` folds NA to null), but the raw-list oracle path diverges. The repo's own correct pattern is `pd.isna` (`storm/profiler.py:211`, `quality/carrier_adapter.py:193`).

**2f. Module sizes (600/700 LOC rule).** `_crypto_ext.py` 531, `_operator_step.py` 436, `_dispatch.py` 563, `_crypto_reference.py` 249, `_operator_registry.py` 220, `_operator_params.py` 273. The compiled FPE loader + `FpeConfig._resolve` additions and the Rust-kernel wiring must respect the ratchet; put the new loader/self-test and the `native_fpe` wrapper in their own modules (parallel to `_kernels_keyed.py`/`_index_ext.py`) rather than growing `_crypto_ext.py` past 600.

## 3. Decisions

**3a. Establish FF1-crate parity first (go/no-go spike).** Before any wiring, run the engine's `FPE_KAT` and the NIST SP 800-38G FF1 KAT corpus through the `fpe` crate's FF1 over the preset charsets (radix 2..64), lengths `min_domain_length..256`, and the engine tweak framing, and assert byte-identical ciphertext against `transforms/_ff1.py`. If it diverges, STOP and surface it (open question 2) — do not adjust the oracle to match the crate. Record the crate version, the source-pattern citation (NIST SP 800-38G FF1; the `fpe` crate wraps the audited `aes` crate), and the parity evidence in the kernel module's Rust docstring, per CLAUDE.md "use established methodology".

**3b. Step 0 — align the oracle's `pd.NA` handling (land first, with fail-before tests).** Make the FPE reference treat `pd.NA` as missing on the raw-list path, matching `source.isna()`. Preferred: fix at the shared source `_is_missing` (`kernel/_scalar.py`) with an explicit `pd.NA`-aware check that does not import pandas on the Arrow hot path, so every reference kernel aligns; alternatively scope the normalization to the FPE reference loop if the broader change is deferred. This is a reference/oracle divergence fixed at the source (testing.md), landed with its own failing-first test before the Rust kernel exists.

**3c. Register the operator (reuse the registry).** Add an `OperatorSpec(strategy="fpe", operator_id="native_fpe", ...)` to `_operator_registry.py`, string-only resident types, tokenizing full-frame assembly, required_kernel="fpe", positive kernel evidence, fail-closed routed diagnostics. Add an `FpeParams` to `native/_operator_params.py` resolving config exactly as `FpeConfig._resolve` / the handler (charset, preserve_separators, validate_luhn, checksum, join_group, namespace, column/tweak identity). The derived admission/evidence tables pick the operator up automatically.

**3d. Per-row errors and the fail-closed kill (FPE's own channel, not date_shift's).** The compiled kernel returns an `FpeBatchResult` equivalent: output array (None for a failed/null row, `""` for empty) plus an ordered tuple of `FpeRowError(row_index, code)`. `run_kernel_step`/`StepResult` gain an FPE error carrier distinct from `format_error_positions`. Both routes map a **non-empty** error set to a `StrategyError` kill whose code equals the **first** failing row's code (source order among non-null, non-empty cells), matching the oracle's first-failure raise and `strategy_code_for_*`. The kernel reproduces the pinned validation order (2a) so the SAME value yields the SAME code. Row-index attribution follows the date_shift precedent (chunk-local on the chunked route; batch-rebased once on the unified route) for the carried index, but the observable kill is one `StrategyError`, not surviving `RowError`s.

**3e. Warnings stay in Python.** The native route computes warnings via `FpeStrategyHandler._residual_risk_warnings` plus the `fpe_join_group_active` warning, over the same non-null, non-empty value set the oracle sees, on both routes. The Rust kernel computes no warnings. Warnings ride `ExecutionResult.warnings`, never the output, so the determinism fingerprint is unchanged.

**3f. Loader, ABI, fail-before-output.** Add `load_compiled_fpe_kernel` (own module) paralleling `load_compiled_crypto_kernel`: import `decoy_engine_native._kernel`, check ABI, run an FPE KAT vector through the entry point at load, return a thin wrapper, else raise `CryptoExtensionUnavailableError` before any output. Bump `_EXPECTED_ABI_VERSION`/`ABI_VERSION` abi-2 -> **abi-3** (new FPE entry points on the companion). Add a preflight probe branch in `native/_dispatch.py` that `_downgrade_to_oracle(decision, "fpe_extension_unavailable")` when the companion is absent/incompatible — so a missing companion runs the **whole table on the pandas oracle**, never half-native.

**3g. Checksum-mode decline (pending open question 1).** For this slice, an fpe column with `checksum` set is declined to the oracle with a coded reason (`fpe_checksum_not_native:<col>`), output unchanged. If Cam chooses in-Rust checksum support instead, it is a separate established-methodology survey + KAT-lock against python-stdnum.

**3h. Logs.** No key material, no cell values in logs or errors (the redacted-error contract; `FpeRowError.message` never embeds the value). Sentry unchanged.

## 4. Acceptance tests (written first; never weakened)

Differential = native lane-on vs an explicit lane-off (pandas oracle) run on the SAME route, comparing: output tables byte-equal incl. schema and `b"pandas"` metadata; warnings (codes, counts, fields); the fail-closed outcome (same `StrategyError` code, or same survival) and any row indices; metrics excluding timings and the activation leaf. Admitted cases poison the oracle fallback so any silent reroute fails.

1. **FF1-crate parity gate (3a):** `FPE_KAT` + NIST FF1 KAT byte-identical between the `fpe` crate and `transforms/_ff1.py` across preset charsets, radix 2..64, lengths `min_domain_length..256`, and tweak framing. This gate blocks the rest of the build.
2. **Byte-match matrix, both routes:** each preset charset and a representative printable-ASCII custom charset; `preserve_separators` on/off; `validate_luhn` on digits; `fpe_join_group` set vs unset (grouped columns share ciphertext); several chunks/batches, ragged sizes, and an empty table. Encrypt and decrypt (round-trip restores originals).
3. **Null / empty / pd.NA:** nulls pass through as null; non-null `""` stays `""`, never ciphered, excluded from warnings; a raw-list column containing `pd.NA` (Step 0) produces output and warnings byte-identical to the oracle on both the Arrow and raw-list paths — fail-before proof that today's oracle mis-handles it.
4. **Error taxonomy / fail-closed parity:** for each of the 5 codes, a value that triggers it under the native kernel raises a `StrategyError` whose code equals the oracle's for the same column and same first-failing value, on both routes; a later failing chunk/batch with a nonzero base offset attributes the same code; the printable-ASCII gate, alphabet-uniqueness, radix-bound, min-domain-floor and out-of-charset (both `preserve_separators` branches) cases each match the oracle's code and message-free redaction.
5. **Warnings parity (stay in Python):** `fpe_partial_plaintext_disclosure` counts (affected/total) and `fpe_join_group_active` match the oracle exactly, including on an all-empty / all-null batch, on both routes.
6. **Companion-absent fallback:** with the FPE companion missing / ABI-mismatched / failing its load-time self-test, preflight downgrades the whole table to the oracle with `fpe_extension_unavailable`, output byte-identical to a pure-oracle run, no half-native output; `MaskKeyRequiredError` still fires before any row.
7. **Checksum decline (3g):** an fpe column with `checksum` set is declined with `fpe_checksum_not_native` and output equals lane-off, on both routes.
8. **Declines unchanged / registry-derived tables:** the new `fpe` spec flows into the admission and evidence tables; every other operator's admission is unchanged; existing fpe oracle behavior is unchanged where not admitted.
9. **Testflight:** STOP if any fingerprint moves.
10. **Sentries; mutation** on the validation-order branch, the per-row code mapping, the first-failure selection, and the Step-0 `pd.NA` check (each mutant killed); Rust `kat_fpe.rs` against the shared corpus; a **perf record** at 1M rows for encrypt and decrypt.

## 5. Failure modes

| Risk | Closed by |
|---|---|
| `fpe` crate FF1 diverges from `transforms/_ff1.py` | 3a spike + test 1 as a blocking gate; divergence is surfaced, not papered over (open question 2) |
| Per-row code or fail-closed kill mismatched to the oracle | 3d first-failure code selection; test 4 |
| `pd.NA` mishandled, oracle silently wrong | 3b fix-at-source + test 3 fail-before |
| Checksum math rolled by hand in Rust | 3g decline for this slice; open question 1 to Cam; test 7 |
| A missing companion emits half-native output | 3f fail-before-output preflight; test 6 |
| Warnings drift or move the fingerprint | 3e Python-only warnings on `ExecutionResult.warnings`; tests 5, 9 |
| Key material or cell values in logs/errors | 3h redacted-error contract; sentry |
| Module-size ratchet breached | 3c/3f new modules; `tests/sentry/test_module_size.py` |

Rollback: revert the merge commit; the ABI bump means a stale abi-2 companion is rejected at load and the table runs on the oracle, so rollback is safe with any companion build.

## 6. Review log

- rev 1 (DRAFT): initial plan from the 2026-10-08 survey (Opus Plan subagent + Opus review). Default scope declines checksum modes to the oracle (3g). Open questions carried to the Codex plan gate and parked for Cam: (1) checksum modes in Rust vs decline-to-oracle for C6a [parked — scope expansion, default is decline]; (2) `fpe`-crate FF1 byte-parity is a go/no-go spike, the first build gate; (3) the `pd.NA` alignment ships folded into C6a as Step 0 (adopted).
