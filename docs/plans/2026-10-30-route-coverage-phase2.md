Status: proposed (phase-2 plan refreshed against `origin/main` a6e5c252; supersedes the uncommitted 2026-10-26 draft and folds its six Codex GO-WITH-FIXES corrections into the slices)

Summary: widen the out-of-core (OOC) FK route to the shapes the full-frame pandas oracle already serves, byte-for-byte or decline-with-reason, in separately gateable slices. Two slices are V1-REQUIRED and buildable on main today (address faker providers; FPE as an OOC FK parent key, crypto-gated). Everything else depends on the phase-1 B1 seams, which are NOT on main, and is deferred past V1 with its design pinned here.

Baseline: engine `origin/main` at `a6e5c252` (H-1 Wave A split, PR #348). Phase 1 as built: merge `a39c6486` (PR #324, B3-B7). B1 seams: branch `claude/blissful-goldberg-mkwpdk` at `e1f96acb` (unmerged, base `195fab12`, 66 commits behind main). F29: `fdd08a8` (PR #336). STORM close-out: `claude/eager-clarke-14deh1` at `ab29ef0c` (unmerged). Every `file:line` below is at `a6e5c252` unless prefixed `e1f96acb:` or `storm:`.

Rules consulted: `DEFECT-CATALOG.md` sections 2 to 5, `HOSTILE-PLAN-REVIEW.md` and `OWNER-DECISIONS-2026-10-30.md` (decoy-platform `handoffs/queue`, `docs/handoffs/`); dev-rules `00-universal.md`, `development-loop.md` (PLAN phase: rules line, design notes, established solutions), `risk-and-exceptions.md` (R-levels), `documentation.md`, `reuse-first.md`, `testing.md` (by title); engine `CLAUDE.md` (established methodology, 600/700 census, pre-GA hard delete, `RELEASE_PHASE`); decoy-platform `docs/ROADMAP.md` (Phase 0 "Route-coverage ph2", F05/F06/F11) and the MASTER spec sections F05, F06, F11.

Risk: R2 for every slice (route admission is a compatibility and privacy surface; slice 4 is also crypto). Slice 4 is crypto-gated: Codex plus Dennis on the exact commit. No slice is self-certified.

Grounding: read-only inspection of the repos named above. Nothing was run: no test, no parity run, no probe, no benchmark. Line numbers are from `cat -n` on the checkout; the reviewer re-verifies at least 25 of them.

# Route-coverage phase 2: OOC route deepening (refreshed plan)

## 0. What changed since the 2026-10-26 draft (re-grounding)

Every citation in the draft predates H-1 Wave A and the phase-1 merge. The table lists what moved, what no longer exists, and what the draft assumed existed but does not.

| Draft claim or citation | Status at `a6e5c252` | Consequence |
|---|---|---|
| B1 row-offset channel: `mask_batch(..., row_offset, row_error_sink)` at `O/_mask.py:449`; `O/_runner.py:449`, `O/_stream_driver.py:322,370` thread `row_offset` | NOT on main. `mask_batch` is `O/_mask.py:362-372` with no `registry`, `row_offset` or `row_error_sink`; neither driver threads an offset (`O/_runner.py:475-484`, `O/_stream_driver.py:346-355`). The seams exist only at `e1f96acb:O/_mask.py:449-461`, `e1f96acb:O/_row_error_bridge.py`, `e1f96acb:O/_offset_bridge.py`. | Slices 2, 3a, 3b, 5 and 6 have a missing prerequisite. Slice 0 below lands it. |
| B1 row-error bridge `O/_row_error_bridge.py`, offset bridge `O/_offset_bridge.py` | Absent from main (no file). bucketize and date_shift still decline with `out_of_core_row_error_strategy_unsupported` (`O/_compat.py:74-87`). | Same. |
| B3 `O/_faker_pool.py` and B4 `O/_faker_native.py` were PLANNED prerequisites | PRESENT. `OocFakerPoolContext` (`O/_faker_pool.py:77`, reservation refusal `:181-189`, `close` `:220`); `_faker_native.decline_reason` (`O/_faker_native.py:76-92`), `decide_table` (`:191`), `native_faker_array` (`:259`). Shipped in PR #324 with B5, B6, B7. | Slice 1 has every prerequisite on main. |
| B6 Arrow code_set dict-map | PRESENT: `kernel/_code_set.py:28` `code_set_map`, used at `O/_mask_group_c.py:397-400`. | Slices 3a and 3b keep this seam. |
| B8 position-keyed categorical on OOC | NOT on main: non-deterministic categorical still declines (`O/_compat.py:321-332`). | Slice 2 cannot "point B8's machinery at the faker pool"; it must build the positional leg itself on the SEQ seam (`N/_operator_step.py:135`, `E/_positional_keys.py:20`). |
| B2 date_shift on OOC | NOT on main (`O/_compat.py:81-86`). | Slice 6 "what it unlocks" shrinks accordingly. |
| `O/_compat.py:28` key set, `:459` enforcement, `:324-333` code_set shape, `:215` when, `:91-116` deferred group C | Moved: key set `O/_compat.py:34`, enforcement `:529-536`, code_set shape `:384-403`, when predicate `:238-247`, deferred group C `:101-128`. | Citations refreshed throughout. |
| Key-mask callsites `O/_stream_join.py:698`, `O/_batch_join.py:346,564`, `O/_relation.py:118-134` | Moved: `O/_stream_join.py:639`, `O/_batch_join.py:347` and `:565`, `O/_relation.py:135`. Still four sites, all positional, none passes `column` (`grep "mask_column("` over `O/`). | Slice 4 inventory in 5.4. |
| `E/_sequential_native.py:145` `_KEY_STRATEGIES`, `:162` `REASON_KEY_NOT_INJECTIVE`, `:13` rationale | Moved: `:128`, `:125` (now `reasons.route.SEQUENTIAL_FK_KEY_INJECTIVITY_NOT_GUARANTEED`), `:13-16`. | Scope claim corrected in 5.4. |
| `E/_operator_registry.py:214` allowlist; `N/_real_type_admission.py:55,163`; `E/_sequential_native_ops.py:391` | Unchanged lines: `:214`, `:55`, `:163`; SEQ admit moved to `:392-393`. Draft's claim that `_faker_deterministic_admission.py:42` and `_faker_positional_admission.py:29` read `C1_PROVIDER_ALLOWLIST`: the deterministic admission reads `OPERATORS["faker"].provider_allowlist` directly (`N/_faker_deterministic_admission.py:42`); the positional admission does not read the allowlist at all. | Slice 1 cross-route proof lists the real readers. |
| `S/_orphan.py:37-60` EXCLUDE-then-CASCADE, `:99` `make_remap_fn`, `:135,:150` | Unchanged `:37-60`; `make_remap_fn` at `:100`, table swap `:143-153`. The resolution core moved to `E/_fk_resolve.py:74,177,197`. | Slice 6 design cites both. |
| `E/_pandas_adapter.py:649-656` key-column error selection | Now `:635-641` (excluded rows from `key_error_rows` for `edge.parent_columns` only) and `:650-653` (EXCLUDE). | Codex correction 2 holds: only KEY-column errors cascade. |
| `G/pool/_sampler.py:191,246-256,265,294-314,315,317,369,391,412` | `sample` `:192`; det+UNIQUE error `:247-256`; UNIQUE `:266-306` (`pd.unique` `:295`, permutation `:306`, `UNIQUENESS_IMPOSSIBLE` `:298`); MATCH `:316`, SCALE `:318`; `_match_source_cardinality` `:370`, `nunique` `:392`, permutation `:403`. | Slice 5 citations refreshed. |
| `T/code_set.py:418,423,410,400-460`; `S/_code_set.py:15-24,81-87,99` | `_pick_gen` call `:418` and def `:573` (row_index bytes `:596`); `_apply_chapter_preserve` `:422` (codes `:434-437`); `apply_code_set` `:331` (`row_index` param `:337`). `S/_code_set.py:82-89` `_PER_VALUE_CODE_SET_ERRORS`; gen row_index `:100`. | Slices 3a and 3b. |
| F29 did not exist | `decoy_engine.reasons` registry (`reasons/_registry.py:22` `REGISTRY_VERSION = "1.0.0"`); choke-point sentry forbids literal codes (`tests/sentry/test_reason_registry.py:1-20`); manifest append-only (`tests/unit/test_reason_registry.py:85-89`); baseline frozen by sha (`:283-288`). STORM adds `decoy-reasons/v1` identity (`storm:src/decoy_engine/reasons/_identity.py:19`). | Section 8. B1 predates F29 by four days (base `195fab12` 2026-10-25, F29 `fdd08a8` 2026-10-29): its new codes are literals and will fail the sentry on rebase. |
| F06 quarantine on OOC | PR #338 r7 refuses parse-stage parent-row quarantine under non-fail policies on EVERY route (`feat/f06-declared-parsing:src/decoy_engine/declared_casting/_eligibility.py:168`, docs `declared-casting.md:100`); forced OOC refuses `row_quarantine`. | Slice 6 overlap in 5.6. |
| F25 protected scratch | No `src/decoy_engine/scratch/` on main; plan on `plan/f25-4c-consumer-capabilities`. | Slices 5 and 6 use `O/_budget.py` and existing spill, and name F25 as a later migration, not a dependency. |
| F22 atomic publication | Re-plan rev 1 on `plan/f22-publication-replan` (`50ad3ba8`), not built. Main has `TransactionalSink` (`E/_transactional_sink.py:59-97`) with `abort` best-effort (`:366-380`). | Slice 6d depends on F22; slices 1 to 5 use the existing sink unchanged. |

Module sizes at `a6e5c252` (`wc -l`): `O/_compat.py` 563, `O/_mask.py` 484, `O/_batch_join.py` 569, `O/_relation.py` 600 (at the goal; one more line needs a census entry), `O/_stream_join.py` 650 (census `tests/sentry/test_module_size.py:128`), `O/_runner.py` 681 (`:122`), `O/_stream_driver.py` 609 (`:124`), `O/_budget.py` 609 (`:79`). GOAL 600, MAX 700 (`:54-55`). Slice 4 touches four files at or near the goal; it must not grow them past their census numbers without a visible bump, and must not cross 700.

## 1. Guarantee and non-goals

Guarantee: after each slice, every job the OOC gate admits produces output that `tests/parity/_strict_compare.compare_strict` reports identical to the full-frame pandas oracle on all nine dimensions (`tests/parity/_strict_compare.py:26-36`), across the three OOC drivers (`resident`, `sink-batch`, `sink-reorder`, `tests/parity/test_phase_d_d1a_hash_baseline.py:41-44`) and irregular batch splits; every job the gate does not admit declines or refuses with a registered F29 reason; a failed run publishes nothing.

Non-goals (stay declined, with the reason that is actually true): geo_generalize (needs a whole-column k-anonymity count aggregation over the full column, which the batch-local contract does not provide; the Codex correction stands: it is a single-column strategy, the problem is the count, not the width; `O/_compat.py:101-107`); formula and derived (dynamic output type, `:108-120`); nested (`:121-127`); cross-row strategies (`:25-33`, `:295-301`); `when` predicates on OOC (`:238-247`); categorical and faker as FK parent keys (not injective; 5.4); stateful faker on an FK key; more than one stateful column; gen mode with chapter_preserve together; bucketize string sources on the row-error bridge (`e1f96acb` declines them); hostile in-process objects (V2 per the owner decision).

## 2. Invariants as properties

- **P1 oracle parity.** For every admitted plan, `compare_strict(oracle, ooc)` is all-True on every dimension, for every batch split and every driver. The oracle is `PandasExecutionAdapter.run` (`tests/parity/test_out_of_core_fk_parity.py:101-104`) with no compiled shortcut: the SEQ seam is absent off the sequential route (`S/_faker.py:101-103` reads `ctx.seq_native`, None here), and the oracle run additionally installs a raising marker on the compiled entry points the slice uses (pattern `tests/parity/_ooc_faker_data.py:132`).
- **P2 split invariance.** The output of a position-keyed or spliced column does not depend on where batches are cut. Proven by splits `{None, 3, 1}` plus a split immediately before a null and a one-row trailing batch.
- **P3 route proof.** A native leg is proven by consumption of the compiled result (a raising Python reference for an admitted value), never by a backend label or equal output.
- **P4 no raw value as output.** No source cell reaches the output, a reason message, a warning, an exception, a log, a repr or evidence. Reasons carry strategy, column name and Arrow type only (`O/_faker_native.py:27-29`, `O/_fpe_native.py:13-14`).
- **P5 fail closed, publish nothing.** Any abort after admission leaves no staged or committed output: `TransactionalSink.abort` runs and `publication_evidence.cleanup` is `CONFIRMED` (`E/_transactional_sink.py:366-380`); spill and run-local state are released (`O/_runner.py:572-575`, `faker_pool.close()` `:341`).
- **P6 bounded memory.** Every slice that holds state names its bound (O(1), O(distinct), or a declared O(rows) ceiling with a reserve-before-materialize check against `OutOfCoreBudget`, `O/_budget.py:194,356`) and records peak RSS on the large tier.
- **P7 key surface injective.** Only per-value injective strategies are OOC FK parent keys. The set is `{hash, redact, truncate, passthrough}` today (`O/_compat.py:34`) plus `fpe` after slice 4. Nothing in this plan admits a many-to-one strategy as a key.
- **P8 registered reasons only.** Every decline, refusal and outcome this plan adds is a `decoy_engine.reasons` identity; the choke-point sentry stays green with no new adapter entry.

## 3. Class table

| # | Class | Applies | Structure that closes it | Sentry or property test | Mutation that must turn it red |
|---|---|---|---|---|---|
| 1 | Value-bearing output | Yes: new reason messages, new warnings, slice 5 budget decline, slice 6 quarantine records | Reasons are registry identities with fixed summaries; messages name strategy, column and Arrow type only (`O/_faker_native.py:27-29`); quarantine JSONL is the one designed home for raw rows and stays protected (F06) | Seeded-marker corpus: a cell value `CELLMARK_<rand>` in every fixture, asserted absent from every exception `str`, `args`, warning, `quality_metrics`, log record and route reason on every slice run (red: inject the value into one message) | Format a cell into one `ExecutionError` message; the corpus test must fail |
| 2 | Cleanup missing on an exit path | Yes: pool context, joiners, sink staging, slice 5 resident store, slice 6 error store | One owner per resource with disposal in `finally` (`O/_runner.py:341`, `:572-575`); slice 5 resident store is a context manager released on every non-commit exit; slice 6 error store is spill-owned under `check_temp_disk_budget` (`O/_budget.py:525`) | Exit-path matrix per owner (4.1): gate refusal, mid-stream exception, late kernel fault, cancel, double close, close after commit, each asserting no temp entry and `cleanup == CONFIRMED` | Delete one `finally` edge or one `close()`; the matrix test for that owner must fail |
| 3 | State machine and concurrency | Partly: slice 5 reserve-materialize-splice-release; slice 6 error store transitions | Written transition table in the module docstring; one lock per object where a thread may share it; no shared mutable cache across runs (`O/_faker_pool.py:1-28` run-scoped) | Barrier-forced interleaving for reserve vs release and for splice vs abort (slice 5); for slices 1 to 4, N/A: no new state beyond the existing run-scoped pool, whose tests already cover one build across batches (`tests/unit/execution/test_ooc_faker_pool_context.py`) | Reorder release before splice; the interleaving test must fail |
| 4 | Route parity and silent fallback | Yes, central | Capability is the compat gate plus `decline_reason`; every decline records a registered reason (`O/_compat.py:365-381`, `O/_faker_native.py:76-92`); the capability docs (`docs/execution-backends.md:88-99,162`) are updated in the same slice | Matrix corpus over routes x providers x modes x drivers with `compare_strict`; route proof by raising reference (P3); `test_phase_d_audit_no_legacy.py` matrix stays consistent | Remove one decline record (return None) or recompute in Python for an admitted value; the matrix test must fail |
| 5 | Referential integrity | Yes: slice 4 (key surface), slice 6 (cascade) | FK shapes enumerated in 4.3; key surface stays injective (P7); REMAP mints with the parent's own strategy, column and namespace so an orphan key equals the oracle's (`S/_orphan.py:100-155`); slice 6 cascades KEY-column errors only (`E/_pandas_adapter.py:635-653`) | Relational test per shape per policy per driver (`tests/parity/test_out_of_core_fk_parity.py:436,531,644,751,1245,1340`) re-run with an fpe key; slice 6 fixtures for key-error vs payload-error | Mint REMAP with `column=None`; the REMAP parity test must fail (wrong tweak) |
| 6 | Untrusted producer | V1 scope only: ordinary-shape values from a provider or kernel | Kernel results validated by exact type and length (`O/_fpe_native.py:225-230`, `O/_faker_native.py` string pool check, see 7.1 test 1.6); provider output that is not string fails closed before any pool is used | Field walk over `ExecutionResult` and `OutOfCoreRejection` with ordinary hostile values (str, int, list, dict, dataclass repr); hostile dunders are V2 | Return a non-string pool value from a stub provider; the slice-1 pool test must fail |
| 7 | Check-then-use on a path (TOCTOU) | N/A for slices 1 to 4: they add no file or path (the existing spill and sink paths are F25's and F22's); slices 5 and 6 add owned temp files under the run temp dir and will migrate to F25 handles when F25 lands | Existing `check_temp_disk_budget(temp_dir, ...)` and run-scoped `temp_dir` (`O/_budget.py:525`) | Slice 5 and 6 tests assert every new file is under the run temp dir and removed on every exit | Create a file beside the destination instead of under `temp_dir`; the exit-path matrix must fail |
| 8 | Weak tests and overclaimed evidence | Yes | Red-first with the RED reason asserted (the expected decline code, not an ImportError); baselines from `uv.lock` via `scripts/dev/gates.sh --baseline`; no widened tolerance, no skipped fixture, no new `SEMANTIC_DIFFERENCES.md` entry except the one slice 3b documents | Each new test file records its red run on the predecessor commit with the failing node ids and the reason; the reviewer reruns | Pass an admitted value through the Python reference and keep the compiled label; P3 test must fail |
| 9 | Behavior-preserving refactor drift | Yes: slice 1 widens five readers at once; slice 4 edits four joiners; slice 0 rebases 2461 changed lines | Characterization first: existing suites (`tests/parity/test_ooc_faker_native.py`, `test_phase_d_faker_seq.py`, `tests/native/test_chunked_nondet_faker_parity.py`, `tests/native/test_operator_tables_snapshot.py`, `tests/native/test_operator_registry.py`) stay green and are listed per slice; sentries scan the touched OOC files (`tests/sentry/test_physical_seam_disconnection.py:112-116`) | Module-size census unchanged or visibly bumped; one integration branch per slice | Change the person-name output for one value; the existing B4 suite must fail |
| 10 | Frozen contract drift | Yes | Section 8 lists the frozen surfaces; additions only; `reason-baseline.json` is never regenerated (`tests/unit/test_reason_registry.py:283-288`); `OUT_OF_CORE_SUPPORTED_STRATEGIES` widens additively (`E/__init__.py:115,132`) | `tests/unit/test_reason_registry.py::test_v1_manifest_is_append_only`; operator table snapshot updated in the same commit as `E/_operator_registry.py:214` | Remove a reason or change a pinned summary; the manifest test must fail |

## 4. Enumerations (filled from the code)

### 4.1 Exit paths of everything that owns files or state

| Owner | Where | Exits that must dispose |
|---|---|---|
| `OocFakerPoolContext` | `O/_faker_pool.py:77`; created `O/_runner.py:220-224`; closed `:341` | normal end; reservation refusal before build (`:181-189`); provider build failure inside `get_pool`; any exception in the run `try` (the `finally` at `:341` must stay the one exit); double `close()` (`:220-223` idempotent) |
| FK joiners (`ChildFkBatchJoiner`, `StreamFkJoiner`) | `O/_runner.py:572-575` `finally`, `_release_joiners` `:500-503`; `O/_stream_join.py:604-613` | normal drain; exception mid-stream; `on_stream_consumed` before the relation build (`O/_emit.py:126-132`); FAIL-policy orphan raise (`O/_runner.py:440-442`) |
| Transactional sink staging | `E/_transactional_sink.py:59-97` protocol; `commit` `:324`; `abort` `:366-380` | commit; abort on any exception (best-effort, must not raise); abort with no `write` made; cleanup evidence `CleanupState.FAILED` when the staging dir survives (`:379`) |
| Run temp dir, relation dir, staged key columns, raw-parent spill | `O/_stage.py:1-20`; `RawParentKeySpill` (`O/_stream_driver.py:274-277`); `check_temp_disk_budget` (`O/_budget.py:525`) | normal; disk budget refusal; exception before `finalize()`; reader opened before end-of-stream marker (`O/_stream_driver.py:367-371`) |
| Slice 5 resident column store (new) | planned `O/_resident_column.py` | reserve refused (decline, nothing materialized); materialize fails (provider or sampler error); splice mismatch (length or offset) aborts; abort from any other column; commit; double release |
| Slice 6 bounded error store (new, deferred) | planned `O/_row_error_store.py` on the external-sort spill discipline (`O/_external_sort.py`) | store budget exceeded (fail closed, no publication); cascade unresolved; quarantine JSONL staged then discarded (`quarantine.py:322,615`) or published (`:412`) |

### 4.2 Execution routes

- Full-frame pandas oracle: `PandasExecutionAdapter.run` (`E/_pandas_adapter.py`), the reference for every comparison.
- Sequential (SEQ) with native payload and key kernels: `E/_sequential_native.py:13-16`, key set `:128`.
- Native chunked and oracle chunked: `N/_chunked_entry.py:410` pool validation; fail-closed on row errors (`E/_chunked_oracle.py:388-395`, policy `E/_pipeline_routing.py:87-91`).
- Out-of-core, three drivers: batch runner `O/_runner.py:344` (`resident` and `sink-batch`), stream driver `O/_stream_driver.py:125` (`sink-reorder`); admission `E/_pipeline_routing_signals.py:50-77`; preflight `O/_preflight.py:41-91`.
- Mixed and isolated child routes: unchanged by this plan; listed so the matrix asserts they keep declining the same shapes (`tests/parity/test_phase_d_audit_no_legacy.py`).

### 4.3 FK shapes

- Single-column edge (`tests/parity/test_out_of_core_fk_parity.py:436`); chain and deep chain (`:531`, `:644`); fan-out multi-child (`:751`).
- Composite group (`:1245`); composite scalar child, rejected fail-closed (`O/_compat.py:267-294`, test `:1275`); composite chain, rejected (`O/_compat.py:499-517`).
- Self-referential, rejected (`O/_compat.py:482-498`); multi-table cycle, rejected (`:216-233`).
- Multi-parent first-hit-wins (`O/_multi_parent.py:1-10`).
- Children in another batch and out-of-order children: the stream join phases (`O/_stream_join.py:25-31`, `:114-121`) re-base `__decoy_row_nr` per output slice; REMAP mints per slice from `__decoy_src_i`.
- Orphan policies PRESERVE, REMAP, WARN, FAIL (`:1340`, `:1359`); FAIL pre-count before any staging (`O/_batch_join.py:325-328`).
- Edge fields: `parent_table, parent_columns, child_table, child_columns, namespace, orphan_policy` (`relationships/_graph.py:87-92`).

### 4.4 Fields of public result and gate types

- `ExecutionResult`: `outputs, timings, boundary_conversion_ms, warnings, quality_metrics, table_kinds, row_errors` (`E/_adapter.py:61-71`). Unchanged by slices 1 to 5; slice 6 adds nothing and reuses `quality_metrics["quarantine"]` (`validators/_types.py:79`).
- `RowError(column, row_index, trigger, reason)` and `RowErrorRecord(table, column, row_index, trigger, reason)` (`E/_row_errors.py:28-56`); `reason` never embeds the cell (`:17-20`).
- `OutOfCoreRejection(code, message)`, `OutOfCoreCompatibility(accepted, rejections)` (`O/_compat.py:152-168`).
- `PreparedFakerPool` (`O/_faker_pool.py:60`), `FakerColumnDecision` (`O/_faker_native.py:94`), `FpeColumnDecision` (`O/_fpe_native.py`).
- `ReasonSpec` identity `(namespace, code)` with fixed summary (`reasons/_registry.py:38-44`); STORM `ReasonRef(namespace, code)` (`storm:reasons/_identity.py:27-32`).

### 4.5 Output surfaces

- Masked tables (resident `pa.Table` or sink files), including FK-resolved child columns and REMAP-minted keys.
- `warnings`: `orphan_fk` aggregate (`O/_runner.py:547-549`), `fpe_join_group_active` (`O/_mask_group_b.py:33-38`), text_mask sub-floor (`:556-571`).
- `quality_metrics`: `code_set_corpora` (`:541-546`), backend evidence from `OocHashRun.metrics` (`O/_hash_run.py:461`), `quarantine` summary (slice 6).
- `row_errors` on the result (`E/_adapter.py:71`); bounded value-free summary (`E/_row_error_summary.py:1-12`).
- Exceptions: `ExecutionError` and `StrategyError` with registry codes and value-free messages (`O/_mask.py:203-209`, `O/_preflight.py:77-81`).
- Route decision and explain: `out_of_core_admission` primary code (`E/_pipeline_routing_signals.py:63-65`); `decide_execution_route` reasons.
- Progress channel row counts (`O/_runner.py:462`); logs; evidence manifest; vault sidecar (`collect_batch_vault`, `:488`); sink staging and publication evidence (`E/_transactional_sink.py:373-380`); quarantine JSONL (raw rows by design, protected under F06).

## 5. Design

### 5.1 Slice 0 (prerequisite, not phase 2): land the B1 seams

The B1 branch holds the durable pre-join `row_offset` threaded into `mask_batch` (`e1f96acb:O/_mask.py:449-461`), the fail-closed row-error bridge for bucketize and top_code (`e1f96acb:O/_row_error_bridge.py`, `OWN_COLUMN_ROW_ERROR_STRATEGIES`), the offset bridge for windowed_date (`e1f96acb:O/_offset_bridge.py:44` `OFFSET_BRIDGE_STRATEGIES`), and the fail-closed policy `fail_closed_on_row_errors` called per batch in both drivers (B1 runner and stream-driver diffs). The offset is the cumulative source-order row count before any join, so `row_offset + j` is row `j`'s position in the whole source, the same ordinal the join restores order by (`O/_stream_driver.py:279-283`, `O/_stream_join.py:28-31`).

What landing it needs now: a rebase over 66 commits (phase-1 B3-B7 touched `O/_compat.py`, `O/_mask.py`, `O/_runner.py`; H-1 Wave A split `O/_stream_join*`); registration of every new code (`out_of_core_row_error_dtype_unsupported` and any other literal) in `decoy_engine.reasons`, since the sentry rejects literals (`tests/sentry/test_reason_registry.py:1-20`); a census check for `O/_runner.py` (681) and `O/_stream_driver.py` (609), which B1 grows. Its own acceptance tests exist on the branch (`test_out_of_core_row_error_bridge.py`, 734 lines; `test_out_of_core_offset_bridge.py`). Owner decision: rebase B1 or re-author from its tests. Nothing in slices 1 and 4 waits for it.

### 5.2 Slice 1: address faker providers on OOC (and every native route)

One edit admits them: `E/_operator_registry.py:214` `provider_allowlist=frozenset({"person_first_name","person_last_name"})` gains `address_street`, `address_city`, `address_state`, `address_zip` (all `poolable=True`, `providers_v2/_real_registry.py:81-84`; `address_full` is `poolable=False` at `:85` and stays excluded). The frozenset is read by `C1_PROVIDER_ALLOWLIST` (`N/_real_type_admission.py:55`), the chunked admission (`:163`), the SEQ admit (`E/_sequential_native_ops.py:392-393`), the OOC decline (`O/_faker_native.py:86`, gate `O/_compat.py:365`) and the chunked deterministic admission (`N/_faker_deterministic_admission.py:42`). So the slice is a cross-route change and must prove parity on SEQ, chunked and OOC.

Pool output type. The chunked route validates the BUILT pool values are strings (`N/_chunked_entry.py:410-420` `_resolve_admitted_pools`, codes `runtime.CHUNKED_FAKER_DETERMINISTIC_POOL_NOT_STRING` `reasons/runtime/_part1.py:144-148`). SEQ makes "a separate check by the seam once the pool exists" (`E/_sequential_native_ops.py:390-391`). The OOC path (`O/_faker_native.py:259` `native_faker_array`, `O/_faker_pool.py:160` `get_pool`) has no such check visible in this read: test 1.6 below is RED-first for it, and if red the slice adds the check at `get_pool`, reusing the chunked predicate, declining with a registered reason, never passing raw or non-string values through.

Doc and message sync in the same commit: `O/_compat.py:368-370` message, `O/_faker_native.py:1-6,80`, `O/_mask.py:264-266`, `docs/execution-backends.md:98,162,178`, `tests/native/test_operator_tables_snapshot.py` and `tests/native/test_operator_registry.py` (they pin the allowlist). The registry sentry `tests/sentry/test_operator_registry_sentry.py` must stay green.

Reasons added: none. Existing `out_of_core_faker_pool_unsupported` and `faker_provider_not_native` keep declining the rest. Established solution: Faker locale providers behind a prebuilt pool and keyed index selection, already the engine's pattern (`S/_faker.py:1-20`); nothing new.

### 5.3 Slice 2 (deferred): positional REUSE faker on OOC

Depends on slice 0 (`row_offset`). B8 is not on main, so this slice builds the positional leg: in `O/_faker_native.py` add the non-deterministic REUSE case calling `sample_faker_array_positional` (`N/_operator_step.py:135`) with keys from `row_positions(row_offset, n, None, ...)` (`E/_positional_keys.py:20`; `gate_positions` is always None on OOC because `when` is declined at `O/_compat.py:238-247`), keyed on `job_seed`, never `mask_key` (`S/_faker_positional.py:9-11`, `S/_faker.py:108-112`), namespace from `faker_selection_namespace` (`S/_faker_positional.py:30`), explicit `pool_size` required. Admission predicate: a positional twin of `faker_pool_precondition_met_seed` (`N/_requirements.py:214-226`), which today requires `deterministic`. Reasons added: none; the shape keeps `out_of_core_faker_pool_unsupported` until admitted.

### 5.4 Slice 4: FPE as an OOC FK parent key (crypto-gated)

Scope claim, corrected per Codex: `_KEY_STRATEGIES = (HASH, FPE)` (`E/_sequential_native.py:128`) governs SEQ native key eligibility, not engine-wide validity. OOC already admits redact, truncate and passthrough as keys (`O/_compat.py:34`). FPE is the one injective native strategy to ADD: FF1 is a bijection on the message space for a fixed key, tweak and format (NIST SP 800-38G), so distinct parent keys stay distinct and a REMAP orphan cannot collide with a real parent unless the source keys were equal. Categorical stays off because deterministic categorical is many-to-one (`G/pool/_sampler.py:325` `_deterministic` over a pool), matching the SEQ rationale (`:13-16`), not because of a universal ban.

Crypto rule: use the existing `fpe` crate through `native_fpe_array` (`O/_fpe_native.py:191-232`, tweak `tweak_column=column` `:221`) where a per-table `TableFpePlan` admits the column (`O/_hash_run.py:172` `fpe_for`), and the existing Python reference `fpe_array` (`O/_mask_group_b.py`, tweak at `:141-154` via `build_ff1_tweak`, `T/fpe.py:155`; key via `derive(job_seed, namespace, FF1_KEY_LABEL)` `:155`) elsewhere. No new primitive, no hand-rolled tweak.

All-callsite key-mask inventory (Codex correction 1), every site passes `column=None` today:

| Site | Role | Change |
|---|---|---|
| `O/_stream_join.py:639` in `_batch_remap_values` (`:623-641`) | child inherited-key REMAP mint, stream driver | pass `column=self._edge.parent_columns[idx]`; the seed is the parent seed (`remap_seeds`, construction check `:151-162`), so `fpe_join_group` rides in its cfg |
| `O/_batch_join.py:347` in `_batch_remap_values` (`:330-348`) | child REMAP mint, batch joiner | zip `edge.parent_columns` alongside `child_columns`; pass the parent column |
| `O/_batch_join.py:565` (`:545-566`) | whole-child mint, the executable oracle twin for the joiner tests | `parent_column` is in scope (`:554`); pass it |
| `O/_relation.py:135` in `build_parent_key_relation` (`:62`, `masked_batch_fn` `:111-136`) | raw-source relation builder; production uses the aligned builder from post-rewrite output (`O/_runner.py:532-539`, `O/_emit.py:132,146`); callers are four unit test files only | thread `parent_col` into the zip and pass it; keep the `OUT_OF_CORE_FPE_COLUMN_REQUIRED` raise (`O/_mask_group_b.py:146-150`) as the fail-closed guard and assert it fires when a caller omits the column |

Payload callers already pass the column (`O/_mask.py:350-357`, `:455-469`). The `mask_column` docstring `:132-136` ("None only on key paths, restricted to column-independent strategies") is rewritten in the same commit. Admission: `_check_edge` (`O/_compat.py:518-543`) adds `fpe` to an OOC key set with the FPE preconditions reused from `_fpe_native.decline_reason` (namespace present, a resolvable `FpeConfig`; a `checksum` config stays on the Python reference, which also handles it) and keeps the composite-chain, self-referential and cycle declines. The parent column's own masking is the normal payload pass (Group B, admitted today), the child inherits the parent's masked value by join, and only the orphan mint changes. An all-out-of-charset orphan under `preserve_separators=True` must raise the same `FpeUnencryptableError`-derived `StrategyError` the oracle raises (`S/_orphan.py:21-28`; `O/_mask_group_b.py:157-161`), aborting with nothing published.

Reasons added: none. A categorical or faker parent key keeps `out_of_core_parent_strategy_unsupported` (`O/_compat.py:529-536`, pinned by `tests/parity/test_phase_d_fpe_key_route.py` and the prep test on `claude/optimistic-feynman-m5cfy5`). The seam-inventory record on that branch numbers this slice "5" and address faker "4"; this plan keeps the draft numbering.

### 5.5 Slices 3a, 3b and 5 (deferred): design pinned

- **3a code_set gen mode.** Position-keyed (`T/code_set.py:573-596`), so it does NOT use the by-value dict-map (`kernel/_code_set.py:28`, which coalesces repeated inputs). It follows the offset-bridge pattern (`e1f96acb:O/_offset_bridge.py`): dispatch `apply_code_set(..., row_index=row_offset + j)` per row, with the pinned `_CorpusRecord` across batches (`O/_mask.py:384-392`). Correct the stale comment at `O/_compat.py:388-391,400-402`. Reasons added: none (`out_of_core_code_set_shape_unsupported` remains for gen plus chapter_preserve).
- **3b code_set chapter_preserve (mask mode).** Value-keyed within a chapter bucket, so it reuses `code_set_map` with the bucketed candidate set, catching exactly `_PER_VALUE_CODE_SET_ERRORS` (`S/_code_set.py:82-89`) into the B1 `row_error_sink` with absolute indexes, and failing closed per batch. Clean data is byte-parity; dirty data fails closed where the oracle quarantines, the same accepted divergence the chunked route already carries (`E/_pipeline_routing.py:87-91`), documented in `tests/parity/SEMANTIC_DIFFERENCES.md` in the slice. Not dependent on slice 6. Reasons added: none.
- **5 single-column resident escape hatch.** Exactly one stateful faker payload column (UNIQUE, MATCH, SCALE) in the job, ungated, on a replayable source. Mechanism: reserve against `OutOfCoreBudget` (`O/_budget.py:194,356`) before materializing; a resident pre-pass reads that one column over the whole source in source order and calls the existing `PoolSampler.sample` once (`G/pool/_sampler.py:192`; UNIQUE `:266-306`, MATCH `:316`, SCALE `:318`); the normal mask pass splices `resident[row_offset : row_offset + n]` per batch. Parity is conditional, not by construction (Codex review of the concept note): the oracle keys MATCH and SCALE on `source.dropna().nunique()` over the frame it receives (`:392`) and UNIQUE on the non-null count (`:266-282`), so the resident input must be the whole column exactly as the oracle sees it; `when` is already declined on OOC, which removes the subset counterexample. MATCH and SCALE first (a two-pass O(distinct) variant is the follow-on), UNIQUE second. Reasons added: `route.out_of_core_resident_column_too_large` (reserve refused) and `route.out_of_core_resident_columns_multiple` (more than one stateful column, or a stateful FK key). Ceiling stated in the slice from measurement, not assumed; the draft's 5 to 7 GB at 100M rows figure is unverified.

### 5.6 Slice 6 (deferred): OOC per-row quarantine, design pinned before any build

Codex correction 2, now grounded: the oracle cascades KEY-column errors only. `_parent_map` excludes a parent row from the resolution map only when a `RowError` sits on one of `edge.parent_columns` (`E/_pandas_adapter.py:635-641`, EXCLUDE at `:650-653`); children keyed on it become `None` with a synthetic `RowError` carrying the parent's trigger (`E/_fk_resolve.py:126-137`, `:197`; `S/_orphan.py:37-49`). A PAYLOAD row error quarantines that row (`quarantine.py:123,212`) and never touches the parent map. Pinned semantics for the OOC design:

1. Key-error vs payload-error: a row error on a parent-key column excludes that key from the relation (`O/_relation.py` build) and cascades; a payload row error removes the row from output only. The two are tracked separately in the bounded store.
2. Mapped-key precedence: a raw key that is both mapped (from a clean row) and errored (from another row) resolves to the mapped value, matching precedence (1) at `E/_fk_resolve.py:117-126` and the documented when-gated limitation (`S/_orphan.py:51-60`).
3. Covered vs uncovered: covered triggers (the configured quarantine set) remove rows; any uncovered error fails closed with nothing published, as today (`E/_pipeline_routing.py:87-91`).
4. Ordering: quarantine records in absolute source order (`row_offset + j`), per table in topological order, so the JSONL and counts match the oracle's `_normalize_worklist` order (`quarantine.py:85`).
5. Bounded reporting and publication: the error store spills under the temp-disk budget (`O/_budget.py:525`), the JSONL is staged (`quarantine.py:322`) and published only inside the sink commit (`:412`) or discarded on abort (`:615`); the summary is `QuarantineSummary` (`validators/_types.py:79`).

Overlaps, with the dependency order and what is NOT duplicated: F06 r7 already refuses parse-stage parent-row quarantine under non-fail policies on every route and refuses `row_quarantine` on forced OOC (`feat/f06-declared-parsing:src/decoy_engine/declared_casting/_eligibility.py:168`; `docs/declared-casting.md:100`), so slice 6 does not add a second refusal; when 6c proves the cascade, it REMOVES that refusal for OOC by adding the capability to `ROUTE_CAPABILITIES`, which F06's sentry requires to come with a wired applier and a parity test. Slice 6d does not build publication: it consumes F22's arena and sink state table (`plan/f22-publication-replan` section 5) once landed. The error store does not build scratch ownership: it uses the run temp dir now and migrates to F25 handles when F25 lands. Order: F06 merge, then F25 then F22 (owner order 2026-10-30), then 6a-6d. Reasons added: decided in the 6 design slice; candidates `runtime.out_of_core_quarantine_store_exceeded`, `runtime.out_of_core_cascade_unresolved`; outcomes reuse `quality_metrics["quarantine"]`.

### 5.7 Design notes (dev-rules)

Slices 1 to 4 add no new structure. Slice 5 adds one module (`O/_resident_column.py`: reserve, materialize, splice, release as a context manager; one place for the ceiling arithmetic) following the reserve-before-materialize pattern of `OocFakerPoolContext` (`O/_faker_pool.py:160-201`). Slice 6 adds the bounded error store on the external-sort spill discipline (`O/_external_sort.py`) and nothing else new. Established solutions: Faker pools with keyed index selection (engine pattern); NIST SP 800-38G FF1 for FPE through the compiled `fpe` crate; DuckDB-style spill and re-read for staging (`O/_stage.py:1-12`); Spark `badRecordsPath` side channel for row errors (`E/_row_errors.py:3-9`).

## 6. Reuse inventory

| Existing thing | Where | Used by |
|---|---|---|
| F29 reason registry and sentries | `src/decoy_engine/reasons/`; `tests/sentry/test_reason_registry.py`; `tests/unit/test_reason_registry.py` | every slice (P8); new route declines go in `reasons/route/_added.py` with an OOC owner, pattern `reasons/route/_part1.py:191` |
| OOC compat gate and preflight | `O/_compat.py:171-341`, `:470-543`; `O/_preflight.py:41-91`; `E/_pipeline_routing_signals.py:50-77` | slices 1, 2, 3, 4, 5 |
| Per-table native plan | `O/_hash_run.py:137` `TableHashPlan`, `:172` `fpe_for`, `:184` `faker_for`, `:323` `prepare_table` | slices 1, 2, 4 |
| Run-scoped faker pool | `O/_faker_pool.py:77-229` | slices 1, 2, 5 |
| Compiled index kernels | `N/_operator_step.py:73` and `:135`; positional keys `E/_positional_keys.py:20` | slices 1, 2 |
| FPE kernel and reference | `O/_fpe_native.py:191`; `O/_mask_group_b.py:141-155`; `T/fpe.py:155` | slice 4 |
| Oracle REMAP and cascade | `S/_orphan.py:100`; `E/_fk_resolve.py:74,177,197`; `E/_pandas_adapter.py:591-653` | slices 4, 6 |
| Arrow code_set map | `kernel/_code_set.py:28` | slices 3a (not used, by design), 3b |
| Budget and spill | `O/_budget.py:194,356,525,569`; `O/_external_sort.py`; `O/_stage.py` | slices 5, 6 |
| Transactional sink | `E/_transactional_sink.py:59-97,366-380` | P5 tests in every slice |
| Parity harness | `tests/parity/_strict_compare.py:26-36`; `test_out_of_core_fk_parity.py:101-113`; `test_phase_d_d1a_hash_baseline.py:41-44`; `_ooc_faker_data.py:132`; `_d2c_fpe_data.py:104` | every slice |
| Sentries | module size `tests/sentry/test_module_size.py:54-55`; seam `test_physical_seam_disconnection.py:112-116`; methodology `test_methodology_citations.py`; operator registry `test_operator_registry_sentry.py` | every slice |
| Gate scripts | `scripts/dev/bootstrap.sh`, `scripts/dev/gates.sh --baseline` (same venv, `uv.lock`) | evidence |

New machinery, with the reason: slice 5's resident store (no existing owner for a whole-column resident result on OOC); slice 6's bounded error store (B1's list sink is O(rows) by design and fails closed on the first error).

## 7. Hostile corpus (acceptance tests, red first)

### 7.1 Slice 1: `tests/parity/test_native_address_faker.py`

The first test of the first slice is written red: `test_address_provider_ooc_matches_oracle[address_street-resident-None]` fails at `a6e5c252` because preflight raises `ExecutionError(code="out_of_core_faker_pool_unsupported")` (`O/_preflight.py:77-81`, gate `O/_compat.py:365-371`). The red run asserts that code, not a generic failure.

1. Parity matrix: provider in the four address providers x driver in `resident, sink-batch, sink-reorder` x `batch_rows` in `{None, 3, 1}` x a fixture with a null at position 4 and `batch_rows=4` (split before the null) x a one-row trailing batch. `compare_strict` all-True against the oracle (P1, P2). Red on predecessor.
2. Route proof: the oracle run installs a raising marker on `_faker_native.sample_faker_array` (`_ooc_faker_data.py:132`) and the OOC run asserts the compiled selection was consumed (`FakerConsumedWitness`); a Python recompute for an admitted value fails the test (P3).
3. Cross-route parity: the same fixtures on SEQ (`run_sequential`) and chunked (native entry) against the oracle, since the one edit widens all three; existing `test_phase_d_faker_seq.py` and `tests/native/test_chunked_nondet_faker_parity.py` stay green.
4. Pool identity: the same provider and locale across two tables builds once (`faker_pool.builds == 1`); a different locale builds twice (`G/pool/_identity.py:26`).
5. Negatives, each asserting the registered code: `address_full` declines; a composite provider declines; `person_dob` declines or fails closed as a non-string pool (never a raw value).
6. Pool output type on OOC (RED-first probe): a stub provider registered to return integers under an allowlisted name must fail closed before any batch with a registered reason; if OOC has no such check (5.2), this test is the one that drives adding it.
7. Marker corpus: `CELLMARK_<rand>` in a source cell never appears in any exception, warning, metric, log or reason on any route (class 1).
8. Fail closed: a kernel fault injected on the third batch with the Parquet sink leaves no staged file and `cleanup == CONFIRMED` (P5, pattern in `test_ooc_faker_native.py` "late kernel fault").

Non-vacuity: each positive test records its red node id and reason on `a6e5c252`; each negative test is paired with a mutation (allowlist entry removed; marker removed) that must make it fail.

### 7.2 Slice 4: `tests/parity/test_ooc_fpe_parent_key.py`

Red on `a6e5c252`: forcing OOC on the `fpe_key_shape` fixture (`tests/parity/_d2c_fpe_data.py:104-131`) raises `out_of_core_parent_strategy_unsupported` (pinned today by `test_phase_d_fpe_key_route.py`).

1. Orphan-policy parity: PRESERVE, REMAP, WARN, FAIL on the A.k -> B.k -> C.k fpe chain, every driver, splits `{None, 3, 1}`; `compare_strict` all-True; REMAP is the load-bearing case and fails any mint with `column=None` (wrong tweak).
2. Both joiners and both output modes: the batch joiner (`O/_batch_join.py:347`) and the stream joiner (`O/_stream_join.py:639`) each produce the same REMAP values; the whole-child twin (`O/_batch_join.py:565`) equals the per-batch mint.
3. `fpe_join_group`: a parent key with a join group mints with the group tweak on both routes (`S/_fpe.py:154-159`, `O/_mask_group_b.py:141-142`).
4. Unencryptable orphan: `preserve_separators=True` with an all-out-of-charset orphan under REMAP raises the same `StrategyError` code on both routes and publishes nothing (P5).
5. Composite and multi-parent fpe keys: either parity with the tweak threaded, or the existing declines (`O/_compat.py:499-517`, `O/_multi_parent.py`) asserted by code; never silently admitted.
6. Negatives: categorical and faker parent keys still decline with `out_of_core_parent_strategy_unsupported`; the standing categorical guard from the prep test is re-authored here.
7. Raw builder guard: `build_parent_key_relation` with an fpe seed and no column raises `OUT_OF_CORE_FPE_COLUMN_REQUIRED` (`O/_mask_group_b.py:146-150`) unless the slice threads the column there too; either way asserted.
8. Native and reference agreement: with the companion present, the parent payload column masks on `native_fpe_array` while the REMAP mint uses `fpe_array`; outputs agree byte for byte (the same key and tweak derivation), and the companion-absent run agrees too (`hide_companion` pattern).
9. Marker corpus and fail-closed as in 7.1 items 7 and 8.

### 7.3 Slices 0, 2, 3a, 3b, 5, 6 (deferred; tests named now)

- Slice 0: the B1 branch's `test_out_of_core_row_error_bridge.py` and `test_out_of_core_offset_bridge.py` re-run green after rebase, plus the F29 sentry green with every new code registered.
- Slice 2 `tests/parity/test_ooc_faker_positional.py`: irregular-split parity (the offset proof), one pool build, `job_seed` keying (a swapped `mask_key` fails), nulls preserved, `when`-gated still declined.
- Slice 3a `tests/parity/test_ooc_code_set_gen.py`: irregular-split parity; pinned corpus record across batches; `namespace` required (`T/code_set.py:437`).
- Slice 3b `tests/parity/test_ooc_code_set_chapter_preserve.py`: clean-data parity; a chapter-absent code after a batch boundary fails closed with the absolute row index and no output; the same fixture quarantines on full-frame, recorded in `SEMANTIC_DIFFERENCES.md`.
- Slice 5 `tests/parity/test_ooc_resident_escape_hatch.py`: per-mode parity across irregular splits (splice alignment); tiny-budget decline with the registered code and no output; two stateful columns decline; UNIQUE capacity error identical to the oracle's `uniqueness_impossible`; memory witness: peak RSS bounded by the declared ceiling on the large tier (pattern `tests/unit/execution/test_ooc_spill_reader_rss.py`); barrier-forced release-vs-splice interleaving.
- Slice 6 `tests/parity/test_ooc_quarantine.py`: per fixture in 5.6 items 1 to 5, OOC output equals the oracle's QUARANTINED output including the cascade; bounded store witness; uncovered error publishes nothing; key-error vs payload-error fixtures; the F06 capability sentry green once the capability is claimed.

### 7.4 Ten ways a gate reviewer will break it (each has a test above)

1. REMAP minted with the wrong tweak (7.2.1). 2. Python recompute behind a compiled label (7.1.2). 3. A one-row trailing batch or a split before a null changes a position-keyed value (7.1.1, 7.3). 4. A non-string provider poisons a pool (7.1.6). 5. A value in a reason message (7.1.7). 6. Staging left after a late fault (7.1.8). 7. Categorical admitted as a key by reading the payload set (7.2.6). 8. The raw relation builder reached with no column (7.2.7). 9. Two stateful columns doubling resident memory (7.3 slice 5). 10. A payload error excluding a parent key (7.3 slice 6).

## 8. Frozen contracts touched

| Contract | Where | Effect |
|---|---|---|
| F29 reason manifest `reason-manifest-v1.json` | `tests/fixtures/f29/`; `tests/unit/test_reason_registry.py:85-89` append-only | Additive only: slices 5 and 6 add identities; slices 1 to 4 add none. `REGISTRY_VERSION` (`reasons/_registry.py:22`) bump follows the F22 convention (1.0.0 to 1.1.0) once per release, coordinated with F22, not per slice. |
| F29 baseline `reason-baseline.json` | frozen by sha (`tests/unit/test_reason_registry.py:283-288`) | Never regenerated. A slice that runs `_reason_baseline.py --write` has broken the contract. |
| `decoy-reasons/v1` identity form | `storm:reasons/_identity.py:19` | Untouched; new identities resolve through it. |
| `OUT_OF_CORE_SUPPORTED_STRATEGIES` public alias | `E/__init__.py:115,132`; `O/_compat.py:149` | Unchanged by slices 1 and 4 (no new payload strategy); widened additively by 2, 3a, 3b, 5. |
| Operator table snapshot | `tests/native/test_operator_tables_snapshot.py`, `test_operator_registry.py` | Slice 1 updates the snapshot in the same commit (additive provider entries). |
| `ExecutionResult`, `RowErrorRecord`, `RelationshipEdge`, `TransactionalSink` | `E/_adapter.py:61-71`; `E/_row_errors.py:47-56`; `relationships/_graph.py:87-92`; `E/_transactional_sink.py:59-97` | Unchanged. |
| Public signatures | `tests/unit/execution/test_r3_public_signature_freeze.py` | Unchanged; slice 5 derives its reserve from the existing budget and adds no public parameter. |
| Release phase | `src/decoy_engine/release.py` `RELEASE_PHASE = "pre-ga"`; tags up to `v0.16.0` exist | Pre-GA: additions unfrozen, removals are hard deletes; the compatibility contract binds at the GA flip. |

F31 assumptions: none. No F31 boundary exists on main; this plan keeps the existing value-free message convention and the owner's three-class rule (schema names allowed, cell values never).

## 9. Slices

| Order | Slice | Repo | Prereq on main | V1 | Serves | Ships alone | Size |
|---|---|---|---|---|---|---|---|
| 1 | 1 address providers | engine | yes (B3, B4 present) | V1-REQUIRED | F05 route parity and F11 byte identity for realistic PHI addresses on the bounded route; the A-2 address gap | yes | 1 line of production code, 1 test file, 6 doc/message edits, 2 snapshot updates |
| 2 | 4 FPE FK parent key | engine | yes (no B1 needed) | V1-REQUIRED, crypto-gated (Codex plus Dennis) | F05 "route changes cannot weaken relationships or determinism" and F11 route byte identity for reversible FK keys; the roadmap names it | yes | 4 callsites, 1 gate edit, 1 docstring, 1 test file; watch `O/_relation.py` at 600 |
| 3 | 0 land B1 | engine | rebase of `e1f96acb` | prerequisite for 2, 3a, 3b, 5, 6; owner decides whether it is V1 | bucketize, top_code, windowed_date on OOC | yes | 2461 lines on the branch plus F29 registration |
| 4 | 3b chapter_preserve | engine | slice 0 | DEFERRABLE | clean-data parity only; full-frame fallback is correct today | yes, after 0 | small |
| 5 | 3a gen mode | engine | slice 0 | DEFERRABLE | same | yes, after 0 | small |
| 6 | 2 positional faker | engine | slice 0, slice 1 | DEFERRABLE | SEQ and chunked already serve it; OOC value is memory only | yes, after 0 and 1 | medium (builds the positional leg) |
| 7 | 5 resident escape hatch | engine | slice 0, slice 1 | DEFERRABLE, re-enter only after the measured one-job prototype the concept review demands | efficiency only; correctness is served by the full-frame fallback | yes, after 0 | 1 new module, 2 reasons, memory witness |
| 8 | 6 OOC quarantine (6a lineage, 6b store, 6c cascade, 6d publication) | engine | slice 0, F06 merged, F25 then F22 landed | DEFERRABLE | F06 "same parsing semantics on every supported route" is met today by a registered refusal; true dirty-data parity is the V2 upgrade | each part alone, in order | 3 to 6 weeks, own plan document at build time |

After-V1 list with reasons: slices 2, 3a, 3b (correct fallback exists, no F05/F06/F11 expectation unmet); slice 5 (efficiency, prototype-gated, needs B1); slice 6 (F06 r7 already fails closed with a registered reason; F22 and F25 not landed; largest risk); slice 0 is an owner call since it carries phase-1 value on its own.

Per-slice risk: all R2. Slice 4 additionally crypto-gated.

### 9.1 Commands (from `uv.lock`, one environment; record `python -V`, DuckDB and PyArrow versions in the evidence)

```bash
uv sync --frozen --extra dev
```

```bash
uv run pytest tests/parity/test_native_address_faker.py -q -p no:cacheprovider
```

```bash
uv run pytest tests/parity/test_ooc_faker_native.py tests/parity/test_phase_d_faker_seq.py tests/native/test_chunked_nondet_faker_parity.py tests/unit/execution/test_ooc_faker_pool_context.py tests/native/test_operator_tables_snapshot.py tests/native/test_operator_registry.py -q
```

```bash
uv run pytest tests/parity/test_ooc_fpe_parent_key.py tests/parity/test_out_of_core_fk_parity.py tests/parity/test_phase_d_fpe_key_route.py tests/parity/test_ooc_fpe_native_real.py tests/unit/execution/test_out_of_core_relation.py tests/unit/execution/test_out_of_core_stream_join.py tests/unit/execution/test_out_of_core_batch_join.py -q
```

```bash
uv run pytest tests/sentry -q
```

```bash
scripts/dev/gates.sh --baseline tests/parity/test_native_address_faker.py
```

Red-run evidence: check out the predecessor commit in a second worktree, run the new test file there, record the failing node ids and the asserted reason code. Regenerate `docs/capability-matrix.md` only if `scripts/gen_capability_matrix.py` output changes (it lists strategies, not route eligibility, `docs/capability-matrix.md:10-11`).

## 10. Worker evidence rules

- Name the class of every finding (section 3) and fix the class, not only the instance.
- Write the generated tests first (parity matrix, exit-path matrix, marker corpus, interleaving where state exists). Record them red on the predecessor commit with the asserted reason.
- Before pushing, attack your own diff: mutate each new mechanism once (the mutation column in section 3) and show the test goes red.
- Run baselines from `uv.lock` in the same environment as the branch (`scripts/dev/gates.sh --baseline`). State what you did not run.
- Never widen a threshold, skip a test, weaken an assertion, add a parity exception or raise a census number to make a slice pass.
- If a defect of a class already in the catalog recurs, stop and report it with a class-level proposal; do not patch a third time.
- Verify the slice's named prerequisite at the pinned commit before starting (`git merge-base --is-ancestor <prereq> HEAD`); slice 1 and slice 4 need `a39c6486` only.

## 11. Pre-mortem

- **Slice 1 regresses a route it did not test.** One frozenset feeds SEQ, chunked and OOC; the cross-route matrix (7.1.3) and the five listed suites are mandatory, not optional.
- **A locale returns a non-string for an address provider.** The OOC pool check may not exist (5.2); test 7.1.6 is red-first and drives the fix.
- **Slice 4 ships with the tweak threaded in three of four sites.** The inventory (5.4) and tests 7.2.2 and 7.2.7 cover all four; the `OUT_OF_CORE_FPE_COLUMN_REQUIRED` guard stays as the backstop.
- **Slice 4 grows `O/_relation.py` past 600 without a census entry.** The sentry fails; add the entry at the exact LOC or move the hash special-case (`O/_relation.py:121-133`) out.
- **B1 rebase conflicts in `O/_compat.py` and `O/_mask.py` are resolved by hand and drift.** Slice 0 re-runs B1's own 1000-plus lines of tests and the F29 sentry; no code lands without them.
- **Slice 5 parity breaks on a non-replayable source or a `when` gate.** Admission requires a replayable source and no `when` (already declined); the irregular-split test catches a batch-local splice.
- **Slice 6 cascades payload errors.** Pinned in 5.6 item 1 with a dedicated fixture.
- **Reasons added as literals.** The choke-point sentry fails; declare in `reasons/route/_added.py` or a runtime part with an OOC owner.
- **Baseline fixture regenerated.** The sha test fails; revert.

## 12. Open questions for the owner

1. Slice 0: rebase `claude/blissful-goldberg-mkwpdk` (66 commits behind, F29 registration needed) or re-author B1 from its tests? Is B1 itself V1?
2. Namespace for new OOC declines: `route` (handoff wording) or `runtime` (where every existing OOC compat code lives, `reasons/runtime/_part1.py:265,286,350`)? This plan proposes `route` for reroute declines and `runtime` for in-run failures.
3. Slice 5 is marked DEFERRABLE although the roadmap row names the resident escape hatch; confirm, or name the one representative job for the measured prototype that would re-enter it.
4. Slice 6 is marked DEFERRABLE with its design pinned; confirm that F06 r7's registered refusal is the V1 answer for OOC quarantine.
5. Should slice 4 also admit composite fpe keys (same tweak threading) or keep the composite-chain decline for V1?
