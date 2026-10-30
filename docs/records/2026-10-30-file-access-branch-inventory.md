# A-4 / B-5 file-access: branch inventory and integration plan (survey, 2026-10-30)

Status: read-only survey record. No code was changed, no branch other than `review/file-access-inventory` was pushed, no PR was opened. This record carries no gate authority; the orchestrator re-verifies.

Engine `origin/main` at survey time: `a6e5c252` (H-1 Wave A, #348). Platform `origin/main`: `00e55c27` (#123). STORM close-out branch `origin/claude/eager-clarke-14deh1`: `ab29ef0c` (14 ahead of engine main, 2 behind: lacks #347 and #348; it already merged F29 at `a5152bf1`).

Every number below was produced by the commands named in the "Commands" section against full (unshallowed) clones fetched on 2026-10-30.

## 0. Verdict in one paragraph

Engine base: `claude/jolly-carson-3k5stf` (`aa424af4`). Platform base: `claude/vibrant-shannon-5lspen` (`0a0f3e23`). No engine branch is a superset of the other four; three tips (jolly-carson, sharp-goodall, laughing-galileo) each redid the same "merge main and reconcile the new sinks" work independently, touching the same 12 source files with different content and zero shared patch-ids. Slice E1 is built and runs mostly green from the lock on jolly-carson (2 characterization failures on pyarrow error text); the other two tips also fail the D10 strace detector on strace 6.8. Slice P1 is built in breadth but 193 platform I/O sites are still registered as `pending_conversion`, the platform still pins engine 0.15.1 (which has no `fileaccess` package), and no platform PR exists. P2 has no branch of its own; parts of it are inside shannon. P3 is only planned. Nothing here has a gate receipt that this survey could see.

## 1. Branch inventory

### 1.1 Engine (`louiskeep/decoy-engine`)

| Branch | Head | Ahead / behind `origin/main` | Merge base with main | Last commit | Files changed vs base | Beyond `ef419f01` |
|---|---|---|---|---|---|---|
| `feat/file-access-policy` | `32b06cb5` | 44 / 488 | `2ed9eb4c` | 2026-10-08 | (E1 build, pre-merge) | n/a (it is the 44) |
| `feat/file-access-integrated-wip` | `ef419f01` | 45 / 75 | `c5e70982` | 2026-10-25 | 190 | merge commit only |
| `feat/file-access-sink-coverage` | `58a27210` | 46 / 66 | `195fab12` | 2026-10-25 | 191 | `58a27210` merge main (adds `profile/_readers_csv.py`) |
| `claude/jolly-carson-3k5stf` | `aa424af4` | 46 / 5 | `1b3a9229` | 2026-10-29 | 205 | `aa424af4` merge main + reconcile (23 files beyond the auto-merge of its parents: src, docs, CHANGELOG) |
| `claude/sharp-goodall-ikag71` (PR #333, draft) | `9d478486` | 50 / 8 | `a8daf1fb` | 2026-10-29 | 204 | `9190fb6c` merge, `ec1622db`, `215cc7e0`, `ecb6dc4b`, `9d478486` |
| `claude/laughing-galileo-h18woa` | `6ba49418` | 51 / 13 | `17c0bdeb` | 2026-10-26 | 203 | `81b81de6`, `7d7b7843`, `857c0bb8`, `109223a3`, `6ba49418` |

Containment (`git merge-base --is-ancestor`, row contains column):

| contains -> | policy | integrated-wip | sink-coverage | jolly-carson | sharp-goodall | galileo |
|---|---|---|---|---|---|---|
| integrated-wip | yes | yes | no | no | no | no |
| sink-coverage | yes | yes | yes | no | no | no |
| jolly-carson | yes | yes | no | yes | no | no |
| sharp-goodall | yes | yes | no | no | yes | no |
| galileo | yes | yes | yes | no | no | yes |

Plainly: every branch contains `feat/file-access-policy` and `ef419f01`; `laughing-galileo` additionally contains `sink-coverage`; no branch contains jolly-carson or sharp-goodall, and neither of those contains galileo. There is no strict superset of the set.

Duplicates by patch-id (`git cherry <upstream> <head> ef419f01`): none. The branch-unique commits of jolly-carson, sharp-goodall and galileo share no patch-id. They do overlap by file: all three touch `connectors/postgres_snapshot.py`, `execution/_isolated_memwatch.py`, `execution/_pipeline_mixed_route.py`, `execution/out_of_core/_null_int_guard.py`, `io/bounded_reader.py`, `profile/_fixed_width_writer.py`, `profile/_readers.py`, `fileaccess/__init__.py`, `tests/sentry/file_access_registry.toml`, `tests/sentry/file_access_modules.toml`, `tests/sentry/test_physical_seam_disconnection.py`, `tests/fileaccess/route_corpus_golden.json`, with different content (pairwise tip diffs on `src/decoy_engine/fileaccess`, `tests/fileaccess`, `tests/sentry`: jolly vs goodall 17 files, jolly vs galileo 18 files, goodall vs galileo 6 files). This is the same reconciliation done three times.

Commit to plan-slice mapping (the 44 shared commits, from `git log origin/main..origin/feat/file-access-policy`):

- Plan revisions (docs only): `37d80405`, `ce267ec3`, `96879c29`, `ba3111ef`, `bd541df5`, `828184aa`.
- E1-0 characterization: `d830c0a4`. E1-1 package: `b098c5ad`. E1-2 port: `6440e23e`. E1-3 and E1-4: `7177c302`, `98ac4236`. E1-5 record and checklist: `1405151b`.
- Codex and dennis gate remediation of E1-2/E1-3/E1-4 (D5 hygiene, D9 sentries, D10 detector): `d982ec9d` through `cce9eb68` (15 commits).
- Rev 3.3 delta X1 to X4 (secret decode, locators, classification, opaque scratch ids, shipped-name registry): `4c6b5316` through `32b06cb5` (14 commits).

Branch-unique commits: `ef419f01` and every later merge commit are the refold plan §2 step 1 (rebase onto current main). `aa424af4`, `ec1622db`, `6ba49418`, `7d7b7843`, `857c0bb8` are E1-2 plus E1-4 rework (the sinks main added since 2026-10-08: bounded reader, fixed-width writer, Postgres snapshot, memwatch, mixed route, null-int guard) and `ENTRY_POINTS` updates. `215cc7e0` and the golden hunk of `6ba49418` are E1-0 characterization re-baselines. `ecb6dc4b` and jolly-carson's `strace_support.py` hunk are D10 detector fixes. `9d478486` follows the `run_pipeline` frozen-signature table.

Only jolly-carson is past the STORM taxonomy rename (`68e05825`, 2026-10-28): its tree has `src/decoy_engine/profiling/model_pack/_packio.py` and no `storm/` directory; the other four tips still carry `src/decoy_engine/storm/model_pack/_packio.py` and the whole `storm/` tree. `ENTRY_POINTS` on galileo still names `decoy_engine.storm.profiler.run_storm` (`fileaccess/__init__.py` on `6ba49418`), a module that no longer exists on main; jolly-carson names `profiling.profiler.profile_columns`, `_job_estimate.estimate_job`, `bounded_reader.iter_csv_chunks`/`iter_parquet_chunks` and `write_fixed_width` instead. The entry-point sentry imports every name (`tests/sentry/test_file_access_entry_points.py:76-83`), so galileo's list would fail on a merged tree.

`uv.lock` is byte-identical between jolly-carson and main (`git diff --stat origin/main origin/claude/jolly-carson-3k5stf -- uv.lock` is empty).

### 1.2 Platform (`louiskeep/decoy-platform`)

| Branch | Head | Ahead / behind main | Content |
|---|---|---|---|
| `fix/file-access-authz-v2` | `fce57c58` | 4 / 759 | docs only: four plan revisions (rev 1 to 3.1) of `docs/plans/2026-10-02-file-access-authz-engine-platform.md`. The plan is already on main; the branch conflicts with main on that one file. No code. |
| `claude/vibrant-shannon-5lspen` | `0a0f3e23` | 45 / 12 | P1 build: 39 feature/test commits plus 6 "worktree-agent" merges and the integration commit. 217 files: `api/file_access/` (23 modules, largest `_staging.py` 363 LOC, `policy.py` 357 LOC), `api/jobs` (38 files), `tests/policy/` (platform sentry, 31 test functions), 323 test functions in file-access-named test files. Still pins `decoy-engine==0.15.1` (`pyproject.toml:36`, `backend.yml:72-73` `ENGINE_PIN_SHA fd51be10`); main pins 0.16.0. |
| `claude/laughing-galileo-h18woa` | `4950b0b7` | 2 / 35 | PoC `PrincipalFileAccess` for upload reads only (`api/file_access/policy.py:23-25` and `:119-120` deny directory and output roles), pins 0.14.2. Its 9 files all differ from shannon's versions; shannon does not contain it. |
| `docs/file-access-authorization-redesign` | `1140ebdc` | 6 / 879 | rev 6 plan, superseded (plan CD-2). Docs only. |
| `plan/a4b5-file-access-refold` | `37cec36e` | 2 / 12 | `docs/plans/2026-10-29-a4-b5-file-access-refold.md` (gated GO per its header) plus the B-6 plan. No conflicts with main. Docs only. |

Platform containment: shannon does not contain galileo; main contains none of them. `git cherry origin/main origin/fix/file-access-authz-v2` shows all 4 commits as not-on-main by patch-id (the plan reached main through another path).

Open PRs: engine #333 (draft, head sharp-goodall, "do not merge alone"). No platform PR references any of these branches (`list_pull_requests` on 2026-10-30: #112, #102, #94 only).

## 2. Conflicts (`git merge-tree --write-tree --name-only`)

Against `origin/main` (`a6e5c252`). Every engine branch conflicts on the same 22 files: 20 source files, all of which F29 (`fdd08a89`, 324 files) touched on main, plus two sentry tests that `a6e5c252` also touched:

```
src/decoy_engine/execution/_chunked.py            _chunked_code_set.py   _chunked_output_sink.py
src/decoy_engine/execution/_governor.py           _isolated_run.py       _transactional_sink.py
src/decoy_engine/execution/_strategies/_code_set.py                       _strategies/_joint_mask.py
src/decoy_engine/execution/capacity.py            generation/statistical/_spec.py
src/decoy_engine/keyprovider.py  plan/_generation.py  quality/dp_provenance.py
src/decoy_engine/subset/_api.py  subset/_materialize.py  subset/_preflight.py
src/decoy_engine/transforms/_codeset_loader.py  transforms/joint_mask.py  unmask.py  vault.py
tests/sentry/test_module_size.py  tests/sentry/test_physical_seam_disconnection.py
```

Per-branch additions:

| Branch | vs main | vs STORM branch |
|---|---|---|
| integrated-wip | 30: core + `docs/decisions/module-size-census-history.md`, `execution/out_of_core/_runner.py`, `profile/_readers.py`, and the rename pair `profiling/model_pack/{_packio,loader,provenance,trainer}.py`, `profiling/profiler.py` | 31: + `execution/_pipeline_routing_signals.py` |
| sink-coverage | 29: as above minus `profile/_readers.py` | 30: + `_pipeline_routing_signals.py` |
| jolly-carson | 28: core + `CHANGELOG.md`, `connectors/postgres_snapshot.py`, `generation/_pinned_compile.py`, `_pinned_execute.py`, `generation/window.py`, `io/bounded_reader.py` | 30: + `_pipeline_routing_signals.py`, `execution/_job_estimate.py` |
| sharp-goodall | 31: core + `postgres_snapshot.py`, `_pinned_execute.py`, `window.py`, `bounded_reader.py`, the 5 `profiling/` rename files | 32: + `_pipeline_routing_signals.py` |
| galileo | 30: core + `postgres_snapshot.py`, `_pinned_execute.py`, `window.py`, the 5 `profiling/` rename files | 31: + `_pipeline_routing_signals.py` |

The `_packio.py` note: on the four pre-rename tips the file is `src/decoy_engine/storm/model_pack/_packio.py` (added by E1); main deleted `storm/` and created `profiling/` in `68e05825`. merge-tree follows the directory rename and reports the conflict at `profiling/model_pack/_packio.py` as a content conflict (both sides add; main's side is the rename target). jolly-carson resolved this already (file present at the `profiling/` path, `storm/` absent). All conflicts are `CONFLICT (content)`; no modify/delete.

Platform shannon vs main: 3 content conflicts, `api/jobs/disk_quota_matrix.py`, `api/jobs/job_run_context.py`, `api/jobs/v2_orchestrator.py`, all from B-6 Stage-2 `87bea9a1` (tokenization vault and durability, repin to 0.16.0) on main. Platform galileo and the refold plan branch merge clean; `fix/file-access-authz-v2` conflicts only on the plan file.

Files touched by both jolly-carson and the STORM branch (semantic overlap even where text merges): `detection/name_hints/loader.py`, `execution/_chunked_output_sink.py`, `execution/_job_estimate.py`, `execution/_pipeline_routing_signals.py`, `execution/capacity.py`, `profiling/model_pack/loader.py`, `profiling/model_pack/trainer.py`, `profiling/profiler.py`, `validation/_config.py`, and the three sentry tests. The STORM branch adds `src/decoy_engine/phase.py`, `src/decoy_engine/reasons/`, `config/_post_scan.py` and renames to `post_scan` in 23 source files; main has zero `post_scan` occurrences today.

## 3. Slice status

### E1 (engine `fileaccess`): built, reconciliation unfinished

Built on every tip: `src/decoy_engine/fileaccess/` (11 modules + 5 adapters), `tests/fileaccess/` (23 test files, 213 test functions on integrated-wip, sharp-goodall and galileo; 214 on jolly-carson), three sentries `tests/sentry/test_file_access_boundary.py`, `test_file_access_entry_points.py`, `test_locator_hygiene.py` (26 test functions), registry `tests/sentry/file_access_registry.toml` (164 entries on integrated-wip, 189 on jolly-carson and galileo, 193 on sharp-goodall; the sentry asserts the exact count), `docs/security/file-access-policy.md`, `docs/records/2026-10-07-file-access-e1-build.md`.

Test runs done for this survey (clean detached worktrees, `uv sync --frozen --extra dev` from each tip's own `uv.lock`, Python 3.11.17, pyarrow 24.0.0, strace 6.8, running as root in a container; `uv run --frozen --no-sync pytest tests/fileaccess tests/sentry/test_file_access_boundary.py tests/sentry/test_file_access_entry_points.py tests/sentry/test_locator_hygiene.py -q`):

| Tip | Result | Failures |
|---|---|---|
| jolly-carson `aa424af4` | 669 passed, 2 failed, 2 skipped | `test_char_error_text.py::test_r1_lazy_source_to_table_missing_and_malformed`, `::test_r4_reference_table_errors` |
| sharp-goodall `9d478486` | 659 passed, 11 failed, 2 skipped | the same 2, plus 8 of 11 in `test_strace_selftest.py` and `test_policy_strace.py::test_the_detector_sees_a_direct_open_and_read_of_the_target` (`_opened(...)` is False) |
| laughing-galileo `6ba49418` | 652 passed, 18 failed, 2 skipped | the same 2, plus all of `test_strace_selftest.py` and 5 of `test_policy_strace.py` (`StopIteration` in the window parser) |

The two shared failures pin pyarrow's error text (`Could not open Parquet input source`), and pyarrow 24.0.0 from the lock emits `Error creating dataset. Could not read schema ...` first. The test file is identical on all four tips and the lock pins 24.0.0 on all of them, so the branch record's "3204 passed, 2 skipped" (`docs/records/2026-10-07-file-access-e1-build.md:451` on jolly-carson) was not produced against this lock. The strace failures are not flaky: re-running `test_strace_selftest.py` alone gives 8 failed on sharp-goodall and 11 passed on jolly-carson. The three tips carry three different window-parser markers in `tests/fileaccess/strace_support.py` (galileo: literal `stat("/NAME"`; sharp-goodall: unanchored `stat\w*\((?:AT_FDCWD...)?"/NAME"`; jolly-carson: anchored `^\d+\s+\w*stat\w*\(.*"/NAME"`). Only jolly-carson's matches strace 6.8 here. Skips: `test_char_vault_in_place.py:35` (root ignores directory permissions) and `test_x2_locators.py:366` (`lightgbm` absent).

Not run: the full engine suite, the native-companion cells, py3.10 CI mirror, mutation and coverage, the `companion` golden section, ruff and mypy.

What E1 still lacks to be shippable (refold plan §2): a merge of current main including F29 and the STORM close-out; an E1-4 inventory rerun on that tree (the registry count will move); `tests/fileaccess` wired into main's CI (main's `.github/workflows/ci.yml` has zero `fileaccess` references; the branch's `ci.yml:116-127` installs strace and runs the suite); golden provenance recorded against unmodified main with regeneration disabled (`FILEACCESS_WRITE_GOLDENS=1` at `test_char_route_corpus.py:26` writes and returns without comparing); the §9 compatibility-checklist evidence (every box in PR #333 is unchecked); a CHANGELOG entry that matches the release that will carry E1 (not 0.9.0).

### P2 (platform-only surfaces): no branch; partly inside shannon

No branch named for P2 exists. By commit subject, shannon carries P2-7's removal (`c97ec939` "data-table SQL runs on the engine's locked DuckDB adapter; drop interim gate"), upload by-id routes (`352d685c`) and STORM pending-grant work (`66096d37`). I did not verify P2-1 to P2-6 or P2-8 to P2-9 row by row; treat P2 as "partly built, unverified".

### P1 (platform policy): built in breadth, not in depth

Present on shannon: P1-1 `api/file_access/` with `PrincipalFileAccess`, broker, child, grants, staging, roots, self-test, denial (23 modules, all under 400 LOC); P1-2 53 `file_access=` keyword sites in `api/jobs` (main has 0); P1-3 (`36390f65`, `9b17f4a8`); P1-5 startup wiring and `EXPECTED_FILE_ACCESS_CONTRACT = 1` (`api/engine_compat.py:176`); P1-6 `denial.py`; P1-7 done (`restrict_external` absent on shannon, present on main `api/jobs/v2_config.py:58`); P1-8 `tests/policy/` with `platform_file_io_registry.toml` (478 entries, 527 sites: 285 `registry`, 193 `pending_conversion`); P1-9 harvest (`85a65bbd`).

Gaps: the 193 `pending_conversion` sites across 36 modules (largest: `api/retrieval/reader.py` 20, `api/generation/generation_parent.py` 16, `api/jobs/v2_cloud_materialize.py` 15, `api/jobs/preview_parent.py` 14, `api/retrieval/decode_child.py` 14) are by the registry's own header "deleted by the conversion that moves the site onto a capability", so P1-4 is incomplete. Vault persistence keeps a path-based branch when `file_access is None` (`api/jobs/v2_vault.py:102-106`). The stop-gap walker is still imported (`api/jobs/routes_preview.py:123`, `api/pipelines/router.py:1342`, `api/workflows/runner.py:33`), which the refold plan allows until every route has capability enforcement. The engine pin is 0.15.1, which has no `fileaccess` package, so the branch's own tests cannot run against the pinned artifact; they need an editable E1 checkout. No P3 (`api/jobs/output_paths.py` absent). Platform tests were not run for this survey: doing so needs an editable E1 install, which is a build.

### P3 (per-owner output roots): planned only

No branch, no `output_paths.py`, no `u_<owner_id>` layout on any surveyed branch.

## 4. Security read (survey, not a gate)

Engine (candidate base jolly-carson). The jail is `RootedFileAccess` over `openat2` with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS` (`fileaccess/_openat2.py:16-19`), a per-component `O_NOFOLLOW` walk when `openat2` is unavailable (`_openat2.py:93,97`), and an opt-in racy portable fallback (`_rooted.py:264-268`). The engine itself has no admin notion: "ADMIN-only external paths" is the platform's operator-root policy (`api/file_access/_roots.py`, plan CD-1). The ambient CLI policy `LocalFileAccess` is constructed only in `_entry.py` and is refused after `forbid_ambient()` (`_entry.py:35-39`, `:107-115`).

User-supplied or config-supplied paths that go through the policy (each is `fileaccess.current().open_read(FileRequest(...))` or a bound policy): `data_discovery.py:189-196`, `io/bounded_reader.py:270-271` and `:313`, `profile/_policy_bound.py:21-22`, `profile/_readers.py:235-236`, `reference_tables/_loader.py:101`, `transforms/_codeset_loader.py:132`, `validation/_config.py:47-48`, `vault.py:325` (write via `output_dir`) and `vault.py:434-435` (read), `execution/out_of_core/_scratch.py:53`.

Paths that do not go through the jail: the 32 `ambient_only` registry entries in four modules, each guarded so a strict policy refuses before any I/O: `connectors/postgres_snapshot.py:439` (`require_ambient`, then `Path.mkdir`, `os.chmod`, `os.replace`, `shutil.rmtree` on the caller's `dest_dir`), `execution/_isolated_run.py:265` (then `tempfile.mkdtemp`, `Path.mkdir`, `pyarrow.parquet.write_table`, `subprocess.Popen`, `shutil.rmtree`), `execution/_isolated_commit.py` (6 entries, path handed over by the parent), `execution/out_of_core/_scratch.py:23` (caller-named `temp_dir`). The remaining 157 registry entries are classified as engine-owned or constant paths (`/proc`, `/sys/fs/cgroup`, `Path(__file__).parent`, `tempfile.gettempdir()`, scratch roots the runner created, `DECOY_SFTP_KNOWN_HOSTS`, the DuckDB `untrusted_session` connection), 40 of them inside `fileaccess/_local.py` itself.

Not checked on the engine side: that each of the 157 non-ambient registry rules is true at its site (I read the rules, not the sites); the native companion routes; the DuckDB adapter's external-access lock; fd handoff into the isolated subprocess; any sink in main's post-`1b3a9229` modules (a text grep for open-like calls in the six new Wave A modules found none, but the sentry was not run on a merged tree); `profile/_cloud_readers.py` (8 entries, cloud is FA-2 scope).

Platform (candidate base shannon). The policy-issued paths are the `registry` entries plus the 53 `file_access=` call sites. User-reachable paths that still bypass `PrincipalFileAccess` (all are `pending_conversion` rows): `api/jobs/v2_config.py:351` `Path(raw).exists()` on a config source path whenever `file_access is None`; `api/jobs/routes_outputs.py:292-294` and `api/files/router.py:685`, `api/retrieval/reader.py:358-366` use their own `realpath` plus relative-path jail over `settings.output_dir`, not the policy; `api/files/router.py:474-485` opens, stats and unlinks uploads `dir_fd`-relative to the process upload root without a principal check at that layer; `api/data_tables/storage.py:306-322` materializes, unlinks and stats data-table files by path; `api/jobs/v2_vault.py:103-106` path-based vault write. The three `source_authz` imports above remain the authorization for preview, pipeline create and workflow runs.

Not checked on the platform side: whether each pending site receives a user-controlled string or a server-minted locator; the web client callers; the per-process self-test behaviour on this kernel; S1B group reads; anything in `api/generation/` and `api/retrieval/` beyond the registry rows.

Defect-catalog classes visible in the survey (named, not fixed): class 8 (weak tests and overclaimed evidence): branch records claim green runs that do not reproduce from the lock; the route-corpus golden on galileo was regenerated on the candidate tree (`6ba49418` body) whereas sharp-goodall's `215cc7e0` took it from unmodified main; class 9 (refactor drift): three independent reconciliations of the same files, and galileo's `ENTRY_POINTS` names a module main no longer has; class 10 (frozen contract drift): `run_pipeline` gains `file_access` in the frozen-signature table on three tips, engine `CONTRACT_VERSION = 1` and platform `EXPECTED_FILE_ACCESS_CONTRACT = 1` are a pair this survey did not find a cross-repo test for (it did not search exhaustively). Class 1 (value-bearing output) is the area E1's D5 work targets; I did not re-verify it.

## 5. Recommendation

### 5.1 Engine: base `claude/jolly-carson-3k5stf` (`aa424af4`)

Why this one: it contains integrated-wip; it is the only tip past the STORM rename (so no `storm/` versus `profiling/` conflict and `_packio.py` is already at its final path); it is 5 commits behind main (fewest); its `ENTRY_POINTS` names only modules that exist on main (the entry-point sentry was not run on a merged tree); its D10 parser is the only one that passes on strace 6.8 here; its `uv.lock` equals main's. What it lacks from the others: galileo's `fileaccess/adapters/arrow.py` `read_schema` (4 lines; shannon does not call it, it uses `read_table`, `parquet_file`, `read_metadata`), galileo's `tests/unit/io/test_bounded_reader.py` and `tests/unit/test_fixed_width_writer.py` edits, sharp-goodall's golden re-baseline from unmodified main (`215cc7e0`) and its frozen-signature test follow (`9d478486`).

Ordered steps (merge main into the branch; never rebase a pushed branch):

1. Wait for the STORM close-out merge to land on `origin/main` (`claude/eager-clarke-14deh1` into main, with #347 and #348 reconciled by its owner). Do not merge E1 before it: `execution/_pipeline_routing_signals.py`, `_job_estimate.py`, `_chunked_output_sink.py`, `capacity.py` and the `profiling/` files are touched by both and the F29 merge into STORM is already done there.
2. `git checkout claude/jolly-carson-3k5stf && git merge origin/main`. Expect the 28 files in §2 plus `_pipeline_routing_signals.py` and `_job_estimate.py`. Resolution rule per class: in the source files, keep F29's typed error and progress plumbing and keep E1's `fileaccess.current().open_read(...)` or policy-bound handle at the same site (F29 wraps errors; E1 changes what is opened; they compose); `CHANGELOG.md` union under Unreleased; `tests/sentry/test_module_size.py` census union with LOC re-measured on the merged tree, never a raised cap; `tests/sentry/test_physical_seam_disconnection.py` allowlist union, no entry deleted; `_pipeline_routing_signals.py` keep the STORM `post_scan` names and E1's policy argument; `_job_estimate.py` keep STORM's envelope and E1's `@entry_point` plus `file_access=None`.
3. Cherry-pick `215cc7e0` (golden from unmodified main) and `9d478486` from sharp-goodall (both touch files jolly-carson's merge also edited, so expect to re-apply by hand), then re-record the golden against the merged unmodified main with `FILEACCESS_WRITE_GOLDENS` unset in the gate run. Cherry-pick the `read_schema` hunk and the two unit-test files from galileo only if a reviewer wants them; otherwise drop.
4. Rerun the E1-4 inventory (`tests/sentry/test_file_access_boundary.py`) on the merged tree; add registry entries one by one with rule and owner; no module-wide exemptions.
5. Resolve the two `test_char_error_text.py` failures by re-recording the expected text from unmodified main under the lock (characterization), not by loosening the assertion.
6. Confirm `ci.yml` carries the strace install and `tests/fileaccess` lane after the merge (the branch's hunk does not conflict).
7. Push jolly-carson; close #333 with a pointer; open the engine PR from jolly-carson.

Redundant after step 7 (delete): `feat/file-access-policy`, `feat/file-access-integrated-wip`, `feat/file-access-sink-coverage`, `claude/sharp-goodall-ikag71`, `claude/laughing-galileo-h18woa`.

### 5.2 Platform: base `claude/vibrant-shannon-5lspen` (`0a0f3e23`)

1. `git merge origin/main` into shannon. Three conflicts from B-6 `87bea9a1`: `api/jobs/disk_quota_matrix.py` (keep B-6's new matrix rows and E1's quota cites for the policy-opened sites), `api/jobs/job_run_context.py` (keep B-6's tokenization vault fields and E1's `file_access` on the context), `api/jobs/v2_orchestrator.py` (keep B-6's durability fix and E1's `file_access=` threading into every engine call). Main's `pyproject.toml` already says 0.16.0; shannon's 0.15.1 does not conflict textually but must not survive.
2. Repin to the E1-bearing engine release as one change across `pyproject.toml:36`, `api/engine_compat.py:171` and `:176`, `.github/workflows/backend.yml:72-73`, `fixtures-verify.yml`, `deploy/Dockerfile.production`, and the packaging and ownership tests (refold plan §4.5). Until that release exists, build and test against an editable E1 checkout and say so in the record.
3. Convert or explicitly defer each of the 193 `pending_conversion` sites; each conversion deletes its row. The vault `file_access is None` branch and the three `source_authz` imports go when their routes are covered.
4. Open the platform PR only after step 1 and with the engine SHA it was tested against in the body.

Redundant after merge (delete): `fix/file-access-authz-v2` (plan-only, plan is on main), `claude/laughing-galileo-h18woa` (PoC superseded), `docs/file-access-authorization-redesign` (rev 6, superseded by CD-2). `plan/a4b5-file-access-refold` should be merged to main as docs and then deleted.

### 5.3 Cross-repo gate

- Engine PR must prove: E1-4 inventory on the merged tree with the exact registry count; `tests/fileaccess` and the three sentries green from the lock on py3.10 and py3.11 with real strace, listing the D10 cells that ran; golden provenance from unmodified main; the §9 checklist boxes each linked to evidence (public-API diff, AT-E9, AT-E10, AT-E19, vault corpus); `run_pipeline` frozen-signature table change acknowledged as the one additive signature change; no sentry threshold widened.
- Platform PR must prove: the editable-E1 SHA it ran against; every `file_access=` engine call covered by `tests/policy/test_engine_file_access_calls.py`; the `pending_conversion` count after conversion, with each remaining row deferred by name; `forbid_ambient` and the contract check in every engine-importing process (AT-P1 inventory); both scheduler flag states; the repin consistency check extended in `tests/test_engine_version_fence_packaging.py`.
- Combined gate must prove: a live denial per V-row and per Appendix A2 row through platform into engine under a strict policy, with a positive control each; D10 zero-access evidence for a streamed run; the golden-launder check; recovery to the prior engine and platform pair without enabling ambient access.
- Roster: Dennis first (R3, frozen contracts: `run_pipeline` signature table, `CONTRACT_VERSION` versus `EXPECTED_FILE_ACCESS_CONTRACT`, phase envelope wire shapes), then Codex FINAL on the paired artifacts (refold plan §4.3). No engine-only approval authorizes the engine merge.

## 6. Top three risks

1. Three divergent reconciliations with no shared patch-ids: whichever base is picked, the other two tips' fixes are lost unless harvested deliberately, and the records on each tip overclaim green runs that do not reproduce from the lock (class 8).
2. The F29 merge: 285 source files changed on main after every tip's base; the 22 shared conflicts are all F29 versus E1, and the STORM close-out adds a second wave in `execution/`. A hand merge of that size without characterization on the merged tree is class 9 drift waiting to happen.
3. Platform P1 is wide but shallow: 193 unconverted sites, a 0.15.1 pin that cannot import `fileaccess`, and no PR. A combined gate run against a platform that still stats config paths outside the policy (`v2_config.py:351`) would not prove the open-time guarantee.

## 7. Open questions for the owner

1. Is the E1-bearing engine release 0.17.0, and does B-6's 0.16.0 repin on platform main stay the floor until then?
2. Should sharp-goodall's PR #333 be closed in favour of a fresh PR from jolly-carson, or retargeted (its description is the better one)?
3. May the two `test_char_error_text.py` expectations be re-recorded from unmodified main under the lock, or is the pyarrow 24 message itself a finding against D5 (it names the file)?
4. Which of the 193 `pending_conversion` platform sites are accepted as deferred for the first combined gate?
5. Does the combined gate run before or after the STORM close-out lands on main? This survey assumes after.

## 8. What was not run or verified

- Not run: full engine test suite, ruff, mypy, mutation, coverage, native-companion cells, py3.10 mirror, any platform test, any merge (all conflict data is from `merge-tree`, no working-tree merge was attempted).
- Not verified: the gate ledger (`gate-ledger.tsv` lives outside both repos per `DEFECT-CATALOG.md:62`; nothing in either repo records a code gate on these branches); the truth of each registry rule at its site; reachability of each platform `pending_conversion` site by user input; P2 rows other than by commit subject; GitHub review state beyond the open-PR listing.
- Environment caveats: tests ran as root (one skip), without `lightgbm` (one skip), on strace 6.8 and Python 3.11.17 from `uv.lock`, inside a container; the branch records cite py3.10 and py3.11 lanes with strace 6.8 too.

## 9. Commands

```
git fetch --unshallow origin; git fetch origin 'refs/heads/*:refs/remotes/origin/*'
git rev-list --left-right --count origin/main...origin/<branch>
git merge-base --is-ancestor origin/<a> origin/<b>
git cherry origin/<upstream> origin/<head> ef419f01
git diff --name-status origin/main...origin/<branch>
git merge-tree --write-tree --name-only origin/main origin/<branch>
git merge-tree --write-tree --name-only origin/claude/eager-clarke-14deh1 origin/<branch>
git log --format=%h 1b3a9229..origin/main -- <conflict file>
git worktree add --detach <dir> origin/<tip>; uv sync --frozen --extra dev
uv run --frozen --no-sync pytest tests/fileaccess tests/sentry/test_file_access_boundary.py \
  tests/sentry/test_file_access_entry_points.py tests/sentry/test_locator_hygiene.py -q
```

Worktrees created for the test runs were removed after the runs.
