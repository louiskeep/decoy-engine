Status: plan (OBS-2 engine evidence emission. rev 1. The consolidated engine half of OBS-2 job-log completeness. Authored after the OBS-2a build correctly STOPPED on a plan defect: the existing `quality_metrics["unified_slice_activation"]["nodes"]` evidence carries operator/backend/calls but NO structured table/column/strategy, and the only column signal is the composite `node_id` which cannot be safely split (names contain `:` and `+`, and a node can cover several columns). So 2a is NOT platform-only; it needs a small additive engine field. That makes all four OBS-2 evidence sub-slices engine-emission items, which Cam approved consolidating into ONE engine PR + ONE pin bump + ONE platform-consume PR to cut gate/pin cycles. Base: engine `origin/main` @ da9623b7 (post OBS-1b #289). Companion platform plan to be revised after this plan gates: `decoy-platform/docs/plans/2026-10-19-obs-2-joblog-completeness.md`. Program plan: `decoy-platform/docs/plans/2026-10-19-observability-program.md` §2 OBS-2. Merge authority: Phase-F bar (dennis + Codex final + local CI; GitHub CI ignored). R2 change: crosses privacy + schema + compatibility boundaries, so Codex is the final gate. Design spec: `docs/guides/logging.md` §4 and `docs/security/error-reporting-and-data-exposure.md`.)

Rules consulted: dev-rules `00-universal.md`, `development-loop.md` (OBS-0 done-state), `observability-and-resilience.md` (log location + type, never value), `risk-and-exceptions.md` (R2), `root-cause-not-patch.md`, `plan-defines-acceptance-tests.md`; engine `CLAUDE.md` (module size 600/700, physical-seam sentry, established methodology, pre-GA hard delete).

# OBS-2 engine evidence emission (consolidated)

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
- Existing fields and the one-to-one timing check are unchanged; the existing `assemble_node_evidence` tests stay green unmodified.

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
