# A-5 STORM taxonomy rename: completion scoping plan

Status: plan (continuation of the authoritative `docs/plans/2026-10-09-storm-split-taxonomy.md`, Codex plan-gate GO rev 4).
Split into Track A (engine rename, independent of the release-pinned consumers, remote -> merge-ready, §2) and Track B (consumer cut-over, owner freeze-window, §4). Track A dispatches now; Track B waits on Cam.
Feature: pre-GA internal rename (`storm` -> `detection`/`profiling`/`verification`), enabling clarity for F15/F07.
Tier: R2 (public-symbol breaking pre-GA rename, no shims; cross-repo). Code gate chain: Dennis -> Codex, per repo.
Branches: engine `claude/storm-split-engine` (1 commit, 90 behind), platform `claude/storm-split-platform` (1 commit, 240 behind), + the `decoy` CLI (NOT started).
Author: orchestrator (local). Created: 2026-10-28.

Rules consulted: `~/dev-rules/00-universal.md`, `~/dev-rules/development-loop.md`,
`~/dev-rules/risk-and-exceptions.md`, `~/dev-rules/refactoring.md`,
`~/dev-rules/api-and-compatibility.md`, `~/dev-rules/release-and-rollback.md`,
`~/dev-rules/testing.md`.

## 1. What it is + what's done

A behavior-preserving Fowler Move + Rename: `git mv storm/ -> detection/ + profiling/ +
verification/`, symbol renames (`run_storm`->`profile_columns`, `StormProfile`->`ColumnProfile`,
`run_storm_post_mask`->`run_post_mask`, `StormPostMaskReport`->`PostMaskReport`, etc.), plus
consumer import retargets. The engine branch (146 files vs merge-base) and platform branch
(25 files, pure import migration) are built; two guard sentries lock the rename
(`test_storm_split_no_residual.py`). KEEP surfaces verified preserved: the `run_storm` CONFIG
field, `"storm-post-mask/v1"` schema value, `"storm.scan"` event, `storm_run_group_id`
column, `/storm/...` routes, so wire/persisted/telemetry compat is retained; only Python
import symbols break (and `run_storm`/`StormProfile` ARE in engine `__all__`, a public break).

## 2. Remote-doable build work: the ENGINE rename is INDEPENDENT (Track A, to MERGE-READY)

Both consumers are RELEASE-PINNED, not source-floating: platform CI pins the engine release
commit (`ENGINE_PIN_SHA = fd51be10`, the v0.15.1 commit) and the CLI installs `@v0.15.1`
(`==0.15.1` in pyproject). Landing the rename on engine `main` touches neither pin, so it does
NOT break platform or CLI: they stay on v0.15.1 until a DELIBERATE forward pin bump (Track B).
The engine rename can therefore go all the way to merge-ready on its own gate, decoupled from
the consumer cut-over. Caveat: independence holds for RELEASE-PINNED installs and CI, NOT for a
consumer's EDITABLE dev checkout -- `deploy/docker-compose.yml` bind-mounts the sibling engine
editable, which a dependency pin does not isolate from source changes. So run Track A in an
ISOLATED engine worktree + venv, and leave any consumer-linked editable checkout on its matching
pre-rename ref. Scope the engine handoff to this:

- Rebase `claude/storm-split-engine` onto current `origin/main`. The rename is cheap because
  `storm/` was FROZEN on main (0 of its files edited in the 90 commits): `merge-tree` gives ~3
  textual conflicts (`CHANGELOG.md`, `src/decoy_engine/__init__.py`,
  `tests/sentry/test_module_size.py`) + exactly ONE production residual to hand-retarget:
  `src/decoy_engine/execution/_pipeline_finalize.py:486,496`
  (`from decoy_engine.storm.postmask import run_storm_post_mask` ->
  `verification.run_post_mask`, a NEW consumer main added post-cut). Re-run the two guard
  sentries on the rebased tree.
- Engine `main` stays stamped `0.7.0` (it is a DEV TRUNK; releases are cut on `release/*`
  branches, not from main) -- do NOT bump main's pyproject, it is a no-op for consumers.
- RUN (not merely draft) the §3 proof on the rebased tree: the behavior-byte-parity harness
  (authoritative plan §3e/4.2/4.7), the testflight `golden_fingerprints.json` byte-identity
  run, the model-pack signed-fixture old-artifact load, the wire-identifier assertions, and the
  installed-wheel +/- `[ml]`/`[ner]` smoke. Independent/cross-model review (the branch had
  none) runs at the gate. This produces a merge-ready engine rename PR. Do NOT cut a release
  and do NOT touch any consumer (Track B).

## 3. Acceptance gap (must be produced on the rebased tree before merge)

Have: mechanical test-body diffs, unchanged golden snapshots, the two sentries, KEEP-surface
preservation. MISSING (core acceptance, not yet done): the before/after BYTE-PARITY proof
(profile + redact + post-mask byte-identical under fixed input/seed/clock/deps/model bytes);
the testflight `golden_fingerprints.json` byte-identity run; the model-pack signed-fixture
old-artifact load; wire-identifier assertions; installed-wheel smoke +/- `[ml]`/`[ner]`.
Plus an independent/cross-model review (the branch was built with none). These must be
RE-run on the rebased tree, not the stale branch.

## 4. Track B: the CONSUMER cut-over (owner freeze window; NOT remote-mergeable)

Track A (§2, engine rename to merge-ready) lands independently on its own gate. Track B is the
later, owner-coordinated cut-over that moves the consumers onto the renamed engine. It is NOT a
set of independently-mergeable branches:

- **The version lever (corrected).** Engine `main` is a DEV TRUNK stamped `0.7.0`; releases are
  cut on `release/*` branches and tagged (`v0.15.1` = commit `fd51be10`, which is NOT an
  ancestor of main). Consumers pin the RELEASE, not main. There is no "rc bump in the same
  engine PR" that protects prod. The cut-over is: after Track A's rename merges to engine main,
  cut a NEW engine release (a `release/*` branch + tag carrying the rename, post-0.15.1); that
  tag is the artifact consumers repin to.
- **Consumers are release-pinned, so nothing breaks "instantly".** Platform CI asserts
  `ENGINE_PIN_SHA == fd51be10` + installed `== 0.15.1`; the CLI installs `@v0.15.1` and pins
  `==0.15.1`. The renamed engine on main is invisible to both until a DELIBERATE forward pin
  bump. The real risk is narrower: do NOT bump a consumer's engine pin until that consumer's
  imports are migrated. The CLI's functional callers of the renamed symbols are `src/decoy/api.py`,
  `cli/demo.py`, `cli/init.py:350`, `scan.py:754`, `report.py:2029`, and NER-exception imports in
  `extras.py:193` and `run.py:946` (`_init_inference.py` and some `StormProfile` mentions are
  doc/comment references). The migration must also retake the test monkeypatch strings and the
  NER failure-path coverage from the authoritative plan.
- **Ordering:** (0) VERIFY the CLI CI is pinned to a pre-rename ref (it already is, `@v0.15.1`,
  so this is a check, not an action); (1) cut the new post-rename engine release/tag; (2) rebase
  + migrate platform imports and move ALL its repin surfaces together, then merge; (3) migrate
  the CLI imports + bump its pin/tag off `v0.15.1`, then merge.
- **Full repin surface (per the #105 non-bootable-main lesson: move together or main won't
  boot):** platform `ENGINE_PIN_SHA` + `ENGINE_PIN_VERSION` in BOTH
  `.github/workflows/backend.yml` AND `.github/workflows/fixtures-verify.yml` (packaging tests
  require both workflows to match), `api/engine_compat.py` specifier, `pyproject.toml` pin,
  `deploy/Dockerfile.production` ARG, the capability catalogs, and the `FROZEN`/version-fence
  expectations (`PIN_PREFIX` lives in `tests/test_engine_version_fence_packaging.py`); plus CLI
  `pyproject.toml` `==` and the `@v...` tag in every CI job (ci.yml x3, release-smoke.yml, the
  regen-diagrams SHA).
- **Temporal urgency:** main has ~118 files importing `decoy_engine.storm`, growing weekly;
  every storm-touching merge adds residuals the branch never saw. Serialize Track A AHEAD of
  other storm work; do not let the branch sit.

## 5. Recommendation / hand-back

Split A-5 into two dispatches. TRACK A (§2): the engine rename is independent of the
release-pinned consumers, so a remote worker carries `claude/storm-split-engine` all the way to
MERGE-READY -- rebase, retarget the one `_pipeline_finalize.py` residual, re-run the two
sentries, and RUN the §3 byte-parity/testflight/wheel proof on the rebased tree. It merges on
its own Dennis -> Codex gate without touching platform or CLI. This can dispatch NOW.

TRACK B (§4): the consumer cut-over (cut the new engine release, repin + migrate platform, then
migrate the CLI) is an owner-coordinated FREEZE-window task with merge authority across all
three repos. It is NOT remote-mergeable and waits on Cam. A remote worker may PREPARE it (draft
the platform import migration + the full repin-surface change, draft the CLI import migration)
but must NOT land any of it until Cam runs the sequence.
