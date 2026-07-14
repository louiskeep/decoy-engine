# Track B + Adversarial-Remediation Program

- **Status:** ACTIVE (autonomous `/loop`, self-paced), opened 2026-07-12.
- **Owner run:** Opus tech-lead session; builds delegated to Sonnet, gated by dennis, docs by barry (model tiering per `~/.claude/CLAUDE.md`).
- **Inputs:**
  - `docs/plans/2026-07-12-track-b-completion-program.md` (TB-1..TB-6; TB-1 shipped on `track-b/tb1-out-of-core-streaming`).
  - `docs/adversarial-architecture-review-2026-07-12.md` (DE-01..DE-20).
- **Mandate (from Cam, this session):** continue through all work, plan before implementing, fold the execution-lane DE findings into Track B, run a security track, **hold TB-6 GCP spend and crypto-architecture product forks for Cam.**

## Governing rules (autonomous-operation.md)

Loop structure per sprint: **DEVELOP -> SELF-CHECK -> REVIEW (dennis) -> REMEDIATE -> VERIFY -> GATE.**
High-stakes work is never self-certified; a fresh-context reviewer (dennis) must pass.
STOP-and-escalate conditions that bind this program:

- **Security/secrets/crypto decisions** -> design + escalate, never autonomous merge. Binds DE-01 (FPE primitive choice), DE-02 (key source).
- **Irreversible/spend** -> Cam gate. Binds TB-6 (GCP), any default-behavior flip that breaks existing configs (DE-03 default, TB-5 contract migration).
- **Repeated failure (2-3x)** -> stop, hand back.
- **Scope creep** -> surface, don't absorb.

## Concurrency notice (2026-07-12)

A separate quality-gate/refactor session is live on `track-b/tb1-out-of-core-streaming`:
committed `b50ec11` (dennis MEDIUM fix) + `5e059b8` (barry docs), and has UNCOMMITTED
work extracting TB-1 source-materialization into `src/decoy_engine/execution/_pipeline_sources.py`
(touches `_pipeline.py`, `_planner.py`). **This program does NOT build in the shared
checkout while that is in flight.** Builds run in isolated git worktrees; sprint ordering
starts with work that does not touch the in-flight files. Re-baseline once that refactor
lands on `main`.

## Tracks and sprint sequence

Each sprint is one reversible slice, its own branch/worktree, dennis-gated, then barry docs.
Model tier noted per sprint. "GATE" = Cam decision required before merge/flip.

### Track A - Execution integrity (extends Track B; zero-spend; buildable)

| Sprint | Finding(s) | What | Tier | Collides w/ refactor? |
|---|---|---|---|---|
| A1 | DE-10 | One lossless Arrow-native FK integer typing contract across routes; pandas coercion stops being the oracle for key data. Route choice must not change correctness at >2**53. | Sonnet build from Opus guide | No (`_pandas_adapter.py`, parity tests) |
| A2 | DE-09 | Finish the public out-of-core lazy route: `source_loader` branch passes lazy/batch handles instead of eagerly resolving+retaining every `pa.Table`; telemetry computed from resolved residency. | Sonnet | YES (`_pipeline_route_exec.py`, `_pipeline.py`) - after refactor lands |
| A3 | DE-17 | Auto-chunk streams each batch into a sink; drop whole-output retention + `combine_chunks` unless caller asks for contiguous output. | Sonnet | Partial (`_pipeline_route_exec.py`) - after refactor |
| A4 | DE-15 | One authoritative execution-decision object: immutable initial plan + append-only runtime transitions; explained route == first attempted route. Folds TB-2 governor calibration. | Opus guide + Sonnet | YES - after refactor |
| A5 | DE-03 (CRITICAL) | Compile exact output schema; undeclared column -> typed error, behind a flag (default-OFF now). Route-independent postcondition before any durable write. **Default-flip to error = GATE (TB-5-style migration).** | Opus guide + Sonnet | Partial |
| A6 | TB-2/TB-3 | Governor budget window + reroute-to-completion; local cgroup-capped validation of the NOW-fixed public route (needs A2). Zero GCP spend. | Sonnet | n/a |
| A7 | TB-4 | Recompute k_path multipliers from A6 isolated peaks; pin constants, retire placeholders. | Sonnet | n/a |

### Track S - Security spine (design-first; crypto forks escalate to Cam)

| Sprint | Finding(s) | What | Tier | Disposition |
|---|---|---|---|---|
| S1 | DE-05 | `SourceHandle` bound across profile+execute; source/output completeness postconditions; missing/extra tables -> typed failure. | Opus guide + Sonnet | Buildable (coordinate w/ `_pipeline_sources.py` refactor) |
| S2 | DE-08 | Run-scoped publication protocol: data artifacts first, authenticated success marker last; readers reject unmarked; quarantine no longer writes before commit. | Opus guide + Sonnet | Buildable |
| S3 | DE-04 | Fail-closed model-pack loader: reject unsigned/wrong-signer/altered/path-substituted before deserialize; default pack from package resources. Trust-root pinning is platform-side. | Sonnet | Buildable (engine half); platform boundary = note |
| S4 | DE-02 | **DESIGN DOC:** KeyProvider boundary - engine accepts key bytes + key/version IDs, derives versioned purpose keys, fails-closed on missing secret. **Key SOURCE + rollout = GATE (Cam).** | Opus | Design + escalate; Phase-0 containment (reject seed-only secure mode) buildable |
| S5 | DE-01 | **DESIGN DOC:** FPE replacement/containment - FF1 vs tokenization vs strict-contain; typed domains. **Primitive choice = GATE (Cam).** Phase-0 containment (reject out-of-domain values, ban `preserve_separators=False` on sensitive fields) buildable now. | Opus | Design + escalate; Phase-0 buildable |

### Track C - Release engineering (later; blocks distribution, not correctness)

DE-11 (PoolSpec unification, HIGH), DE-07 (hermetic wheel/sdist), DE-13 (engine-owned version), DE-14 (CI runtime/perf ownership), DE-19 (installed-artifact consumer tests), DE-20 (doc drift -> barry). Sonnet/Haiku.

### Held / gated

- **TB-6** (50M/100M GCP scale proof): HARD-HELD on Cam spend approval + Track A green + security spine at least Phase-0.
- **DE-03 default-flip**, **DE-02 key source**, **DE-01 primitive choice**: Cam decisions.

### Backlog (deferred, no demonstrated default-on HIGH)

DE-06, DE-12, DE-16, DE-18.

## Execution order (collision-aware)

1. **S5-design + S4-design** (Opus, docs-only, zero collision) - unblocks Cam's crypto forks while the refactor lands.
2. **A1 (DE-10)** (worktree, no file overlap) - FK lossless typing.
3. **S2 (DE-08)** + **S3 (DE-04)** (worktrees, no overlap).
4. Re-baseline on `main` once the `_pipeline_sources.py` refactor merges, then **A2 -> A3 -> A4 -> A5**, **S1**.
5. **A6 -> A7** (Track B local validation/calibration).
6. **Track C**, then surface **TB-6** + crypto forks to Cam.

## Slack cadence

Manual ping at: program start, each sprint start, each sprint finish (with the Sonnet build report), each dennis gate verdict, any STOP/gate reached, and program end. Plain text, no emoji/em-dash.

## Done when

- Track A + Track S buildable sprints merged (dennis-green, CI-green), each behind its rollback flag where it changes default behavior.
- Crypto-fork design docs (S4, S5) delivered and escalated to Cam with options.
- TB-6 and the default-flips surfaced to Cam, not executed.
- Every gated item explicitly handed back, not silently skipped.
