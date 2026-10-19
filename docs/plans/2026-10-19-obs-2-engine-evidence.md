Status: plan (OBS-2 engine evidence emission. rev 1. The consolidated engine half of OBS-2 job-log completeness. Authored after the OBS-2a build correctly STOPPED on a plan defect: the existing `quality_metrics["unified_slice_activation"]["nodes"]` evidence carries operator/backend/calls but NO structured table/column/strategy, and the only column signal is the composite `node_id` which cannot be safely split (names contain `:` and `+`, and a node can cover several columns). So 2a is NOT platform-only; it needs a small additive engine field. That makes all four OBS-2 evidence sub-slices engine-emission items, which Cam approved consolidating into ONE engine PR + ONE pin bump + ONE platform-consume PR to cut gate/pin cycles. Base: engine `origin/main` @ da9623b7 (post OBS-1b #289). Companion platform plan to be revised after this plan gates: `decoy-platform/docs/plans/2026-10-19-obs-2-joblog-completeness.md`. Program plan: `decoy-platform/docs/plans/2026-10-19-observability-program.md` §2 OBS-2. Merge authority: Phase-F bar (dennis + Codex final + local CI; GitHub CI ignored). R2 change: crosses privacy + schema + compatibility boundaries, so Codex is the final gate. Design spec: `docs/guides/logging.md` §4 and `docs/security/error-reporting-and-data-exposure.md`.)

Rules consulted: dev-rules `00-universal.md`, `development-loop.md` (OBS-0 done-state), `observability-and-resilience.md` (log location + type, never value), `risk-and-exceptions.md` (R2), `root-cause-not-patch.md`, `plan-defines-acceptance-tests.md`; engine `CLAUDE.md` (module size 600/700, physical-seam sentry, established methodology, pre-GA hard delete).

# OBS-2 engine evidence emission (consolidated)

## rev 2: Codex plan-gate NO-GO and reshape

Codex gated rev 1 NO-GO with 11 findings (7 HIGH, 4 MEDIUM). The gate confirmed 2a is sound (`PhysicalNode` carries `table`/`columns`/`strategy`; `physical/_compiler.py:343` concatenates unescaped names into `node_id`, so splitting it is unsafe) and that excluding `reason` from the row-error summary is right. It found 2b/2c/2d materially under-specified and partly wrong. This splits the slice into one buildable piece and a design agenda.

2a: BUILD NOW. Only finding against it is MEDIUM #8, a plan-text correction: existing exact-shape evidence fixtures must be authorized to gain the `table` field and the three literal keys (keep exact equality, never swap to subset). Folded into §2. Build proceeds; merge held for Cam's shape steer (see below).

2b/2c/2d: DESIGN FIRST, do not build. The gate agenda to resolve before a rev 3 re-gate:
- 2d (#1): `kernel_calls` counts strategy-branch executions (including successful Faker and some idle/Arrow branches), NOT compiled-kernel invocations. The "compiled-only / absence-equals-zero" claim is wrong. Preserve the counter's actual behavior; document the real rules; assert explicit expected counts (active Faker, Arrow-only, idle, all-false `when`). Cumulative table/per-strategy scope is correct.
- 2b (#2): the decline graph has ~8 sources (static admission, group-key, preflight, Faker admission, resolved pools, real-type, `when`, companion probes), not the 5 `reasons.append` sites. The named `_not_native` helper is actually `_oracle_evidence`. Build the complete source-to-code matrix with attribution and precedence; give distinct causes specific codes; stable serialized values; reserve `OTHER` for genuinely unknown input only.
- 2b (#3): attribution must be built where column and decision are known (the classifier), not at the generic routing helper `_downgrade_to_oracle` (which erases column attribution). Define `table_wide` as causal scope, independent of the table-wide routing consequence. Specify preservation/ordering of earlier records when a `when` decline overrides `reroute_reason`.
- 2b/2d (#4, the central gap): the emission BOUNDARY is unspecified. `kernel_calls` lives on `NativeRouteEvidence` (mutable internal), not the public `quality_metrics["chunked_route"]` payload; adding fields there does not reach the result. Name the exact destination keys and primitive serialization, and specify propagation through native and oracle chunk results, resident aggregation, streamed accumulation, multi-table metrics, and isolated serialization. For counters, copy each cumulative snapshot and keep the latest when folding; never sum snapshots or alias a mutable dict.
- 2c (#5): `summarize_row_errors` needs an explicit table and single-table validation, with a separate grouping step for multi-table results (results and exceptions can hold several tables; empty records cannot identify one). Pin sample order, a default/hard sample bound, zero/invalid-limit behavior; bound the count map by schema columns and the closed trigger vocabulary.
- 2c (#6): chunk identity is not on `RowErrorRecord` or `RowErrorsFailedError`. Summarize each chunk while its index and absolute offset are available, attach before raising, and aggregate retaining per-sample identity. Both routes stop at the first error-bearing chunk, so cross-chunk repeated positions need a unit aggregation fixture, not a pipeline scenario.
- 2c (#7): completeness describes observed coverage, not job outcome. Post-mask full-frame enforcement raises with only uncovered errors, so a summary from `.records` can undercount. Define a producer/state matrix (full-frame completion, post-mask policy failure, chunk failure, premature termination, isolated noncompletion); capture observed records before policy filtering when promising an exact total; distinguish exact observed total from total errors in unprocessed input.
- #9: acceptance wording must permit exact schema identifiers only in their designated fields (the leak sentry does not cover arbitrary dataclass payloads); assert exact serialized key sets and inject distinct provider/expression/exception/reason/unknown-trigger markers and require their absence; map unknown triggers to `other`.
- #10: `_chunked_entry.py` is 665 LOC and also needs wiring; B and D share carriers, so they are NOT independent commits as rev 1 claimed. Identify the extraction that frees headroom or the justified census change; record B to D sequencing. `_row_errors.py` is NOT in the physical-seam allowlist and must be added if edited.
- #11: parity needs a pinned baseline (flag-on/flag-off can share a new regression). Pin golden/oracle fixtures with explicit expected values; for 2d, independently count branches, not another projection of `kernel_calls`.

Shape decision pending Cam: consolidation was approved on the framing that all four were small additive emissions. That held for 2a only. 2b/2c/2d are a real multi-subsystem design. 2a is built and gate-validated as merge-ready (Codex plan-gate design SOUND, dennis GO, Codex final GO); whether it ships on its own (one pin bump now) or folds into the consolidated PR after 2b/2c/2d are designed (one pin bump later) is Cam's call. Surfaced on Slack; not blocking.

## rev 3: grounded 2b/2c/2d design

Grounded against the code (dossier, all line refs verified at this worktree). This supersedes the thin rev-1 §3/§4/§5 for build. 2a is unchanged (§2). Codex plan-gate on this rev is HELD for Cam's shape steer (do not fire the gate or build until Cam confirms consolidation-vs-split and that 2b/2c/2d proceed now).

### A. Shared foundation: the emission-boundary contract (resolves #4)

`kernel_calls` is a mutable field on `NativeRouteEvidence` (`native/_dispatch.py:133`), delivered once to the route-evidence sink then mutated per chunk. It does NOT reach the public result. There is no `NativeChunkEvidence` class. The PUBLIC payload is `quality_metrics["chunked_route"]`, a JSON-safe dict built PER CHUNK by `chunk_route_evidence` (`native/_chunked_evidence.py:133-178`) and folded across chunks by TWO folders that must stay in lockstep:
- resident: `aggregate_chunked_route_evidence` (`_chunked_evidence.py:181-236`)
- streamed: `_RouteFold` / `OutputEvidenceAccumulator.route_evidence()` (`_chunked_output_sink.py:191-307`)

Both SUM per-column `calls`/`elapsed_ms`/`preserved_rows`, take head fields (`table`, `native_admitted`, `reroute_reason`) from the FIRST chunk, copy (not alias) column dicts, and raise on cross-chunk disagreement. Single-table lands at `quality_metrics["chunked_route"]` (`_pipeline_auto_chunk.py:452`, after `_without_elapsed`); multi-table lands at a DIFFERENT key `quality_metrics["chunked_route_by_table"]` (`_pipeline_multi_table.py:391`). Isolated/capped runs round-trip the whole `quality_metrics` dict across the process boundary (`_isolated_common.py:379`), so a JSON-safe additive field needs no extra serialization.

CONTRACT for any new chunked_route field (B and D both use this):
1. Emit it in `chunk_route_evidence` so every per-chunk dict carries it.
2. Carry it through BOTH folders with an EXPLICIT fold rule (below), copying never aliasing, and extend the cross-chunk consistency guard.
3. Confirm it survives both `chunked_route` and `chunked_route_by_table`, and that `_without_elapsed` passes it through.
4. Fold-contract test for the new field (extend the existing streaming-evidence fold tests).

Fold rules differ by field kind and MUST be stated, or the bug the gate warned of recurs:
- Preflight-constant head fields (B decline records): same value every chunk, folded HEAD-FROM-FIRST (taken once, consistency-checked like `reroute_reason`). Never summed.
- Cumulative snapshots (D `kernel_calls`): the running total is already accumulated on the shared mutable evidence, so each chunk's emitted snapshot is a superset of the previous. Fold by taking the snapshot from the LAST folded chunk (define fold order explicitly), copying the dict. NEVER sum snapshots (double-counts) and never retain the mutable alias.

### B. OBS-2b structured route-decline codes (resolves #2, #3, #9)

The complete decline graph (dossier Q1) has ~19 causes across three phases. Emit a structured record at EACH decision source where the column (if any) is known, NOT at `_downgrade_to_oracle` (`_dispatch.py:155-166`), which re-tags all `node_routes` to oracle and flattens per-column cause to one table-wide string.

New module `native/_route_decline_codes.py`:
- `RouteDeclineCode`: a closed `str` enum with one member per distinct cause in the Q1 matrix. Column-attributable causes (keep their own code): `STRATEGY_VETOED_CHUNKED` (#4, dead-but-wired, reserve), `GROUP_KEY_SELF_ANCHOR` / `GROUP_KEY_MASKED_SIBLING` (#5), `FALLBACK_POLICY_NOT_NATIVE` (#6), `NO_NATIVE_KERNEL_OR_POOL` (#7), `WHEN_PREDICATE_NOT_NATIVE` / `WHEN_OUTSIDE_SUBSET` / `WHEN_READS_MASKED_COLUMN` (#8), `FAKER_SOURCE_NOT_STRING` (#11), `FAKER_SCHEMA_NOT_GUARANTEED` / `FAKER_METADATA_NOT_ALLOWLISTED` (#11b), `FPE_CHECKSUM_NOT_NATIVE` / `HASH_CONFIG_REJECTED` / `FAKER_PROVIDER_NOT_NATIVE` / `SOURCE_TYPE_NOT_STRING` / `GROUP_KEY_SIBLING_MISSING` / `GROUP_KEY_SIBLING_TYPE_NOT_NATIVE` (#12), `FAKER_PROVIDER_OUTPUT_NOT_STRING` (#19). Genuinely table-wide causes: `FK_RELATIONSHIP` (#1), `NO_MASK_NODES` (#2), `NON_SCALAR_NODE` (#3, node has no single column), `ADAPTER_REQUESTED` (#9), `UNCOVERED_OR_MISSING_COLUMNS` (#10), `CRYPTO_EXT_UNAVAILABLE` / `FPE_EXT_UNAVAILABLE` / `INDEX_EXT_UNAVAILABLE` / `RAW_HEX_EXT_UNAVAILABLE` (#13-16), `EMPTY_INPUT` (#17), `UNCONFIGURED_SET_MISMATCH` (#18). Reserve `OTHER` for genuinely unknown input only. Pin stable serialized string values.
- `RouteDeclineRecord` (frozen): `code: RouteDeclineCode`, `table: str`, `column: str | None`, `table_wide: bool`. `table_wide` = CAUSAL scope (the cause is inherently table-wide), independent of the routing consequence that every decline sends the whole table to the oracle. `column is None` iff `table_wide`.

Emission + preservation:
- Phase A (`_static_route_decision`) accumulates causes 3-7 across nodes; emit one record per `reasons.append`. Phase B steps 8-16 short-circuit; emit at each. Phase C (17-19) in `_chunked_entry`. APPEND to a `decline_records: tuple[RouteDeclineRecord, ...]` on `NativeRouteEvidence` (new field next to `reroute_reason`, which stays unchanged). Cause #8 (`when`) is computed even when the table is already vetoed (`_dispatch.py:394-398`) and must PRESERVE earlier records (append, do not replace); order records by decision phase.
- Surface `decline_records` as a head field via the shared contract (A), folded head-from-first.

Platform consumes the codes, refines the table-level reroute reason to per-column via OBS-1's safe allowlist (unknown code -> `other`) applied before persistence and narration.

Leak (resolves #9): the record holds a closed enum code plus schema identifiers only; no provider/expression/exception/strategy free text. Acceptance asserts the EXACT serialized key set, injects distinct hostile markers in provider/expression/exception/column text, and requires their absence from every record and from the folded payload. Identifiers appear only in the designated `table`/`column` fields. The leak sentry does not cover arbitrary dict payloads, so this is a dedicated hostile-input test, not a sentry claim.

### C. OBS-2c bounded value-free row-error summary (resolves #5, #6, #7)

Records (`RowErrorRecord`: table, column, chunk-local `row_index`, trigger, reason) carry NO chunk identity or absolute offset, by design on both legs (`native/_chunked_row_errors.py:1-12`). `RowErrorsFailedError.records` is per-single-table on the chunked legs (raised at `_chunked_entry.py:357`, `_chunked_oracle.py:393`), potentially multi-table only on the full-frame path. Post-mask full-frame enforcement raises with the UNCOVERED subset only (`_pipeline_finalize.py:428-441`); quarantine-covered errors never reach `.records`, so `.records` undercounts. The true observed total is `mask_row_errors` (covered + uncovered), already surfaced as counts in `quality_metrics["row_errors"]` (`:412-416`) and `quality_metrics["quarantine"]`. A non-completed isolated run carries `row_errors == ()` exactly like a clean completed run (`_isolated_common.py:392`); distinguish by `outcome` (`completed` vs `oom_killed`/`crashed`), never by the empty tuple.

New module `execution/_row_error_summary.py` (add `_row_errors.py` and this module to the physical-seam allowlist if edited):
- `RowErrorCompleteness`: `COMPLETE` (completed, exact total known), `PARTIAL_BEFORE_ABORT` (chunked leg stopped at first error-bearing chunk), `UNAVAILABLE` (non-completed outcome, nothing to attribute).
- `RowErrorSummary` (frozen): `table: str` (REQUIRED, validated single-table; multi-table grouped by a separate step), `by_column: Mapping[str, Mapping[str, int]]` (column -> trigger code -> count; map size bounded by schema columns x closed trigger vocabulary), `total_observed: int` (from `mask_row_errors`/the counts, NOT just `.records`, so quarantine-covered errors are included), `covered: int`, `uncovered: int`, `sample: tuple[...]` of `(column, trigger, row_index, chunk_index, chunk_row_offset)` with NO reason, `sample_limit: int` (hard default), `omitted: int`, `completeness: RowErrorCompleteness`.
- `summarize_row_errors(records, *, table, completeness, total_observed, covered, uncovered, sample_limit, chunk_index, chunk_row_offset)`: pure; validates every record belongs to `table`; carries `trigger` as-is (closed set), unknown -> `other`; pins sample order; rejects a zero/negative limit.

Capture chunk identity: at the raise sites (`_chunked_entry.py:336-357`, `_chunked_oracle.py:385-394`) `chunk_index` and `base_row_offset` are in scope; summarize the chunk there and attach to the raised error (or stage into `quality_metrics`) BEFORE the exception escapes, since it is discarded afterward. Cross-chunk repeated positions are tested with a unit aggregation fixture (both legs stop at the first error-bearing chunk, so a multi-chunk pipeline scenario is not reachable).

Leak: no reason text anywhere; hostile reason text asserted absent.

### D. OBS-2d strategy branch counts (resolves #1 + ambiguity 1)

`kernel_calls[strategy]` (`_chunk_masking.py:276-278`) counts strategy-BRANCH executions per (column, chunk), including successful Faker pool selections and idle bucket_perturb/date_shift/group_key branches and the Python-only text_redact/text_mask branches (`result.ran is None`), but excluding idle categorical/faker and idle when-masked branches. It is NOT a compiled-kernel count. The pinning test asserts `kernel_calls["faker"] == 3` (`tests/native/test_dispatch_faker.py:388-389`); do NOT change it.

Surface it on the public payload as a table-level per-strategy field named `strategy_branch_calls` (the name avoids implying compiled invocation), documented as branch activations with the text_redact/text_mask + idle caveat explicit. For "real compiled/selection work" the payload already carries per-column `executed_backend` and the evidence already tracks `compiled_kernel_executed` / `pool_select_calls`; expose those for the "did a compiled kernel actually run" question rather than overloading `strategy_branch_calls`. Thread it through the shared contract (A) with the CUMULATIVE-SNAPSHOT fold rule (take the last chunk's snapshot, never sum). Keep per-column `calls` unchanged and distinct.

Parity (resolves #11): the no-output-change assertion uses a pinned golden/oracle baseline, not flag-on vs flag-off (which can share a new regression); assert row/column order, dtypes, metadata, and deterministic values separately from the diagnostic fields. For 2d, independently count the specified branches, not another projection of `strategy_branch_calls`.

### Sequencing, module size, sentries (resolves #10)

- `_dispatch.py` is 597 LOC and `_chunked_entry.py` is 665 LOC, both near the 600 soft / 700 hard cap. The enum/record (B), the summary builder (C), and any D helper land in NEW modules, not in these files; any unavoidable growth needs an exact-LOC census entry under 700.
- B and D share the chunked_route evidence carrier and both folders, so they are NOT independent commits: land the shared foundation (A) first, then B and D on it; C is independent (separate carrier). Within whatever PR shape Cam picks, sequence A -> {B, D} -> C, each intermediate commit testable.
- Add `_row_errors.py`, `_row_error_summary.py`, `_route_decline_codes.py`, and any edited `execution/` module to the physical-seam allowlist with a one-line justification.

### Acceptance (per feature, independent expected values)

Each datum tested datum-by-route across unified / chunked / legacy-unavailable / generation / mixed, with INDEPENDENT expected values. B: each known decline yields its enumerated code with correct column/table-wide attribution; hostile text absent; `when`-override preserves earlier records. C: later-chunk and repeated-position aggregation (unit fixture); truncation (sample_limit + omitted, total exact); FAILED-job persistence; completed-zero != unavailable; total_observed includes quarantine-covered. D: two columns one strategy across chunks match an independent branch count and are not multiplied by the fold; idle and text-op caveats asserted. All: no output/determinism change (pinned baseline); additive only; sentries green.

## rev 4: executable contract (resolves the rev-3 plan-gate NO-GO)

Codex plan-gated rev 3 NO-GO: #1/#2/#3/#8 RESOLVED; blocking refinements on #4/#5/#7; smaller required fixes on #6/#9/#10/#11 and one new-defect note. All are tech-lead-level (pre-release; Cam delegated engineering calls). This section is the authoritative build contract and supersedes §A-D and §3/§4/§5 where they differ. Code facts below verified at this worktree (b5bcbcb3).

### R4.1 Emission boundary, corrected and completed (blocking #4)

Correct the §A description to the verified truth, do not claim checks that do not exist:
- Both folders consistency-check ONLY `table` (`_chunked_evidence.py:192`, `_chunked_output_sink.py:208`) and `pandas_read_passthrough` (`:203`, `:217`). `native_admitted`/`reroute_reason` are taken HEAD-FROM-FIRST with NO cross-chunk check (`_chunked_evidence.py:232-233`, `_chunked_output_sink.py:302-304`).
- Pre-existing asymmetry, OUT OF SCOPE, leave untouched: the resident folder sums `preserved_rows` (`_chunked_evidence.py:219-220`); the streamed folder does not. The new D field is table-level (not per-column), so it is unaffected; do not "fix" this asymmetry in this slice.

New head field `decline_records` (B) and new table-level field `strategy_branch_calls` (D):
1. Emit in `chunk_route_evidence` on every per-chunk dict (B: the preflight-constant tuple; D: a COPY of the current cumulative `kernel_calls` snapshot).
2. Add BOTH to the `head is None` empty-head defaults (`_chunked_evidence.py:222-229`, `_chunked_output_sink.py:292-299`): `decline_records: ()`, `strategy_branch_calls: {}`.
3. Fold rules (NEW work, implemented by this slice, not pre-existing):
   - `decline_records`: head-from-first, same as `reroute_reason`. ADD an explicit cross-chunk consistency check (raise on disagreement) as new code; state in the plan that this check is newly added.
   - `strategy_branch_calls`: cumulative snapshot. Fold order is chunk EMISSION order (both folders fold in iteration order); keep the LAST folded chunk's snapshot, copied not aliased. Never sum. "Last" = final cumulative total by construction.
4. Mutation-isolation test: after folding, mutating the source `NativeRouteEvidence.kernel_calls` must not change the folded payload (proves the copy).

Zero-chunk / empty-input decline surfacing (the #4a gap): empty input returns `iter(())` with no chunk results (`_chunked_entry.py:476-479`), so the fold yields the empty-head default (`table: None`). The table-level decline (`EMPTY_INPUT`, and any preflight table-wide decline that early-returns) is already recorded to the optional `route_evidence_sink` at `:478` (`_oracle_evidence(table, "empty_input")`). FIX: at finalization (`_pipeline_auto_chunk.py:452` single-table; `_pipeline_multi_table.py:391` multi-table), when the fold produced the empty-head default, build the public `chunked_route` payload from the KNOWN `table` argument plus the sink's table-level record, so `table` and `decline_records=(RouteDeclineRecord(EMPTY_INPUT, table, None, table_wide=True),)` reach the payload with zero chunks. Table identity comes from the finalization `table` argument, never from the (None) fold head. Acceptance: an empty-input run yields a public payload with the correct `table` and the `EMPTY_INPUT` decline record.

### R4.2 Row-error summary API, fully bounded (blocking #5)

`summarize_row_errors(records, *, table, columns, completeness, total_observed, covered, uncovered, sample_limit=DEFAULT_SAMPLE_LIMIT, chunk_index=None, chunk_row_offset=None) -> RowErrorSummary`, pure. Concrete bounds:
- `DEFAULT_SAMPLE_LIMIT = 50`; `MAX_SAMPLE_LIMIT = 1000`. `sample_limit <= 0` raises `ValueError`; `sample_limit > MAX_SAMPLE_LIMIT` CLAMPS to `MAX_SAMPLE_LIMIT` (does not raise).
- `columns: frozenset[str]` = the table's schema columns (REQUIRED). Every record's column must be in `columns`; a record outside it raises. The `by_column` map is thereby bounded by `len(columns) x len(closed trigger vocab)`.
- Deterministic sample order: sort eligible records by `(column, chunk_index, row_index)`, then truncate to the effective limit. `sample` holds the first `min(limit, len(eligible))`.
- `omitted = max(0, len(eligible) - len(sample))` where `eligible` = the records passed in (chunk-local `.records`). `total_observed`/`covered`/`uncovered` are the counts-based observed totals and are DISTINCT from `len(eligible)`; document both.
- Invariants validated in `__post_init__`: `covered >= 0`, `uncovered >= 0`, `total_observed == covered + uncovered`, `omitted >= 0`, `len(sample) <= sample_limit`.

### R4.3 Chunk identity, correct offsets (#6)

Capture at the raise sites the RUNNING offsets, not `base_row_offset`:
- Native (`_chunked_entry.py`): loop `i` and running `row_offset` (initialized from `base_row_offset` at `:292`). Pass `(chunk_index=i, chunk_row_offset=row_offset)`.
- Oracle (`_chunked_oracle.py`): `chunk_index` and running `row_offset` (`:319`). Pass `(chunk_index, chunk_row_offset=row_offset)`.
Attach the built summary to the ESCAPING exception: `RowErrorsFailedError` gains an optional `summary: RowErrorSummary | None` attribute, set before raise. Staging only into local metrics is insufficient. Tests: unequal chunk lengths and a nonzero invocation `base_row_offset`.

### R4.4 Completeness as observed coverage (blocking #7)

Producer/state matrix for `RowErrorCompleteness`:
- `COMPLETE` = the run OBSERVED every error in processed input, exact total known. Two producers: (a) full-frame clean completion; (b) full-frame post-mask POLICY FAILURE (all records observed at `_pipeline_finalize.py:410` before the partition). COMPLETE is defined by completed observation, NOT by job success.
- `PARTIAL_BEFORE_ABORT` = a chunked leg stopped at the first error-bearing chunk (`_chunked_entry.py:357`, `_chunked_oracle.py:393`); later unprocessed chunks unknown.
- `UNAVAILABLE` = non-completed outcome with nothing transportable (isolated caught failure, OOM, crash). COMPLETE-with-zero-records (clean completion) is DISTINCT from UNAVAILABLE.

Build + carrier:
- Build per-table summaries from ALL observed records (`mask_row_errors`) BEFORE the partition at `_pipeline_finalize.py:428-441`. `total_observed = len(mask_row_errors)`, `covered = len(row_errors_covered)`, `uncovered = len(row_errors_uncovered)`. Attach the summary to `RowErrorsFailedError` (R4.3) while leaving `.records` (the uncovered subset) UNCHANGED.
- Public carrier: `quality_metrics["row_error_summary"]` (single-table) and `quality_metrics["row_error_summary_by_table"]` (multi-table full-frame), JSON-safe. On the raise path the summary rides the exception; on completed runs it rides `quality_metrics`.
- Build `by_column` directly from `RowErrorRecord.table`/`.column`; NEVER reconstruct attribution from `quality_metrics["row_errors"]` keys, which concatenate unescaped identifiers (`_pipeline_finalize.py:414`).
- Isolated boundary DECISION: isolated CAUGHT failures return `UNAVAILABLE` (the process-exception path drops metrics by design at `_isolated_worker.py:216` / `_isolated_run.py:525`); do not build cross-process exception-payload transport. "FAILED-job persistence" is scoped to the in-process `RowErrorsFailedError` summary and to successfully-completed isolated runs (metrics transported normally).

### R4.5 Hostile-input acceptance, scoped (#9)

Scope absence assertions to the NEW projections ONLY (`decline_records`, `row_error_summary`), never to the whole folded payload (which keeps the legacy free-text `reroute_reason` by design). Assert adversarial provider/expression/exception/reason markers absent from the new projections; preserve adversarial identifiers verbatim ONLY in the designated `table`/`column` fields; unknown trigger serializes `other`. State the EXACT serialized key set for `RouteDeclineRecord` (`code`, `table`, `column`, `table_wide`) and `RowErrorSummary` (incl the column-keyed count-map keys). Do not require sanitizing `reroute_reason`.

### R4.6 Module size, concrete plan (#10)

`_chunked_entry.py` is 665 LOC (35 headroom); `_dispatch.py` 597. Design keeps new logic OUT of these files: `summarize_row_errors` and all summary logic live in `_row_error_summary.py`, so the native raise site adds only a call + attach (~5-8 LOC). If that still breaches, PRIMARY extraction: move the empty-input/preflight-admission wiring block (`_chunked_entry.py:476-499+`, the `state.first is None` guard + `reject_*` + `validated_rest` + C5c-ii admission) into a new `native/_chunked_preflight_wiring.py`. Census any file in 600-700 at its exact LOC; never exceed 700. Measure during build, not assumed now.

### R4.7 Parity baseline, concrete (#11)

Freeze a golden masked-output artifact captured at base `da9623b7` as a committed fixture, with expected values recorded INDEPENDENTLY of the changed implementation; parity asserts row/column order, dtypes, schema metadata, and deterministic values against that frozen artifact. A live oracle sharing the changed code is insufficient alone. For 2d, hand-count the specified branches independently; never assert `strategy_branch_calls` against another projection of itself.

### R4.8 Drop the extra-field exposure (new-defect note)

Do NOT add `compiled_kernel_executed`/`pool_select_calls` to the public payload in this slice (rev-3 §D floated them without a contract). They stay internal explanatory references. `strategy_branch_calls` is the ONLY new D public field. The "did a compiled kernel actually run" question is answered by the existing per-column `executed_backend` already on the payload.

## 0. Why

OBS-1 wired the job narrative but four job-log data points are still missing or wrong at the engine boundary. The platform cannot report them because the engine does not emit them in a structured, leak-safe shape:

- The unified-slice route cannot report per-column backends, because node evidence has no table/column/strategy fields (the 2a defect).
- Per-column fallback reasons exist only as composite free-text strings that embed dynamic column and strategy text, so the platform cannot surface them without a leak.
- Row errors have no bounded, value-free summary that is safe to persist and narrate.
- The branch-execution count (`kernel_calls`) is computed but never surfaced with defined semantics.

All four are additive diagnostic evidence. None changes masked output, determinism, or any accepted-job result. This plan delivers the engine half of all four as one PR, structured as four independent commits so a gate finding on one subsystem does not block the other three in the same pin bump.

## 1. Non-negotiable invariants (every sub-slice)

- NO OUTPUT CHANGE. Evidence is diagnostic only. Masked and generated output, row order, dtypes, schema metadata, and determinism are byte-identical before and after. Each sub-slice lands a parity assertion proving flag-equivalent output on a representative table (reuse the D9 flag-on/flag-off parity pattern where a route has one).
- LEAK-SAFE. New evidence is codes, counts, positions, and schema identifiers ONLY. Never a cell value, an expression, a raw exception, a provider string, or a composite reason string. Table, column, and strategy names are treated as safe schema identifiers (they already travel in `node_id`, in `RowErrorRecord`, and in chunked-route evidence); this is consistent with `docs/guides/logging.md` and the existing leak sentry. Reason codes and trigger codes are CLOSED enumerated sets. Unknown or unmapped inputs map to an explicit `other`, never to raw text.
- ADDITIVE ONLY. New fields are added next to existing ones. No existing field is renamed, removed, or has its meaning changed (`kernel_calls`, per-column `calls`, `reroute_reason`, `RowError.reason` all keep their current semantics and callers).
- PURE AND UNIT-TESTABLE. Each emitter stays a pure function of its inputs where the current code already is (for example `assemble_node_evidence` takes no context), so every invariant is testable without running a pipeline.
- SENTRIES. Any changed or new `execution/` module must be in the physical-seam allowlist (`tests/sentry/test_physical_seam_disconnection.py`); add new modules with a one-line justification. Keep every module at or under 600 LOC (hard max 700) or add an exact-LOC census entry (`tests/sentry/test_module_size.py`). `_dispatch.py` is at 597 LOC, so 2b and 2d logic lands in NEW modules, not in `_dispatch.py`.

## 2. Commit A: OBS-2a unified-slice attribution (smallest, highest value)

Site: `src/decoy_engine/execution/_unified_slice_evidence.py`, `assemble_node_evidence`, the `node_evidence[node.node_id]` dict (lines 136-150).

Change: add three structured fields read directly off the `PhysicalNode` already in scope (`_plan.py:176`: `node_id: str`, `table: str`, `columns: tuple[str, ...]`, `strategy: str`):

```python
node_evidence[node.node_id] = {
    "table": node.table,
    "columns": list(node.columns),
    "strategy": node.strategy,
    "operator": evidence.actual_operator,
    ...  # existing fields unchanged
}
```

These come from the plan node, not from parsing `node_id`. Update the module docstring to name the three added fields. No other change.

Acceptance:
- An ACTIVATED unified-slice run: every node's evidence carries `table`, `columns`, `strategy`, with values INDEPENDENTLY computed from the physical plan (assert against the plan's nodes, not re-parsed from `node_id`).
- A node covering multiple columns reports all its columns as a list (the case that defeats node_id splitting).
- A column name containing `:` or `+` is carried verbatim in `columns` and is never confused with the id delimiters.
- Existing fields and the one-to-one timing check are unchanged. Per gate finding #8, existing exact-shape evidence fixtures are AUTHORIZED to gain the `table` attribute on their node namespaces and the three literal keys in their expected dicts and key sets. Keep exact equality and every negative test; never swap to a subset check and never change an existing key's value.

Leak check: table/column/strategy are schema identifiers; no values. Covered by the unit test plus the leak sentry.

## 3. Commit B: OBS-2b structured route-decline codes

Sites (decision sources): `src/decoy_engine/execution/native/_dispatch.py` (`reasons.append(...)` at 258/267/273-277/286/295, and the table-level `_not_native` / `_downgrade_to_oracle` helpers at 148-161), `native/_real_type_admission.py`, `native/_faker_deterministic_admission.py`.

New module: `src/decoy_engine/execution/native/_route_decline_codes.py` (keeps `_dispatch.py` under the LOC cap). It defines:

- `RouteDeclineCode`: a closed `str`-valued enum. Initial members, one per existing decision source, each named for the cause and carrying NO dynamic text: `NON_SCALAR_NODE`, `STRATEGY_NOT_NATIVE_CHUNKED`, `ORDER_DEPENDENT`, `FALLBACK_POLICY_NOT_NATIVE`, `NO_NATIVE_KERNEL_OR_POOL`, `REAL_TYPE_REJECTED`, `FAKER_ADMISSION_REJECTED`, `WHEN_REJECTED`, and `OTHER`. The set is derived from an enumeration of today's `reasons.append` call sites (survey them exhaustively before coding; add a member per distinct cause, never a catch-all that hides a real cause).
- `RouteDeclineRecord` (frozen dataclass): `code: RouteDeclineCode`, `table: str`, `column: str | None`, `table_wide: bool`. `column is None` iff `table_wide is True`.

Emission: at each `reasons.append(...)` site, append a paired `RouteDeclineRecord` with the matching code, the table, the column (for per-column declines), and `table_wide=False`. For the table-level declines (`_not_native`, `_downgrade_to_oracle`, the non-scalar preflight exclusions that apply table-wide), emit `table_wide=True, column=None`. Carry the records as a new field `decline_records: tuple[RouteDeclineRecord, ...]` on `NativeRouteEvidence` NEXT TO the existing `reroute_reason` string. The string is unchanged and stays for internal use; the platform consumes only `decline_records`.

Table-wide vs per-column causality (Codex rev-1 P1-2): a whole-table decline means the reason does not necessarily apply to every column, and a per-column decline applies only to its column. `table_wide` makes the two distinguishable at the consumer, so the platform never fabricates a per-column reason from a table-wide decline, and never generalizes a per-column decline to the table.

Acceptance:
- Hostile input: a provider name, column name, strategy, expression, or exception text that would appear in the legacy composite string NEVER appears in any `decline_records` entry (the code is a fixed enum member; the column is a schema identifier). Test by routing a table whose column names and strategy labels contain adversarial text through each decision path and asserting the records contain only enum codes plus the identifier.
- Each existing decline cause yields its specific enumerated code with correct `table`/`column` attribution and the correct `table_wide` flag.
- A table with one table-wide decline and one per-column decline yields exactly one `table_wide=True` record and one `table_wide=False` record, not two of either.
- `reroute_reason` string content is unchanged (existing dispatch tests green unmodified).

## 4. Commit C: OBS-2c bounded value-free row-error summary

Sites: `src/decoy_engine/execution/_row_errors.py` (`RowError`, `RowErrorRecord`, `drain_row_errors`), `native/_chunked_row_errors.py` (chunk-local positions), `errors.py:396` (`RowErrorsFailedError.records`), non-completed isolated outcome carries an empty tuple (`_isolated_common.py:392`).

New module: `src/decoy_engine/execution/_row_error_summary.py`. It defines:

- `RowErrorCompleteness`: closed enum `COMPLETE`, `PARTIAL_BEFORE_ABORT`, `UNAVAILABLE`. `COMPLETE` with zero records (completed, no errors) is distinct from `UNAVAILABLE` (never completed, so the empty tuple is NOT zero).
- `RowErrorSummary` (frozen dataclass): `table`, `by_column: Mapping[str, Mapping[str, int]]` (column then enumerated `trigger` code then count), `total: int` (exact observed total), `sample: tuple[RowErrorSampleItem, ...]` where each item is `(column, trigger, row_index, chunk_index, chunk_row_offset)` and carries NO `reason`, `sample_limit: int`, `omitted: int` (total minus sampled), `completeness: RowErrorCompleteness`.
- `summarize_row_errors(records, *, completeness, sample_limit, chunk_index=None, chunk_row_offset=None) -> RowErrorSummary`: pure. Builds the summary from `RowErrorRecord`s, EXCLUDING `reason`. `trigger` is already a closed set (`format_error`, `mask_error`); carry it as-is, map anything unexpected to `other`. Positions are chunk-local, so each sample item records its `chunk_index` and `chunk_row_offset` so a reader can interpret a repeated local `row_index` across chunks without collision.

Caller wiring: at the drain/aggregation point that already knows the table and chunk, build the summary with the correct `completeness` (completed-zero vs partial-before-abort vs unavailable) and chunk identity. Do not read a non-completed empty tuple as `COMPLETE`.

Acceptance:
- Later-chunk failures and repeated chunk-local positions are disambiguated by `chunk_index`/`chunk_row_offset`.
- Truncation: more than `sample_limit` errors yields `len(sample) == sample_limit` and `omitted == total - sample_limit`, with `total` exact.
- FAILED job (`RowErrorsFailedError.records`) produces a summary with the error records and the right completeness.
- `COMPLETE` with zero records is not equal to `UNAVAILABLE`; both are representable and distinct.
- No `reason` text appears anywhere in the summary (leak test over adversarial reason text).

## 5. Commit D: OBS-2d kernel_calls with defined semantics

Site: `NativeChunkEvidence.kernel_calls: dict[str, int]` (`native/_dispatch.py:133`), populated per strategy branch during chunk masking (`_chunk_masking.py`).

Change: surface `kernel_calls` on the route evidence the platform reads, with its semantics pinned in the docstring and in the plan:

- It is a TABLE-level, per-STRATEGY cumulative snapshot of compiled-kernel branch executions across all chunks of that table. It is NOT per-column (several columns can share one strategy) and NOT a per-chunk delta.
- Idle/empty: a strategy that ran no compiled call (idle input, Arrow-only operator, or the Faker route which does not populate `kernel_calls`) is ABSENT from the dict, not present-with-zero, and the platform must read absence as "no compiled branch executed", never as a success count of zero. Document this explicitly so 2d is never presented as universal compiled-kernel proof.

Keep the existing per-column `calls` field untouched and distinct (different semantics). If any helper exceeds the LOC cap, the exposure helper lands in the 2b new module or its own small module.

Acceptance:
- Two columns sharing one strategy across multiple chunks: the strategy's `kernel_calls` count equals the independently observed total branch executions (not a per-column or per-chunk value).
- A table with an idle strategy: that strategy is absent from `kernel_calls`, and the test asserts absence, not zero.
- The Faker route: `kernel_calls` is empty/absent and is reported as such.

## 6. Compatibility and release

- The four new fields are additive on `NativeRouteEvidence`, the unified-slice node evidence, the route evidence the platform reads, and a new row-error summary carrier. Inventory every engine-side selective copy of these structures before coding (grep for field-by-field reconstruction) so an additive field is not silently dropped inside the engine before it reaches the boundary.
- CHANGELOG entry under the current rc, naming the four additive evidence fields and the no-output-change guarantee.
- After merge: cut the engine release artifact that carries these fields and bump the platform pin to it. The platform-consume PR (revised `2026-10-19-obs-2-joblog-completeness.md`) targets that pinned artifact and verifies the runtime version. Engine 0.9.0 is retained on the platform side only as missing-field compat coverage (old-export test), not as the consume-path env.

## 7. Verification and gates

- Venvs: build and run the engine suite on `~/.cache/decoy-ci-mirror-venv` (pure-python/sentry) and `~/.cache/decoy-d1a-abi3-venv` (native/parity/isolated companion). One test run at a time via `PYTHONPATH="$(pwd)/src:$(pwd)" ~/bin/pytest-one <python> <paths> -q`.
- Targeted suites: `tests/` for unified-slice evidence/witness, dispatch/admission, row errors, native chunk masking, plus the physical-seam and module-size sentries and the leak sentry (`tests/sentry/test_log_interpolation.py`). ruff check + format, mypy.
- Parity: the no-output-change assertion per sub-slice (D9 flag parity pattern where available; otherwise a before/after masked-output equality test on a representative table).
- Gate chain: Opus authored this plan and VERIFY; dennis adversarial review; Codex final (R2). Build STOPS on any plan defect and reports it, never routes around a divergence. Merge on full green gate chain + local CI (Phase-F bar). Pre-merge Slack explainer (what the slice is, what it does, an example) before the engine merge and again before the platform-consume merge.
- DOCUMENT: engine CHANGELOG; after merge, platform RECENTLY-SHIPPED + section Slack. ROADMAP batched (another session is editing it).

## 8. Done when

- [ ] Commit A (2a): unified-slice node evidence carries structured `table`/`columns`/`strategy` from the plan node; multi-column and delimiter-bearing names covered; existing evidence tests green unmodified.
- [ ] Commit B (2b): a new closed `RouteDeclineCode` enum + `RouteDeclineRecord` carried on `NativeRouteEvidence`; every decision source emits a structured record with correct column/table-wide attribution; hostile-input leak test green; `reroute_reason` string unchanged.
- [ ] Commit C (2c): a bounded value-free `RowErrorSummary` with chunk identity, exact total, sample limit + omitted count, and a completeness enum where completed-zero != unavailable; failed-job + later-chunk + truncation covered; no reason text present.
- [ ] Commit D (2d): `kernel_calls` surfaced on route evidence with table/per-strategy snapshot semantics documented; idle/Faker absence asserted (not zero); per-column `calls` untouched.
- [ ] Invariants: no output/determinism change (parity asserted per sub-slice); additive only; leak-safe (sentry + per-field hostile tests); sentries green (physical-seam allowlist updated for any new/changed module; module sizes under cap or censused).
- [ ] CHANGELOG; release artifact cut + pin bump staged for the platform-consume PR. dennis + Codex GO.

cam
