Status: review only. No authority; the orchestrator re-verifies.

# F31 shared safe-output boundary: Opus hostile plan review

Plan: `docs/plans/2026-10-30-f31-shared-safe-output-boundary.md` on `plan/f31-safe-output` at `a909118` (821 lines). Baselines used: engine `fdd08a8` (equal to `origin/main` today), CLI `decoy` `574aa1e`, Platform `051ff2b` (the plan's pins; Platform `origin/main` has since moved to `ea68d8e`, not re-diffed). STORM reference: `claude/eager-clarke-14deh1` in all three repos. Contract: `DEFECT-CATALOG.md` and `HOSTILE-PLAN-REVIEW.md` on decoy-platform `handoffs/queue`.

## Verdict: NO-GO

Counts: **2 BLOCKER, 8 HIGH, 5 MEDIUM, 3 LOW.**

The mechanism design is strong: closure-held provenance, sealed payload, exact type table, frame-hygiene rules, child FD bootstrap. It is not the problem. The problem is the scope. The plan defines "ordinary output" so widely that it overrides contracts other teams already accepted, while saying those contracts do not change. It also defines no meaning for events that cross a process or a database. F36, F32, F30 and STORM all depend on both of those things. These are owner decisions, not worker fixes. If a worker builds the plan as written, the gate will find B1 and B2 in round 1, and the remediation will reopen them in round 2.

`plan-lint plan` prints `OK` (run with `python3 -I` from the queue-branch copy). The lint checks that sections exist. It does not check that their content is true.

## 1. Grounding

Range check: a script resolved all 237 engine `file:line` citations in the plan. All of them exist and are in range. Content check: 31 citations read by hand. 27 match the plan's claim. Exceptions below.

Content verified: `pyproject.toml:5-13` (0.7.0, `>=3.10,<3.13`); `reasons/_registry.py:24-44`; `internal/logging.py:16-60`; `connectors/postgres_snapshot.py:505-526`; `reason_adapter.py:1-16`; `_progress_events.py:86-98`, `:273-280`, `:430-477` (seven variants, legacy keys), `:553-599`; `tests/unit/progress/test_versioned_contract.py:37-78`; `context.py:22-30`, `:227-240`; `execution/_isolated_worker.py:47-70`; Platform `api/audit/logger.py:83-92`, `api/evidence/assembly.py:1887-1888`, `api/jobs/routes_runtime_facts.py:76-87`, `api/generation/generation_parent.py:189-200`, `generation_child.py:277-291`, `api/cloud/io.py:134-137`, `api/jobs/v2_cloud_materialize.py:409-410`, `api/cloud/pg_probe.py:85-86`, `api/cloud/_pg_probe_child.py:83-84`; CLI `scan.py:773-798`, `subset.py:278`, `fit.py:321`, `templates.py:113`, `evidence.py:118`; STORM `_projections.py:1-30`, `fidelity_projection.py:1-12`, engine `phase.py:459-478`.

Wrong, phantom or missing:

- **Phantom prerequisite.** `feat/f43-ci-integration` (plan:135, :370, :381, :645) does not exist on any remote. `git ls-remote` against decoy, decoy-engine and decoy-platform finds only `decoy` `plan/f43-ci-integration` (`a750ca0`), and that branch has no `src/decoy/ci/` files. `SafeFinding`, `ci/contracts.py:1-11` and `ci/engine_logging.py:1-12` are unverifiable. See H4.
- `decoy/src/decoy/cli/run.py:207-219` (plan:128, cited as "progress, errors") is the help text of the `--json` and `--quiet` options, not an emitter.
- The plan says `connectors/postgres_snapshot.py` "maps exceptions to a closed code and type" and tells the worker to "Preserve this pattern" (plan:114). The code does `cause_type=type(exc).__name__` at `:242`, `:256`, `:327`, `:408`, `:491` and `:515`, and joins it into the error text at `:130-131`. That is the forbidden pattern. See H6.
- The Slice 0 self-check runs `tests/sentry/test_value_free_boundary.py` (plan:528). That file does not exist in the engine; it is a CLI test name. Slice 1 is the slice that creates an engine version of it.
- `scripts/dev/gates.sh` and `scripts/dev/bootstrap.sh` (catalog section 6) do not exist in the engine. The baseline evidence rule cannot be met as worded.

Enumerations:

- **Exit paths** (plan:170-174). Correct for the sinks that exist today. Missing: the new process-owned install has no uninstall or test-teardown exit (M1).
- **Routes** (plan:176-180). Correct for engine execution routes. Missing the **re-ingest route**: an event read back from a database, a log file or a child envelope (B2).
- **FK shapes** (plan:182-186). Correct. Plan:157 cites an "FK-shape output matrix" that the acceptance section never defines (L2).
- **Fields** (plan:188-197). Incomplete. These root-public types are missing: `QualitySummary`, `FkValidityReport` and `CompositeCoherenceReport` (`validation/post/_types.py:36-75`), and the attributes of every public exception class (for example `PostgresSnapshotError.cause_type`). `QualitySummary.sampled_values: dict[str, list[Any]]` (`_types.py:71`) holds raw cell samples. `FkValidityReport.relationship` (`:38`) holds `"parent_table.parent_columns -> child_table.child_columns"`. See H7.
- **Output surfaces** (plan:199-204, matrix :412-416). Platform is missing the Prometheus exporter (`api/observability/metrics.py:208-266`, `render()` at `:351`), the server-log SSE tail that re-reads log files (`api/server_logs/tail_stream.py`), and the job SSE that replays the `Job.log` text column from the database (`api/jobs/routes_streaming.py:86-101`).

## 2. Findings

### BLOCKER

**B1. The aliasing scope silently breaks contracts the plan says stay frozen. Classes 10, 9, 1.**
Plan sections: Guarantee (plan:43, "A short or clean-looking source identifier is still a value and is aliased"); Boundaries (plan:64, which counts `--json`, progress, job events and API bodies as ordinary output); matrix plan:416 ("API success and error bodies"); frozen contracts plan:229 ("changes the producer representation, not published keys"); plan:383; green condition 19 (plan:749, "OpenAPI plus `decoy-web` contracts remain unchanged").
Evidence: three contracts already accepted put short, clean column names into output on purpose:
- STORM CLI `_projections.py:12-17`: "A short clean-charset column name is schema and is output by design".
- Platform `fidelity_projection.py:10-12`, whose code is already accepted.
- CLI `scan.py:150`, which documents `decoy scan fields scan.json --json | jq '.fields[].name'`.

Platform joins runtime facts and evidence on real node ids. See `routes_runtime_facts.py:46-100` (grouping `Job.log` by STEP == `node_id`) and `assembly.py:1887-1888` (`ctx.logger.step(name=<node_id>)`). Plan:274 neuters `Logger`, so that join input disappears. The progress v0 contract has `table` and `column` keys (`test_versioned_contract.py:37-62`).
Failure scenario: the worker aliases names as the plan says. The web fidelity view then shows `field-3`, the Nodes tab is empty, `scan --json` is useless, and the key-only schema snapshots (plan:162 checks "exact keys, enum members") stay green while the meaning of the values changes. Or the worker keeps names through a data-plane exemption, which opens the hole the invariant forbids. Either path means a third round.
Class-level fix: before any slice, the owner records one decision table with one row per surface and one of three classes:
- operational (aliased);
- owner-result plane (a typed, charset-policed `SchemaName` returned only to the authenticated owner, the STORM rule);
- data plane.

The frozen-contract table then states value semantics, not only keys.

**B2. Persisted and cross-process events have no semantics, but every consumer needs them. Classes 4, 10.**
Plan sections: single model (plan:282, :292 "never reconstructs one from serialized text"); aliases (plan:297, a per-run key from the root `SafeOutputScope`); plan:669 (Platform installs once at server startup).
Evidence:
- Platform `Job.id` is an `Integer` (`api/models.py:329`). The only identifiers the plan allows are UUID or ULID opaque ids (plan:40, :292), so no job id can appear on any ordinary surface.
- Job SSE replays database text in another process (`routes_streaming.py:86-101`), and so does `fidelity_projection.py:3`.
- The isolated child hands its parent a JSON file (`_isolated_worker.py:70`).
- The plan never says what "a run" is for a long-lived server. If it is one allocator per process, all jobs share the 4096 cap per alias kind (plan:297) and become linkable. If it is one allocator per request, the aliases stored with a row differ from the aliases shown when the row is re-rendered.

Failure scenario: F30 needs correlation identifiers (MASTER F30 FRAME), and F32 builds bundles after the run from stored records with stable pseudonyms (MASTER F32). Neither is buildable. F36 says it assumes only what F31 "promises" (F36 plan "F31 assumptions" table), so its Platform slice inherits the gap.
Class-level fix: add one re-ingest factory per family that parses canonical `decoy-safe-output/v1` bytes into a freshly registered event (the plan's own Parse, don't validate source). Define the alias scope as "job", with the alias table persisted keyless per job. Add a typed `ServerOrdinalId` for server-minted integer keys.

### HIGH

**H1. F32 cannot be expressed in schema v1. Class 10.** The `SafeEvent` fields (plan:284-293) have a flat `aliases` tuple. There is no edge or pair type, no version field, and no column-type enum. F32 needs "relationship topology", "types" and "runtime versions + limits" (MASTER F32 section). Version strings are not on the allowed list (plan:33-41). F32 would force a v1 schema change right after release. Fix: add `SafeEdge(alias, alias, closed relation kind)` and a closed `RuntimeVersion` enum built from the lockfile, or say explicitly that F32 must extend v1 additively.

**H2. F30 telemetry is impossible as stated. Classes 1, 4.** Plan:314 says sink drivers only write bytes and never hand a mapping outward. OpenTelemetry attributes and `prometheus_client` `.labels(...)` (`metrics.py:433`) need label mappings or strings handed to a third-party SDK. The Prometheus surface is absent from the matrix and the inventory. Failure: either the worker writes an OTel driver that builds a dict (and a gate reviewer calls it a mapping leak), or F30 has no path. Fix: name a sealed `labels()` projection that only a registered metrics driver may consume, and add `/metrics` to the matrix.

**H3. STORM assumptions are not met. Class 10.** The STORM r7 handoff (line 12) defers `error_result`'s `type(exc).__name__` (`phase.py:478`) to F31. The F31 public API (plan:270) exports no `SafeErrorType` and no type-label function, only `safe_event_from_exception`. `phase-result/v1` and `phase-event/v1` are missing from the frozen-contracts table (plan:222-229), yet plan:320 says their payloads "are not permitted unchanged". STORM Platform is waiting on exactly this envelope (ROADMAP:206). Fix: export `safe_error_type(exc) -> SafeErrorType`, and list the phase schemas as frozen with an additive-only migration.

**H4. The F43 prerequisite is phantom. Class 8.** See section 1. Slice 6's own stop rule (plan:370) halts the slice. `families/ci.py` (plan:265) is built for a consumer that does not exist. Fix: drop F43 from F31. F43 should consume F31 later.

**H5. The Slice 4 gate cannot pass without changing hashes. Classes 8, 10.** Plan:608-611 requires the `rg 'default=str|...'` search to match only `_exception.py`. Today it matches:
- `generation/_reproducibility.py:204` (the canonical config hash);
- `generation/pool/_builder.py:42`;
- `plan/_generation.py:260`;
- `profiling/model_pack/trainer.py:593`;
- a comment at `plan/_compile.py:649`.

None of these are ordinary output. Failure: the worker either changes reproducibility fingerprints or narrows the gate, and narrowing is forbidden. Fix: scope the gate to the sink manifest, not to a grep of the whole repository.

**H6. Two exception-type policies, and `__name__` is not banned. Class 1 (the STORM "dynamic class names" instance).** The plan keeps `postgres_snapshot`'s `__name__` pattern (plan:114). It omits the existing name-string allowlist `_progress_events.safe_error_class` (`:86-98`) from the reuse inventory (plan:206-218). The analyzer list (plan:449-462) has no rule against `__name__` or `__qualname__`. Failure: a seeded secret-named subclass raised inside `postgres_snapshot` reaches `PostgresSnapshotError` text by way of `cause=`. Fix: Slice 2 replaces both policies with `_ALLOWED_EXCEPTION_TYPES`, and the analyzer bans dynamic `__name__` and `__qualname__` reads on exception types.

**H7. The field walk misses root-public raw samples. Classes 1, 6.** See section 1, enumerations: `QualitySummary.sampled_values` and `FkValidityReport.relationship`. The isolated envelope carries `quality_metrics` into the Platform manifest (`_isolated_worker.py:58-69`). Fix: derive the field walk from `decoy_engine.__all__` at test time instead of a list written by hand.

**H8. Slices 6 and 7 are not two-round slices, and Slice 3 crosses repositories. Class 9, ordering.** Slice 6 inherits a STORM CLI that failed its gate five times (ROADMAP:206) and also takes on every command in files of 1405 lines (`scan.py`) and 1717 lines (`run.py`). Slice 7 covers about twelve sink families. Slice 3 is an engine slice that runs before 0.8.0 ships, but it edits Platform `generation_child.py` and `_pg_probe_child.py` (plan:580). `_pg_probe_child.py:83-84` uses stdout as its structured transport, and Platform would have to import the engine-private `_child_fd_bootstrap`, which is not public (plan:270). Fix: put the bootstrap in the public API, move the Platform child edits into Slice 7, and split Slices 6 and 7 into entry boundary, command migration and framework sinks.

### MEDIUM

- **M1. Class 2 and class 8: the permanent install has no test lifecycle.** `install_process_safe_output()` permanently replaces the streams (plan:348). The CLI has 69 test modules that use `CliRunner` in-process. Either those tests skip `main()`, which makes the matrix vacuous, or the first install poisons the pytest process. Fix: a test-only install lease with exact restore, plus a red test that fails if any e2e cell bypasses `main()`.
- **M2. Class 8: no ratchet for the analyzer.** Slice 1 lands an analyzer that rejects passing `exc` to any function other than the boundary (plan:466). The engine has 260 `except ... as` sites, and they migrate in Slices 2 to 5. The plan does not say how CI stays green in between. A shrink-only baseline manifest is required; otherwise a worker will "temporarily" allowlist.
- **M3. Class 3: no transition table.** The catalog requires one. `SafeOutputScope` and process mode have these states: uninstalled, owned, scoped, nested, violated and failed. They are described in prose (plan:341-354), not as a table with forbidden pairs.
- **M4. Class 8: a flaky test.** The mandatory "CPython must observe a reused ID or fail" test (plan:430, :504) depends on the allocator. It can fail under `PYTHONMALLOC=malloc`, debug builds or coverage tracing. Keep the deterministic collision test and make the observed-reuse test advisory.
- **M5. Class 10: version pin.** "Bump to 0.8.0" and "pin exactly 0.8.0" (plan:51, :278) conflict with F36's rule to choose the release identity from the lineage (F36 plan:352). If anything ships first, the pin is stale.

### LOW

- L1. Platform citations are pinned to `051ff2b`; `main` is now `ea68d8e`.
- L2. The "FK-shape output matrix" (plan:157) is never defined.
- L3. The "450 lines including tests" budget (plan:268) is ambiguous for analyzer fixture files.

## 3. Catalog classes: the case each one leaves open

1. Value-bearing output: the structure is real. Open: the naming policy (B1), the `__name__` ban (H6), and the missing surfaces (section 1).
2. Cleanup: the scope owner is good. Open: process install teardown (M1).
3. State: barrier tests are listed. Open: no transition table (M3).
4. Routes: the engine routes are covered. Open: the re-ingest route (B2).
5. Referential integrity: N/A is justified. Open: the output-side FK report fields (H7, L2).
6. Untrusted producer: strong. Open: the Platform child's stdout protocol (H8).
7. TOCTOU: adequate for the unsafe file.
8. Weak tests: Open: the Slice 0 file is missing, the gates scripts are missing, the rg gate (H5), the flaky reuse test (M4), the ratchet (M2).
9. Refactor drift: Open: the node_id join and progress names (B1). Characterization tests for `routes_runtime_facts` and `assembly` are missing.
10. Frozen contracts: Open: `phase-result/v1`, `fidelity-report/v1` value semantics, and the F32 schema (B1, H1, H3).

## 4. Ten ways a gate reviewer breaks it, and whether there is a red-first test

| # | Break | Red-first test in plan? |
|---|---|---|
| 1 | `--help` or a parse error echoes a seeded option value | Yes (plan:640) |
| 2 | An e2e test calls `CliRunner(app)` and skips the entry wrapper | No (M1) |
| 3 | Job SSE replays a marker from `Job.log` | No (not in matrix) |
| 4 | A `/metrics` label carries a value | No (H2) |
| 5 | The fidelity schema snapshot passes while names become `field-N` | No; snapshots check keys only (B1) |
| 6 | A secret-named exception subclass flows through `postgres_snapshot` `cause=` | Partial; runtime only, no static ban (H6) |
| 7 | `QualitySummary.sampled_values` reaches the manifest or evidence | No (H7) |
| 8 | The Slice 4 `rg` gate is red on `_reproducibility.py` | No (H5) |
| 9 | Child and parent alias ordinals disagree, or a re-rendered row relabels | Partial; threads only (B2) |
| 10 | Analyzer CI is red for Slices 1 to 4, or an allowlist creeps in | No (M2) |

## 5. Can a remote worker build it in two remediation rounds?

No, not as written. Slices 0 to 2 can, because they are self-contained and well specified. These things would cause a third round:

- B1 and B2, which are product decisions a worker cannot make;
- inheriting the STORM CLI in Slice 6;
- the phantom F43;
- the analyzer ratchet.

After B1, B2, H4 and H8 are fixed, the engine slices are about two rounds each and the consumer slices are about two rounds each, provided they are split.

## 6. Attacking this review

- B1 counter-case: perhaps the owner wants aliasing everywhere. Then B1 still stands, because plan:229 and plan:749 claim no contract change. The finding is the contradiction, not the choice.
- B2 counter-case: factories accept untrusted source objects (plan:295), so enums and counts can be re-parsed. True. That is why B2 is scoped to aliases, opaque ids and integer ids.
- H4 counter-case: F43 may exist on a local machine. I can see only remotes.
- H5: I did not verify that the reproducibility hash is frozen. Even if it is not, the gate is still unsatisfiable without editing data-plane code.

Not run: no test suite or baseline was run. This is a review of a document, and I made no build claim. I did not re-diff Platform `ea68d8e`. I did not verify the F25 note (plan:164).

## What to cut (the worker does not need these)

- "Rules consulted" (plan:9) and Phase pillars (plan:76-81).
- "Established solutions surveyed" (plan:83-98): move it to module docstrings, which the plan already requires.
- Both Argon closure matrices (plan:480-508): they are review history, and the design and test sections already carry them.
- The Risks table (plan:787-809): it restates the design.
- The 21 "Objective green conditions" (plan:727-751): they duplicate the acceptance tests. Keep one line pointing to the tests.
- The logging census prose (plan:137-145): replace it with the generated AST manifest from Slice 1.
- The F25 note (plan:164), the STORM and F43 reference rows (plan:134-135), the `decoy-web` paragraph (plan:372), the documentation plan (plan:759-785, reduce to three bullets) and the gate chain (plan:811-821, move to the handoff).

The five sections that carry the load are: the Guarantee (plan:21-45), the single model plus serializer (plan:280-320), the exception boundary plus containment (plan:322-360), the acceptance tests (plan:389-478) and the slices (plan:510-711). Together they are about 400 lines.
