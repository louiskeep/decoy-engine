Status: plan (rev 1, DRAFT - pending Codex plan gate)

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, testing, documentation; CLAUDE.md "use established methodology".

# C6b-i: admit text_mask to the native routes as ARROW_PYTHON

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C. Owner decision (Cam, 2026-10-10): C6b ships in two steps mirroring C6c (C6c-i ARROW_PYTHON admission, then C6c-ii the Rust kernel). This is **C6b-i**: admit text_mask to both native routes as an `ARROW_PYTHON` operator (the oracle's own `mask_cell` run per cell), so a text_mask column no longer forces its whole table onto the pandas oracle. **No Rust kernel** (that is C6b-ii). Branch `feat/c6b-i-text-mask-arrow` off engine main `af308869`. Risk: **MEDIUM** (routing/admission only; no new kernel, no crypto; output is the oracle's own `mask_cell`, byte-identical by construction).

## 1. Goal and scope

Today a text_mask column has no `OperatorSpec`, so it is never native-admitted and the whole table (every sibling column: hash, faker, categorical, ...) is downgraded to the pandas oracle (`_dispatch._static_route_decision` atomic whole-table decline). C6b-i registers text_mask as an `ARROW_PYTHON` operator: the native route keeps running, text_mask cells are masked by calling the shipped oracle `mask_cell` per cell (byte-identical), and the siblings stay native. Per-cell text_mask throughput is unchanged; the win is the table no longer leaving the native route.

**In scope (mirror C6c-i's text_redact admission exactly):**
- An `OperatorSpec(strategy="text_mask", operator_id="native_text_mask", shape="kernel", planned_backend=ARROW_PYTHON, required_kernel=None, positive_kernel_evidence=False, unified_resident_types=_STRING_ONLY, full_frame_assembly="null_on_empty")` in `_operator_registry.py` (text_redact's spec `:155-164` is the template; text_mask's oracle also assigns an object column, so empty/all-null -> Arrow null).
- A `native_text_mask` wrapper (parallel to `native_text_redact` in `native/_kernels_scalar.py`) that calls the shipped `transforms/text_mask.py::mask_cell` per cell via the handler's resolved config - the SAME code the oracle runs, so output is byte-identical with no kernel.
- A `text_mask_config_rejection` (parallel to `text_redact_config_rejection`, `_operator_config_rejections.py:270`) that declines a column per-column with a coded reason `text_mask_ner_not_native:<col>` when `ner` is configured - mirroring text_redact, because the NER path is spaCy model-availability + version-guarded and stays on the oracle. A declined column downgrades the whole table to the oracle (existing mechanism), output unchanged.
- The chunked string-source gate (`native/_real_type_admission.py`) + the unified string resident-type gate: text_mask `str()`-converts non-string cells, so admit only string sources (as text_redact does); non-string declines to the oracle.
- Both routes: unified binding (`physical/_shadow_bindings.py`) + chunked dispatch, as text_redact wires them.

**Out of scope:**
- The Rust deterministic-span kernel + the Faker-synthesis / NER-span decline to Rust: that is **C6b-ii** (after). C6b-i's ARROW_PYTHON path runs the FULL `mask_cell` in Python, including the fpe/date_shift/faker span branches, so it handles them correctly already; only `ner`-configured columns decline (model availability), matching text_redact.
- Any change to `mask_cell` behavior, the span detectors, warnings, or output. C6b-i changes routing only.

## 2. Established facts (survey + 2026-10-10 reads; file:line)
- text_redact's ARROW_PYTHON `OperatorSpec` (`_operator_registry.py:155-164`): `shape="kernel"`, `planned_backend=ARROW_PYTHON` (`:43`), `required_kernel=None`, `positive_kernel_evidence=False`, `unified_resident_types=_STRING_ONLY`, `full_frame_assembly="null_on_empty"`. The derived admission/evidence/assembly/backend tables all flow from the spec.
- `native_text_redact` (`native/_kernels_scalar.py:133-191`) is the ARROW_PYTHON caller: it runs the oracle span path per cell and falls back fully to Python; C6b-i's `native_text_mask` is the direct analogue, calling `mask_cell`.
- `text_redact_config_rejection` (`_operator_config_rejections.py:270`) returns `text_redact_ner_not_native:<name>` when `ner` is set; wired into the admission rejection set. C6b-i adds the text_mask analogue.
- text_mask handler (`_strategies/_text_mask.py`): reads `provider_config` knobs `detectors`, `per_detector_strategy`, `unmatched_span_policy`, `token`, `sub_floor_span`, `ner` (`:66-99`); `str()`-converts non-string cells (string-source gate needed); masking is `transforms/text_mask.py::mask_cell`. Faker-default detectors (person_name/location/...) are NER-only (Tier-2), unreachable without `ner`, so a non-`ner` column has no faker spans on the built-in path; and even when a span routes to faker, `mask_cell` runs it in Python on the ARROW_PYTHON path.
- text_mask already streams memory-bounded on the pandas CHUNKED route (P4 slice 3, `_chunked_text_mask.py`); C6b-i adds the NATIVE-route admission, not a new masking path.
- Whole-table atomic decline today: a column with no `OperatorSpec` -> `no_native_kernel:<col>:text_mask` -> `_downgrade_to_oracle` whole table (`native/_dispatch.py`). C6b-i removes that for admissible text_mask columns.

## 3. Decisions / method
3a. Register `text_mask` as ARROW_PYTHON (2's spec), modelled byte-for-byte on text_redact; update the registry-totality + operator-tables snapshot sentries.
3b. `native_text_mask` calls the shipped `mask_cell` (same config resolution as the handler) per cell - no kernel, byte-identical to the oracle; carries text_mask's warnings exactly as the oracle does (the ARROW_PYTHON operator returns the same `ExecutionResult` shape; warnings ride `ExecutionResult.warnings`, never the output).
3c. `text_mask_config_rejection` declines `ner`-configured columns (`text_mask_ner_not_native:<col>`), mirroring text_redact; everything else admits. (C6b-ii will add the faker-span/Rust-kernel declines; not here.)
3d. String-source gate on both routes (text_mask str()-coerces non-strings); non-string source declines whole-table to the oracle.
3e. Degenerate schema: `full_frame_assembly="null_on_empty"` (oracle assigns object -> empty/all-null is Arrow null), matching text_redact; no string-pin.
3f. Logs/behavior unchanged.

## 4. Acceptance tests (written first; never weakened)
Differential = native lane-on (ARROW_PYTHON) vs an explicit lane-off (pandas oracle) on the SAME route; output byte-equal incl. schema + `b"pandas"` metadata; warnings (codes/counts/fields); metrics excl. timings. Admitted cases poison the oracle fallback so a silent reroute fails.
1. **Byte-match both routes:** text_mask over free text (default config; a `per_detector_strategy` routing a built-in detector to fpe/date_shift/redact; `unmatched_span_policy` variants; `sub_floor_span`) - native ARROW_PYTHON == oracle, chunked + unified, several chunks/ragged, empty table, nulls.
2. **Table stays native (the point of the slice):** a table with a text_mask column AND sibling hash/categorical/faker columns - the siblings run native, text_mask runs ARROW_PYTHON, and the table is NOT whole-downgraded to the oracle (assert `native_admitted` + the sibling operators' native evidence).
3. **ner decline:** an `ner`-configured text_mask column declines whole-table with `text_mask_ner_not_native:<col>`, output == oracle, both routes.
4. **Non-string source decline:** a numeric/dict-encoded/large_string source declines to the oracle, both routes.
5. **Degenerate schema:** empty / all-null text_mask output is Arrow null, matching the lane-off oracle, both routes.
6. **Declines-unchanged / registry-derived:** the new spec flows into admission/evidence/backend tables; every other operator's admission unchanged; existing text_mask oracle behavior unchanged where not admitted.
7. **Testflight:** STOP if any fingerprint moves (warnings ride ExecutionResult, output unchanged).
8. **Sentries** (registry totality, operator-tables snapshot, module size); a 1M-row routing record (native-admitted table with a text_mask column vs today's whole-oracle) to show the table-stays-native win.

## 5. Failure modes
| Risk | Closed by |
|---|---|
| ARROW_PYTHON path diverges from the oracle | 3b runs the SAME `mask_cell`; test 1 byte-match both routes |
| ner column silently runs native (model/version hazard) | 3c config-rejection; test 3 |
| Non-string source mis-typed | 3d string-source gate; test 4 |
| A text_mask column still drags its table to the oracle | 3a admission; test 2 asserts siblings stay native |
| Warnings drift / move the fingerprint | 3b warnings on ExecutionResult only; tests 1, 7 |
| Registry/snapshot sentries drift | 3a updates them; test 6 |

Rollback: revert the merge; without the spec text_mask returns to whole-table oracle decline (today's behavior).

## 6. Review log
- rev 1 (DRAFT): Opus-authored from the C6b framing survey + the C6c-i/text_redact precedent reads (2026-10-10). Pending Codex plan gate. C6b-ii (the deterministic-span Rust kernel, declining Faker + NER spans) is the follow-on slice, after this.
