Status: review

# Codex hostile review of the f22-replan (2026-10-30)

Reviewer: Codex (gpt-5.6-sol), read-only, against the plan at its pushed head. This review carries no authority; the orchestrator re-verifies.

Verdict: **NO-GO. 3 BLOCKER / 5 HIGH / 2 MEDIUM / 0 LOW.**

Reviewed [the plan](docs/plans/2026-10-30-f22-publication-replan.md) against [the contract](/home/cam/obsidian-vault/30_Resources/Decoy-Plan-Contract-and-Defect-Catalog.md), F22 `2fa4f8a5`, and F29 `fdd08a89`. I spot-checked 61 distinct anchors and derived inventories from both commits.

## Blocking findings

1. **BLOCKER: Class 1/6 is not closed: `to_report()` is not a containment boundary.** Plan §§4.4, 4.5, 5.3, 6.3, lines 147–167 and 200–206.

   The plan explicitly leaves raw public objects and `dataclasses.asdict()` unsafe. Public fields still carry paths or values:

   - `AbortReport.retained_path/resource`: `2fa4f8a5:publication/_retention.py:39-40`
   - `ResolvedGeneration.path`: `_managed.py:70-77`
   - `IsolatedRunResult.committed_output_dir`, outputs, metrics and row errors: `_isolated_common.py:362-385`
   - `PublicationEvidence.stored_bytes` has table names as keys: `fdd08a89:_publication_observer.py:42-49`
   - `IsolatedRunResult.error` uses `type(exc).__name__`: `fdd08a89:_isolated_common.py:238-259`
   - `approve_ref` is shape-only, not provenance-aware: `reasons/_record.py:72-76`
   - `PublicationEvidence.context(table=...)` feeds table text through that shape check: `_publication_observer.py:51-62`
   - Quarantine logging emits final path, staging path and raw exception: `2fa4f8a5:quarantine.py:498-504`

   Failure: a hostile exception class name, raw cause, path, or valid-shaped child table reaches a result, log, traceback or reason while every `to_report()` test stays green.

   Required plan correction: define one boundary over every enumerated outward surface, with exact-type/provenance capabilities; either remove unsafe public fields or state and prove a narrower contract that the catalog permits.

2. **BLOCKER: Class 2/4/7 is not closed: producers still create paths outside the arena.** Plan §§2, 5.1, 6.4, lines 57–61, 171–180, 243–248.

   Two concrete escapes:

   - Managed publication creates `CURRENT.<uuid>.tmp` directly under the dataset root: `2fa4f8a5:publication/_managed.py:402-405`. The proposed sentry scans only `tempfile` APIs, so this passes.
   - Retention creates `policy.store/<uuid>/payload`, `holds`, and `meta.json`: `_retention.py:111-137`. The design never reconciles retention with “arena disposal is the only cleanup.”

   Holding an arena fd also does not by itself remove the original directory if an attacker renames it and replaces the old name with a symlink. The proposed swap test can protect the sentinel while leaking the renamed arena.

   Failure: pointer-write failure, rejected-output retention, or arena rename leaves attempt-owned material outside the arena with all proposed sentries green.

3. **BLOCKER: Class 3 RECOVERING semantics are incomplete and contradictory.** Plan §§5.2 and 6.2, lines 186–198 and 227–237.

   - I1 says validation is rejected as busy, then says the next commit observes that failed validation. A non-latched rejected call cannot also mutate the request.
   - Successful recovery from OPEN or SEALED is unspecified.
   - Deferred abort runs only when recovery returns OPEN/REFUSED/FAILED, but I3 demands ABORTED after recovery without restricting the prior/result state.
   - A recovery that finds a binding from UNCERTAIN becomes SEALED; a racing abort must not turn that published attempt into ABORTED.
   - No restart case exists. A fresh sink begins OPEN after process death, even if backend reconciliation finds an existing binding.

   Failure: the process dies after binding but before updating memory; a fresh sink reconciles successfully yet remains writable or recommittable.

   Required correction: specify `(prior state, operation, result/exception, deferred abort) → final state` exhaustively, including restart, then test validate/commit/abort/second recovery against each recovery outcome.

## High findings

4. **HIGH: Required enumerations are incomplete.** Plan §4.

   - Exit paths omit arena creation/owner-file failures, work-root failures before `try/finally`, payload/source/chmod failures, retention lifecycle, reader holds, lookup/reconcile exits and disposal failure variants.
   - “Eight routes” actually lists nine items if full-frame and mixed are separate; managed unit is a sink variant, not an execution route.
   - Public-result inventory lists only the receipt fields of `ExecutionResult` and `GenerationResult`, not every dataclass field. It also omits `PreparedPublication`, `ChildPreparationEnvelope`, `ReasonRecord`, `ReasonContext`, `MemoryEvidence`, `RowErrorRecord`, `ArtifactSummary` as a reachable type, and `PublicationEvidence`.
   - Output surfaces omit raw public objects, repr, traceback/cause, dynamic-class logs, quarantine logging and publication evidence.
   - FK enumeration does not cover composite, self-referencing, cross-table and children-in-another-batch across stages/routes as the contract requires.

5. **HIGH: Five class proofs are vacuous or incomplete.**

| Class | Closure | Mutation proof |
|---|---|---|
| 1 output | No: only serializer-safe | No |
| 2 cleanup | No: pointer temp, retention, renamed arena, restart | Partial |
| 3 concurrency | No: contradictory and missing transitions | Partial |
| 4 route/fallback | Arena parity only; no registered-fallback proof | No |
| 5 FK | One parent/child case only | No |
| 6 untrusted producer | Row-error cap, but no complete exact-type entry boundary | Partial |
| 7 TOCTOU | Sentinel protected; renamed original can leak | No |
| 8 evidence | “Delete assertion” has no independent meta-test | Partial |
| 9 refactor drift | Moving a file outside fixed scan roots makes the sentry miss it | No |
| 10 frozen contract | Plausible snapshots, but missing public contracts/versioning | Partial |

   K5 is wrongly claimed red: after the prep rename, data is at the parent-leased path and baseline cleanup should remove it. K6 has no described hook: the parent calls `communicate()` before reading the envelope (`_isolated_spawn.py:86-120`), and none of the listed wrappers blocks after envelope write.

6. **HIGH: F29 integration is not executable enough.** Plan §§5.4, 7 and 9.

   Missing:

   - exhaustive receipt/busy/cleanup token → `ReasonRecord` identity mapping;
   - provenance for config identifiers versus child identifiers;
   - exact observer update ordering and removal of duplicate state writes;
   - resulting `PublicationEvidence` schema and table-name policy;
   - producer-inventory entries for every new typed reason;
   - registry minor-version bump. F29 explicitly requires a minor bump when adding identities: `fdd08a89:tests/unit/test_reason_registry.py:151-159`.

7. **HIGH: Slice order is not independently gateable.** Plan §10.

   - F1 I3 needs arena disposal introduced in F2.
   - F1 busy reasons depend on registry additions assigned to F4.
   - F3 asserts `reason.context.cleanup_state`, whose mapping is assigned to F4.
   - F0 cannot make all ported F22 suites green before execution integration exists.
   - F5 defers documentation until after five slices that are claimed separately gateable.

8. **HIGH: Sizing and bloat are understated.**

   F0 is not “~0 new LOC” relative to F29. It ports approximately:

   - 2,620 source LOC
   - 4,607 unit-test LOC
   - 239 sentry LOC
   - 7,466 total lines

   F29 already has near-limit modules that later slices touch: `_isolated_run.py` 622, `_sequential.py` 696, out-of-core `_runner.py` 681, and `quarantine.py` 645. The proposed split modules themselves fit under 600, but the plan gives no per-module post-slice census for these existing files.

## Grounding defects

**MEDIUM: Wrong, missing or phantom citations/claims:**

- `_managed.py:385` is a function header; the pointer switch is at `:418`.
- `_isolated_commit.py:124` is the function header; the sibling path is minted at `:129-130`.
- `AbortReport.retained_path/resource` are reversed in §1.2; they are lines 39 and 40 respectively.
- `api/jobs/memory_evidence.py:4` is not in either engine commit.
- The §9.2 `main:` `_isolated_common.py` claim has no line.
- `docs/handoffs/tools/plan-lint` is phantom in this worktree. The installed command is `/home/cam/bin/plan-lint`; it passes the plan.
- The state inventory says eight flags but lists nine state-bearing items.
- The FK “one rename” citations omit immutable publication’s actual rename at `_local_directory.py:379`.
- K2–K5 “red today” is false for K5; K6 lacks a reachable injection seam.

## Ten gate attacks

| Attack | Red-first test? |
|---|---|
| Kill real child mid-spill | Yes |
| Kill after prep rename | Named, but baseline/red claim is wrong |
| Kill after envelope before exit | No viable hook specified |
| Retain rejected staging outside arena | No |
| Fail managed pointer-temp write outside arena | No |
| Rename arena and replace its old name with a symlink | Partial; original may leak |
| Recovery versus validation failure | Named, but oracle contradicts busy semantics |
| Crash after bind; fresh process recovers then commits | No |
| Hostile exception class/path appears in result, cause or log | No |
| Valid-shaped child table enters `object_ref` or `stored_bytes` | No provenance test |

## Frozen contracts and build forecast

The plan omits `PublicationEvidence` from frozen contracts, lacks the F29 registry version bump, and has no full snapshot for the changed `IsolatedRunResult`/report behavior. The required rules line also does not list the canonical development rulebooks.

A remote worker is unlikely to finish within two remediation rounds. A third round would be caused by:

- reconciling retention and managed pointer temps with the arena invariant;
- defining restart recovery and successful RECOVERING transitions;
- replacing shape-only identifier approval and dynamic class names;
- splitting near-ceiling F29 modules;
- repairing slice dependencies and test oracles.

This needs a class-level re-plan, not implementation-time patching.
tokens used
174,681
