Status: plan (revision 3, author = Opus). Codex plan gate: rounds 1 and 2 REVISE folded; round 3 (final before escalation) pending.
Rules consulted: 00-universal, development-loop, testing, architecture, code-review, scope-discipline

# C6c-i: text_redact as an Arrow operator on both native routes

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C, C6c ("text_redact on the Rust paths"), staged:
- **C6c-i (this plan):** admit text_redact to the unified full-frame route and the chunked native route as an `ARROW_PYTHON` operator. It reuses the oracle's own span detection and splice, so parity holds by construction.
- **C6c-ii (later):** a Rust span kernel. It needs a regex-engine decision first (the detectors use lookarounds, which the `regex` crate lacks) and a code-point-offset contract.

Branch `feat/c6c-i-text-redact-arrow` off engine main `2adc4d52` (R1 and R1b merged). Risk R2: one operator added to two routes, unkeyed, parity-gated, no new kernel.

## 1. Goal and scope

Today any table with a text_redact column is declined in full to pandas on both native routes, because text_redact has no `OperatorSpec` (`execution/_operator_registry.py:78-176`) and so is outside `NATIVE_KERNEL_STRATEGIES` (`native/_dispatch.py:280-300`). Free-text clinical columns are common, so one such column costs every other column in the table its native path (rust-coverage audit, `docs/records/2026-09-30-rust-coverage-evidence-audit.md`).

C6c-i admits the plain configuration:
- no `ner`;
- `token` a string;
- `detectors` absent, explicitly `null`, a list or a tuple;
- string source;
- no `when:`.

The text_redact column itself runs the same Python per-cell span logic as the oracle, so its own speed is unchanged. The gain is that the rest of its table stays native. Evidence reports it honestly as `arrow_python`, not compiled.

Out of scope:
- The Rust span kernel (C6c-ii).
- `ner` configs (model load, version check, batching). They stay on the oracle with a coded reason.
- The two silent pass-through configs (non-string `token`; `detectors` neither None/null nor a list or tuple). They leave the source column unchanged with its type, so they stay on the oracle with a coded reason.
- text_mask (C6b) and FPE (C6a).
- Out-of-core, which already runs text_redact in group (b).

## 2. Established facts (main `2adc4d52`)

**Oracle `TextRedactHandler.run`** (`_strategies/_text_redact.py:56-188`):
- `token = cfg.get("token", _DEFAULT_TOKEN)`. A non-string token returns the frame unchanged (`:68-69`).
- `label_token = bool(cfg.get("label_token", False))`.
- `detectors`:
  - None means all detectors.
  - A list or tuple is `[str(d) for d in detectors] or None`, so an empty list means all (`:117-123`).
  - Anything else returns the frame unchanged (`:124-125`).
- NER branch when `ner` is truthy (`:76-112`, `:160-184`).
- Column handling:
  - extension dtypes become `object`, otherwise the column is copied;
  - nulls come from `col.isna()` (None, nan, pd.NA, pd.NaT);
  - a non-string non-null cell becomes `str(cell)`.
- Per cell, `spans = iter_spans(text, detector_ids, extra_spans=None)`, and the output is `text` if there are no spans, else `_splice(text, spans, token, label_token)` (`:146-151`).
- Output is an `object` Series. There are no warnings and no row errors.
- `iter_spans` (`storm/detectors.py:983`) runs unknown detector ids as no-ops and returns sorted, non-overlapping spans (leftmost-longest).

**Metadata:**
- Capability row `text_redact` declares zero diagnostics (`native/_capabilities.py:246`), so unified admission needs no new routing.
- Determinism class `DETERMINISTIC_NO_DRAW` (`native/_determinism_protocol.py:808`).

**Chunked:** text_redact is in `CHUNK_SAFE_STRATEGIES` (`_chunked_fk.py:86`). The source-dtype gate exists only on the auto route (`_planner.py:447`). `_chunked_group_key.py:153-161` already models text_redact's pass-through vs stringifying behavior for group_key siblings.

**Native template:** `native_redact` and `native_truncate` (`native/_kernels_scalar.py`) are the existing `ARROW_PYTHON` operators. Their `OperatorSpec` has `shape="kernel"`, `planned_backend=ARROW_PYTHON`, `required_kernel=None`, `positive_kernel_evidence=False`, `unified_resident_types=_STRING_ONLY` and `full_frame_assembly="tokenizing"`. The chunked string pin `_STRING_OUTPUT_STRATEGIES = {hash, truncate, redact}` (`native/_chunked_schema_rule.py:49`) guards the degenerate-chunk types.

**R1b:** both routes call `native/_operator_step.py::run_kernel_step` with params from `native/_operator_params.py::resolve_operator_params`. A new operator is one registry entry, one params class with its resolver branch, one step branch, and only the route contracts it needs.

## 3. Decisions

**3a. Kernel.** New `native_text_redact(array, *, detectors: tuple[str, ...] | None, token: str, label_token: bool) -> pa.Array` in `native/_kernels_scalar.py`, or a sibling module if the scalar module would pass 600 lines.
- It receives `detectors` already normalized by the resolver (3b), which is the ONE owner of the normalization. The kernel passes `list(detectors) if detectors is not None else None` and does not re-apply the empty-means-all rule, so a resolver mutant that drops that rule is observable.
- It iterates the Arrow array's Python values with the oracle's exact cell rules:
  - null stays null;
  - a non-string cell becomes `str(cell)`;
  - otherwise it calls `iter_spans(text, list(detectors) if detectors is not None else None, extra_spans=None)` (an empty tuple stays an empty list, which runs zero detectors, as `storm/detectors.py:1014` does) and `_splice`.
- It imports `iter_spans`, `_splice` and `_DEFAULT_TOKEN` from their current homes. They are NOT copied, so there is one implementation. No import-direction sentry forbids this (Codex round 1), so `_splice` is not moved; it keeps its out-of-core consumer.
- Output is `pa.string()` from the kernel. Each route's assembly decides the final type (3d).
- The oracle's per-cell `str()` of a non-string value only matters for non-string sources, which admission excludes (3c). The kernel still applies the same rule so it is correct if called directly.

**3b. Registry and params.**
- One `OperatorSpec`: `strategy="text_redact"`, `operator_id="native_text_redact"`, `shape="kernel"`, `planned_backend=ARROW_PYTHON`, `required_kernel=None`, `positive_kernel_evidence=False`, `unified_resident_types=_STRING_ONLY`, `full_frame_assembly="null_on_empty"`, no `routed_diagnostics`. Codex round 1: the oracle assigns `dtype=object` (`_text_redact.py:187`), so an empty or all-null result is Arrow `null`, which `null_on_empty` reproduces; `tokenizing` would give `double` (`physical/_shadow_assembly.py:66`).
- `TextRedactParams(detectors: tuple[str, ...] | None, token: str, label_token: bool)`. The resolver applies the oracle's exact normalization once: `token = cfg.get("token", _DEFAULT_TOKEN)`; `label_token = bool(cfg.get("label_token", False))`; `detectors = tuple(str(d) for d in raw) or None` when raw is a list or tuple, and None when raw is absent or explicitly None (`_text_redact.py:115`).
- The step branch returns `StepResult(native_text_redact(...), None)`, since it is not a compiled kernel.
- **Unified binding and adapter (Codex round 2 HIGH).** R1b's unified adapter validates unkeyed operators against `_UNKEYED_PARAMS` (`physical/_shadow_operators.py:143`) and rejects any other id before the step (`:250-251`). Without an entry, an admitted text_redact table would raise `UnifiedSliceInvariantError` (`_unified_slice.py:258-277`). So:
  - add the registry-derived `native_text_redact` id mapped to `TextRedactParams` to `_UNKEYED_PARAMS`;
  - add `TextRedactParams` to the unkeyed narrowing in `_shadow_bindings._key_binding` (`:155`), so the binding carries no `KeyBinding`;
  - add the operator-id constant in `physical/_shadow_operators.py` alongside the other nine, read from the registry.
- The R1b resolver single-source sentry is extended to cover the text_redact default token.

**3c. Admission (both routes), one config predicate.** `text_redact_config_rejection(name, provider_config) -> str | None` in `native/_operator_config_rejections.py`:
- `text_redact_ner_not_native:{name}` when `ner` is truthy;
- `text_redact_token_not_string:{name}` when token is present and not a str;
- `text_redact_detectors_malformed:{name}` when detectors is present, not None, and not a list or tuple.

It is wired into BOTH config-gate dispatchers, alongside redact and truncate:
- `_requirements.py` (the fallback policy every route reads);
- `native/_plan.py::_config_rejection` (`:308`, the native eligibility report). Without it, eligibility would advertise excluded configs as eligible once the registry entry exists (Codex round 1).

Where the codes surface (narrowed to existing plumbing, Codex round 1):
- The coded reason appears in the `_plan.py` eligibility report, exactly as redact's and truncate's do.
- The chunked dispatcher records the generic `fallback_policy_not_native:{col}:{policy}` (`_dispatch.py:305`), like every other operator.
- Unified admission declines without a code (`_unified_slice_admission.py:204`), as it does for every operator today.

No new telemetry path is built in this slice. The source must be `pa.string()`: unified through `unified_resident_types`, chunked through the existing first-chunk real-type check, adding text_redact to the string-source gate with code `text_redact_source_type_not_string:{col}:{type}` in the chunked downgrade evidence. The unified resident-type and `when:` gates decline without a code, as they do today. A non-string source takes the existing downgrade to the chunked-oracle leg, never a hard error. `when:` is already rejected on both native routes by the generic `when_predicate_not_native` path; the builder confirms it with a test.

**3d. Output type on the chunked route.** Add text_redact to the string pin only when its config passes 3c. A classifier (`text_redact_pinned_columns`, like `date_shift_pinned_columns`) runs inside `build_schema_rule`, so both legs and the streamed sink agree. A rejected config keeps its oracle types.
- For admitted text_redact, both legs pin `string` per chunk.
- Unified route: the `null_on_empty` assembly makes an empty or all-null result Arrow `null`, equal to the oracle; otherwise `string`.
- Chunked route: assembled output is `string`, which equals whole-frame except for an entirely null or empty column (whole-frame gives `null`). This is the same documented exception redact and truncate already carry on the chunked route.

**3e. Evidence.** This matches redact and truncate exactly:
- Planned and executed backend `arrow_python`.
- No compiled claim: `compiled_kernel_executed` stays False.
- No `kernel_idle` entry, since positive evidence is not expected for `ARROW_PYTHON`.
- The ordinary call counters still count: the chunked `kernel_calls` increment for an unkeyed step (`_chunk_masking.py:171-172`) and the unified `batches_run` (`_shadow_operators.py:329`). These record operator calls, not compiled work (Codex round 2).

**3f. Docs.**
- CHANGELOG.
- Compatibility contract: native admission for text_redact with the excluded configs listed.
- Rust-coverage audit record: mark R011/R012 (or the text_redact rows) as partially lifted.
- Capability docs, if they list native operators.
- Roadmap: C6c-i shipped, C6c-ii (Rust span kernel) planned.

## 4. Design notes

- **Why Arrow+Python before Rust.** The text_redact column's cost is unchanged, but the table-wide veto disappears. The Rust kernel can then replace one function behind the same params and step branch without touching admission or evidence again.
- **One implementation of the span logic.** The kernel calls the oracle's `iter_spans` and `_splice`, so a detector change can never make the routes disagree. C6c-ii's differential corpus will use the same functions as its reference.
- **Admission is config-only plus source type.** Every excluded config has a coded reason, so evidence says why a table stayed on the oracle.

## 5. Acceptance tests (written first; red-before recorded)

1. **Parity matrix, unified and chunked, against the oracle.**
   - Configs:
     - default;
     - `detectors` of one, several, an empty list (means all) and an unknown id (a no-op);
     - a custom `token`;
     - `label_token` true and false.
   - Text corpus:
     - every built-in detector hit;
     - overlapping candidates (leftmost-longest);
     - adjacent spans;
     - spans at the start and end of a cell;
     - no spans;
     - multi-byte Unicode before and inside spans;
     - the empty string.
   - Null kinds in the source: None and pd.NA via Arrow nulls.
   - Chunk shapes: zero-row, all-null, single-row, ragged.
   - Values and order equal the oracle. Types follow 3d, with explicitly string-typed empty and all-null inputs.
   - Every admitted case ALSO asserts native-route evidence (`arrow_python` backend for the column, table admitted). Without it, a fallback to the oracle would pass the parity check vacuously.
   - Configs also include `detectors: null` and a tuple (both admitted).
2a. **Unified execution end to end (Codex round 2 HIGH).** A real unified-slice pipeline run, through admission, the coordinator and assembly with the oracle poisoned, for:
   - a text_redact-only table;
   - a mixed hash and text_redact table;
   - the text_redact-only table again with the compiled companion ABSENT (text_redact needs none).
   Each completes without `UnifiedSliceInvariantError`, equals the oracle, and reports `arrow_python` for the text_redact column.
2. **Table-level lift.** A table with hash, faker and text_redact columns now takes the native route on both routes. Before the change it is declined in full. Evidence names `arrow_python` for text_redact and the compiled backends for the others.
3. **Excluded configs stay on the oracle.** `ner: true`, `ner: {model: ...}`, a non-string token, and a malformed (non-None, non-list) detectors value each:
   - give the exact 3c code in the `_plan.py` eligibility report;
   - give `fallback_policy_not_native` in chunked evidence;
   - decline unified admission;
   - produce output equal to today's.
4. **Non-string source.** An int64 source with text_redact takes the oracle (unified decline; chunked downgrade with `text_redact_source_type_not_string`) and equals today's output. The auto-router end to end does not crash.
5. **`when:`.** text_redact with `when:` stays off native via the existing code. Output is unchanged.
6a. **Direct kernel contract.** `native_text_redact` on non-string arrays (int64, float64, bool) applies `str()` per non-null cell. Nulls stay null. `detectors=None` runs all detectors; `detectors=()` runs none (a cell with a known hit stays unchanged). Separately, the public config `detectors: []` redacts that same hit (empty means all, owned by the resolver). This makes the `str()` and normalization mutants observable without going through admission.
6. **Single implementation.** An AST check confirms that `native_text_redact` calls `iter_spans` and `_splice` and defines no regex of its own. The resolver sentry covers the default token.
7. **Evidence.** `arrow_python` backend, `compiled_kernel_executed=False`, ordinary call counters as redact's, absent from `kernel_idle`.
8. **Determinism.** Two runs give identical output. It does not depend on `mask_key` or `job_seed`: changing either leaves the text_redact output unchanged.
9. **Mutation.**
   - Required mutants:
     - `label_token` dropped;
     - the empty-list-means-all rule dropped;
     - the null check inverted;
     - the `str()` coercion dropped;
     - the 3c gates removed one at a time;
     - the pin classifier admitting rejected configs. Killed by a rejected config (non-string token) over an int64 source run chunked: the oracle returns int64 unchanged, so a wrong pin changes the observable type.
   - Any mutant shown equivalent is recorded as such, with the reason.
   - All must be killed. Record the results.

Red-before: tests 1, 2, 2a, 3, 6, 6a, 7 and 9 fail on the base (no operator, no codes). Tests 4, 5 and 8 are green-before for the oracle path and must stay green.

Every new test also runs under the Python 3.10 mirror.

## 6. Risk, rollback, gates

| Risk | Mitigation |
|---|---|
| Output type differs from the oracle on degenerate chunks | Config-gated pin through `build_schema_rule`; tests 1 and 3 |
| Silent pass-through configs admitted and stringified | Coded rejections; test 3 |
| Python per-cell speed mistaken for a Rust win | Evidence says `arrow_python`; docs say the column's own speed is unchanged |
| Module size | New code is small; census exact |

Rollback: revert the merge commit. text_redact goes back to declining its table to the oracle.

Gates: Codex plan gate, Sonnet tests-first build, dennis, Codex final gate, ci-mirror, merge under the standing authority, the post-merge suite including `tests/perf`, and a main CI check.

## 7. Plan-gate history

- Rev 1: initial.
- Codex round 1, REVISE (4 MEDIUM). Folded in rev 2:
  - Unified assembly is `null_on_empty`, matching the oracle's object dtype.
  - `detectors: null` is admitted.
  - The `_plan.py` eligibility dispatcher is wired, and where codes surface is narrowed to existing plumbing.
  - Normalization has one owner, with direct kernel tests, native-route evidence in every parity case, and an observable pin mutant.
  - `_splice` is not moved.
- Codex round 2, REVISE (1 HIGH, 1 MEDIUM, 1 LOW). Folded in rev 3:
  - H: the unified adapter's `_UNKEYED_PARAMS` and the binding narrowing are in scope; an end-to-end unified run (test 2a), with and without the companion, was added.
  - M: the kernel keeps an empty tuple empty; only the resolver applies empty-means-all.
  - L: evidence wording keeps the ordinary call counters.
