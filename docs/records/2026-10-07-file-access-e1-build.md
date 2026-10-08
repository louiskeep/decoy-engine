Status: record

# File access E1 (engine `FileAccess` policy): build record

Plan: [2026-10-02-file-access-authz-engine-platform.md](../plans/2026-10-02-file-access-authz-engine-platform.md),
rev 3.2 and its 2026-10-07 delta. Contract page for integrators:
[file-access-policy.md](../security/file-access-policy.md). Base: engine `main` `2ed9eb4c`
(plan commits `37d80405`, `ce267ec3`). Branch `feat/file-access-policy`. Nothing was pushed, tagged
or published; the version is still 0.7.0 and the 0.9.0 release needs Cam's go.

## What was built

- **E1-0** `tests/fileaccess/test_char_*.py`, `route_corpus.py` and its golden: error text of every
  Appendix A read row, the table sink's publish, failure and zero-record semantics (absent, empty and
  non-empty destination), accepted and rejected output names, the default log destination, quarantine
  and vault writers, and a corpus of 11 current-main routes (full frame, auto-chunk, streamed to a
  sink, lazy source, hold-back spill, multi-table dispatch, transform-bearing lazy source, native
  `when`, sequential FK, out-of-core FK, generate-only) with route evidence and output digests, with
  and without the native companion. All green on the untouched base on Python 3.11 and 3.10.
- **E1-1** `src/decoy_engine/fileaccess/`: protocols and roles, `LocalFileAccess`,
  `RootedFileAccess` (`openat2` with a descriptor-walk fallback), `PreopenedFileAccess`, output
  transactions, `entry_point`, `forbid_ambient`, `bind_iterator`, `run_in_context`, denial hygiene, and
  the adapters (`arrow`, `pandas`, `polars`, `duckdb`, `serial`).
- **E1-2 / E1-3** every Appendix A row is routed through the policy: sources and lazy readers, fixed
  width, subset (inputs, preflight, staged output), snapshots (three sites), code-set corpus,
  reference tables with the shipped-table registry, mask secret, vault read and write, config file,
  quarantine writers, `ParquetTransactionalSink` and the hold-back spill on output transactions,
  discovery SQL on a locked DuckDB connection, custom providers, model packs and name hints through
  directory capabilities, the log file, the isolated route (fail closed under a strict policy), and
  the thread-context helpers in the governor monitor and the FPE pool.
- **E1-4** sentries: `tests/sentry/test_file_access_boundary.py` (sink scan with a registry of 163
  reviewed call sites), `test_file_access_entry_points.py`, 33 negative and 2 positive fixtures,
  and the runtime detector (`tests/fileaccess/strace_support.py`, `test_policy_strace.py`).
- **E1-5** CHANGELOG, `docs/security/file-access-policy.md`, CODEMAP and docs index, this record.

## Judgment calls

1. **No `beniget`.** The sentries use the stdlib `ast` with their own alias, re-export, partial,
   `getattr`, `importlib`, `__import__`, `Path`-variable and sink-as-value resolution. This keeps the
   dependency set and the certified DP profile's pinned fingerprint unchanged.
2. **`current()` falls back to the ambient policy unless `forbid_ambient()` was called.** The plan has
   internal code raise when nothing is installed. Existing code and the CLI call many internals
   outside any entry point, and every one would have needed to become an entry point. The fail-closed
   property the guarantee needs holds where it matters: after `forbid_ambient()`, nothing installed
   raises, and every `ENTRY_POINTS` member refuses with no policy (tested in a child process).
3. **`ENTRY_POINTS` is wider than the plan's list**: it also holds every function the platform or CLI
   imports that can reach the policy (`strategy_config_fingerprint`, `apply_quarantine`,
   `plan_from_yaml`, `run_subset_preflight`, `load_corpus`, ...). Directory-valued loaders that already
   take a caller path (`ModelPackLoader`, `load_name_hint_terms`, `load_disguises`, `classify_fields`)
   take `file_access=` and `capture()` it instead of installing it for a call.
4. **Isolated route (D6 fallback taken).** A child interpreter inherits no policy and its config-named
   reads cannot be listed in advance without the field walker the plan rejects, so under a strict
   policy `run_pipeline_isolated` raises `FileAccessUnavailable` before any I/O when it would spawn or
   names a `staging_dir` or `output_dir`; `run_fk_out_of_core(temp_dir=...)` likewise. The ambient
   path keeps its existing code, whose caller-derived sites are registry entries marked
   `ambient_only`, valid only because the guard (`require_ambient`) sits in front. This extends the
   plan's "caller paths are never registrable" rule beyond `_local.py`; the sentry asserts the guard
   exists. **Finding for the plan:** D6 and delta item 4 cannot "carry capabilities through both
   stages" without a broker for the child, which is platform-side scope.
5. **Module lists.** The plan's capability list is kept exact (`_rooted`, `_output`, `_preopened`,
   `_openat2`): the handles live in `_rooted.py`. The log handler lives in `_local.py` instead of an
   `adapters/logging.py`, and there is no `adapters/archive.py` (nothing in the engine zips or tars).
   `joblib` and in-memory `yaml` parsing are registry entries over `BytesIO`.
6. **Quarantine stage is opaque**: `write_jsonl_staged` returns the stage, not a `Path`. One E1-0
   characterization test was changed from reading the returned path to observing the stage on disk.
   A `quarantine_direct` stage label keeps the un-staged writer's replace semantics and writes through
   `/dev/null` and symlinks.
7. **Stage names come from a closed label set** (`table_sink`, `quarantine`, `isolated`, `hold`,
   `subset`, `vault`, `pack`), which fixes the prefix, so a caller never supplies a stage name and the
   existing `_decoy_*` prefixes tests rely on are kept.
8. **Behavior preserved on purpose** where existing tests pin internals: `Path.stat` and
   `Path.resolve` for the corpus cache key, `os.replace(src, dst)` with two arguments for the
   directory publish, a module-level `open` seam in the fixed-width reader, `pq.read_table` and
   `pq.ParquetFile` by attribute, the `_decoy_hold_` spill prefix.
9. **Lexical instead of file-system** for three comparisons that had no business touching the disk:
   the capacity estimate's absolute path, the quarantine alias guard, the default model-pack check.
10. **Field keys** are the config position where one is available; helpers that receive only a path
    (snapshot digest, code-set corpus) use a generic key (`snapshot_file`, `provider_config.corpus_source`).
11. **Dead code removed**: `get_file_size`, `is_path_exists`, `create_directory_for_file`
    (`internal/fs.py`), unused anywhere.
12. **Portable fallback** confines with a descriptor walk (`O_NOFOLLOW` per component), which is
    stronger than the plan's `realpath` + `commonpath`.
13. **strace** is not installed on the devbox. The binary was extracted from the Debian package into
    the scratch directory (no system change) and passed through `STRACE`. CI installs it (`ci.yml`).
14. **Census and ratchets**: dense-module bumps are in `test_module_size.py` and
    `docs/decisions/module-size-census-history.md`; `_pipeline.py` stays at exactly 684 lines (three
    earlier bookkeeping tests pin it); three over-max legacy files shrank back under their recorded
    sizes (`_compile.py`, `trainer.py`, `_runner.py`, the last by moving scratch-root creation into
    `out_of_core/_scratch.py`).
15. The plan's `RecordingFileAccess` lives in `tests/fileaccess/support.py`, not the package.
16. **Config and CI changes outside the package.** `.github/workflows/ci.yml` installs `strace`
    (the D10 runtime detector fails rather than skips without it) and adds `tests/fileaccess` to the
    routing guard so the new directory cannot fall out of CI. `pyproject.toml` excludes
    `tests/sentry/fa_fixtures` from ruff: those files are deliberately bad code (negative sentry
    fixtures with unused imports and raw sinks) that the sentries must scan, so they cannot be linted
    clean or formatted.
17. **Public API diff** (signature comparison of 1239 base symbols against head, `apisurf.py`): every
    change adds a keyword-only `file_access=` or an optional trailing parameter, except two that are
    not additive and are noted for the compatibility checklist: `quarantine.write_jsonl_staged`
    now returns an opaque `OutputTxn` and `publish_staged_jsonl` / `discard_staged_jsonl` take it
    (they took and returned a `Path`; nothing in this repo or the platform source outside the
    quarantine module calls them), and six private-module attributes that were stdlib re-exports
    (`pq`, `shutil`, `tempfile`, `logging` imported into `_transactional_sink`, `quarantine`,
    `_codeset_loader`) are gone. `read_fixed_width` and `run_discovery_sql` keep their old calls and
    gain optional arguments.

## Red before

Probe `redbefore.py` run on the untouched base (`ce267ec3`), no policy:

```
V15 bare traversal read           READ ['id', 'col_secret']
V7  customer: reference           READ ['id', 'col_secret']
V6  corpus_source                 READ {'code': 'B'}
V9  mask_secret_ref file:         READ 32 bytes
V18 snapshot_file digest          READ (digest computed)
V22 discovery read_text           READ SENTINEL-...
V22 discovery COPY TO             refused: DiscoverySqlError   (already blocked by the keyword filter)
V5  quarantine output, any path   stage created beside q.jsonl
V1  source path, absolute         READ schema ['id', 'col_secret']
```

The same probe on the E1 head with no policy (the CLI): V15 and V22 read_text are refused, everything
else reads as before, which is exactly the two documented behavior changes. Under a strict policy
(`tests/fileaccess/test_policy_denials.py`) each of these is the one denial. The tests that need the
new API (`file_access=`, `decoy_engine.fileaccess`) are red on the base by `ImportError` or
`TypeError`; the behavior-level red is the table above.

## Sink inventory (E1-4 sentry)

The scan resolves 163 call sites that need a registry entry, plus the adapter and capability modules
that pass by rule. By category: 39 ambient `LocalFileAccess` opens in `_local.py`; 33 in the isolated
route and the caller-named scratch root (ambient only, behind `require_ambient`); 60 engine scratch,
`/proc`, cgroup and temp-filesystem sites (out-of-core spill, pool quality, governor, disk probes);
9 in the cloud readers and SFTP (in-memory buffers and the known-hosts file, FA-2); 22 package data,
in-memory buffers and engine-owned handle constructions. The full list, with each call site's
construction rule, owner and reason, is `tests/sentry/file_access_registry.toml`.

## Results

Run on the final tree (Python 3.11 is the native-companion venv; 3.10 is the CI mirror).

| Check | Result |
|---|---|
| `tests` (full), 3.11, strace available | 1 failed, 25371 passed, 172 skipped, 21 deselected, 59 xfailed |
| `tests/sentry` + `tests/fileaccess`, 3.10 | 2850 passed, 1 skipped (after the fd-count fix; the one failure in the first pass was a process-wide fd count, now counted per test tree) |
| `tests/sentry`, 3.11 | 2578 passed, 1 skipped |
| `tests/fileaccess`, 3.11 | 88 E1-0 characterization tests green on the base and on head; the policy suites pass |
| `ruff check`, `ruff format --check`, `mypy src` (3.10 venv) | clean (511 source files) |
| `scripts/test_flight.py` (check only) | 53/53 invariant checks, 5/5 fingerprints match golden |

The one failure in the full run is `tests/unit/test_v2_cloud_sources.py::TestCloudSourceEndToEnd::test_profile_gcs_source_via_mocked_client`:
`No module named 'google'` in the native venv. It fails on the base the same way (the venv lacks the
GCS extra) and touches no changed code.

Red before, new-API tests: collecting `tests/fileaccess` policy suites against the base source stops at
`ModuleNotFoundError: No module named 'decoy_engine.fileaccess'` (the conftest imports the package),
so every new test is red there. The behavior-level red is the probe table above.

Order of commits on `feat/file-access-policy`: E1-0 `d830c0a4`, E1-1 `b098c5ad`, a WIP checkpoint
`6440e23e` taken when a session limit interrupted the port, E1-3/E1-4 `7177c302`, the mask-secret text
read, the lazy-reader test fix, and the record/docs commit.

## dennis gate and remediation

dennis returned NO-GO on the first build: 3 HIGH, 6 MEDIUM, 4 LOW. Each is fixed at its source in
its own commit (the series after `1405151b`). Findings, root cause, fix, test:

| # | Finding | Root cause | Fix | Test |
|---|---|---|---|---|
| H1 | Strict policy turned every library exception into a denial: corrupt Parquet or YAML, `MemoryError`, `ArrowMemoryError`, ENOSPC and EDQUOT lost their class | `guarded` treated "the library failed" as "the policy said no" | A denial only when the policy decides. A library failure on an authorized handle is `FileAccessReadError` (`file could not be read: <position>`, no chain). Memory failures (the engine's own `is_memory_failure`, so DuckDB and OpenSSL shapes too) and ENOSPC/EDQUOT/ENOMEM keep their class; the resource `OSError` is rebuilt without its filename | `test_policy_errors.py`: corrupt Parquet, YAML, JSON, undecodable text; every under-cap shape survives; ENOSPC/EDQUOT keep errno, drop filename |
| H2 | `LazySource(path, scratch=True)` read any path through `engine_handle`, registry-legitimized | A public flag stood in for a capability | Flag removed. The out-of-core stager builds a `ScratchAccess` over its own directory descriptor, constructible only through `scratch_access_for` (issuer token); the isolated child uses the plain ambient source. The `engine_handle` registry entry is gone | `test_policy_scratch.py` |
| H3 | A non-UTF-8 secret file leaked the locator and a byte through `UnicodeDecodeError`, chained, with the whole file in `.object` | The decode error was the cause of the raised error | Under a strict policy the message names only the config position and is raised after the `except` block closes, so the decoder error is neither cause nor context. The ambient message is unchanged | `test_policy_errors.py`: no locator, byte value, cause or context. Checklist line below corrected |
| M4 | A raw thread fell back to ambient under a strict policy | `current()` only consulted `forbid_ambient()` | Using any non-ambient policy latches the process: a context with no policy raises, and `is_ambient()` is false there. Tests reset the latch in an autouse fixture | thread test and default-process test in `test_policy_entry.py`. Existing tests that called a no-policy entry after a strict one were reordered |
| M5 | Sentry missed stream, memory-map, numpy, HDF/Excel, `codecs.open`, `ctypes.CDLL`, `runpy`, `spec_from_file_location`, `pyarrow.orc`, chained pool submit | Incomplete Appendix B | All added with one negative fixture each; each test asserts the specific sink was flagged. `CDLL(None)` stays allowed in the one capability module that binds `openat2` | `test_sentry_flags_the_specific_sink` (14) and the capability case |
| M6 | The "no read" strace assertion could not fail | `read`/`pread64`/`close` were not traced | Added to the trace set. Positive control: a direct open, read and close is detected. D10 self-tests: in-process, child process, execve, `dir_fd` open, directory listing, `renameat2` (with `RENAME_NOREPLACE`, since glibc maps flag 0 to `renameat`), pyarrow, pandas, polars, DuckDB read and write | `test_policy_strace.py`, `test_strace_selftest.py` |
| M7 | Ambient vault write changed: read-only directory, missing-parent error text, hardlink | The write went through a staged replace | Restored: a path under the ambient policy truncates and rewrites in place (`vault_direct` label, issued only by `LocalFileAccess`). A strict `OutputDir` still gets an exclusive create. CHANGELOG corrected | `test_char_vault_in_place.py`, green on the base too |
| M8 | Generic field keys; `disguises` used `MODEL_PACK_ROOT` | A key was fixed per call site | `fileaccess.at_position(...)` carries the config position; the engine sets it where a corpus or reference is resolved (the code_set and joint_mask handlers, the chunked code_set resolver, the out-of-core resolver) and the loaders append their suffix. New `DISGUISE_ROOT` role | `test_policy_denials.py` |
| M9 | Probe route should be inconclusive under a strict policy | The probe would reach the isolated route | `resolve_probe_recovery` returns `None` when the policy is not ambient; `run_pipeline_isolated` and the governor keep failing closed with `FileAccessUnavailable` | `test_policy_routes.py` |
| L1 | `os.link` followed symlinks | default | `follow_symlinks=False` | covered by output tests |
| L2 | `warnings.catch_warnings` is process-global | | Dropped; library warnings are no longer suppressed under a strict policy | |
| L3 | `FdDirHandle.entries()` stat followed links out of the root | `DirEntry` defaults | Entries are wrapped; type queries never follow a link | `test_policy_rooted.py` |
| L4 | Entry-point sentry exempted whole packages | Prefix list | The exemption is six named seams with reasons, each asserted to exist and to reach the policy. `run_fk_out_of_core`, which the platform calls, is checked to be an entry point | `test_file_access_entry_points.py` |

Judgment calls added by the remediation:

- The latch in M4 is one-way for the process, like `forbid_ambient()`. A process that mixes ambient
  CLI calls with strict-policy calls must make the ambient ones first.
- `DISGUISE_ROOT` is a new `FileRole` member. It is additive, but the platform's policy must decide
  it (a deny-by-default policy already refuses it).
- Field keys fall back to the generic name where the engine receives a detached config dict with no
  table or column (`validate_joint_mask_config(cfg)`, `strategy_config_fingerprint(cfg)`). A host that
  knows the column wraps the call in `at_position`. Plumbing the position through those dict-only
  signatures would change public signatures, which this gate did not authorize.
- Library warnings are no longer suppressed under a strict policy (L2). None of the library calls in
  scope emits a warning that names a path; this is not verified for every library version.

## dennis round 2 (GO) and the fixes before the Codex final

- **M-1.** The strict guard passed text-matched memory failures through unscrubbed; Arrow's
  `Wrapping <cell> failed` carries the cell value (dennis's probe: `SSN-123-45-6789` read through the
  pandas adapter). Now only failures recognised by TYPE keep their object (`MemoryError` including
  `ArrowMemoryError`, `duckdb.OutOfMemoryException`, `OSError` ENOMEM/ENOSPC/EDQUOT with the filename
  dropped). A failure recognised only by its text is rebuilt as the same type around
  `scrub_error_text(...)`, raised `from None`, and kept only if `is_memory_failure` still matches the
  scrubbed text; otherwise it becomes `FileAccessReadError`. Test uses a real invalid-UTF-8 cell.
- **M-2.** The out-of-core runner carried a byte-for-byte copy of the stream driver's
  `_code_set_records_and_evidence_for_table`. The root cause of the missing position was the copy, so
  the runner now imports the one function and the copy is deleted (`_runner.py` 707 to 663 lines, census
  lowered). The shared function sets `at_position(tables.<t>.columns.<c>)`. One denial-key test per
  resolution site: the pandas `CodeSetHandler`, `resolve_pinned_code_set_records`, and the shared
  out-of-core resolver; a fourth test asserts the runner uses that same function. The M8 row above is
  corrected (no `at_position` in the pandas adapter).
- **L-1.** Field keys were re-encoded on every unpickle. `FileAccessDenied` and `FileAccessReadError`
  now rebuild around the stored (already encoded) key; `scrub` does the same. Test: two round trips.
- **L-2 (tracked follow-up, not fixed).** Write failures under a strict policy are reported as "file
  could not be read" because `guarded` serves reads and writes alike.
- **L-3 (tracked follow-up, not fixed).** `fileaccess/_errors.py` imports the memory classifier from
  `execution/_isolated_common`, a package-layering inversion; the classifier should move to a neutral
  module.

## Final verification (head 807ccd88, code last changed in 48647012)

| Check | Result |
|---|---|
| `tests` (full), 3.11 | 1 failed, 25436 passed, 172 skipped, 21 deselected, 59 xfailed |
| `tests/sentry` + `tests/fileaccess`, 3.10 | 2916 passed, 1 skipped |
| `tests/sentry` + `tests/fileaccess` + `tests/native/test_column_access_surfaces.py`, 3.11 | 3101 passed, 1 skipped |
| `ruff check`, `ruff format --check`, `mypy src` (3.10 venv) | clean |
| `scripts/test_flight.py` (check only) | 53/53 invariants, 5/5 fingerprints match golden |

The one failure is `tests/unit/test_v2_cloud_sources.py::TestCloudSourceEndToEnd::test_profile_gcs_source_via_mocked_client`
(`No module named 'google'`, the native venv lacks the GCS extra); it is the same failure as before this work and
touches no changed code. The pinned dispatch inventory and the module-size census pass. The
`test_char_vault_in_place.py` characterization passes on the untouched base. The first full run after the
remediation was interrupted by a usage limit and discarded; the table above is a clean rerun.

## Verification after round 2

After the round 2 commits: `ruff check`, `ruff format --check` and `mypy src` clean (3.10 venv);
`tests/fileaccess` + `tests/sentry` pass on 3.10 and on 3.11 (2923 passed, 1 skipped each, `STRACE` set to
the scratch binary); the out-of-core, code_set and fileaccess files together on 3.11 passed (3717 passed
plus the seam-permit failure that the last commit fixed); `scripts/test_flight.py` check mode 5/5
fingerprints match golden. The full `tests` run was not repeated after round 2 (last full run: 1 failed,
25436 passed; the failure is the missing `google` module).

## Not verified

- The platform half (P1): `forbid_ambient()` wiring, `PrincipalFileAccess`, the platform sentries.
- Windows and macOS (strict policies are Linux 5.6 or later; the descriptor-walk fallback is tested on
  Linux only with the walk forced).
- Coverage and mutation on the changed units, dennis and Codex gates, engine CI mirror steps other than
  ruff, mypy and the tests named above (sphinx `-W`, the no-extras environment).
- A strict policy over a real `/proc`-less container.

## PR body draft: compatibility contract section 9 checklist

- [x] Additive public surface: every changed public signature adds a keyword-only `file_access=`
  or an optional trailing argument (signature diff in judgment call 17). Exceptions: the quarantine
  staged-file helpers now take and return an opaque `OutputTxn`, and six stdlib re-exports on private
  modules are gone.
- [x] Behavior unchanged without a policy except two refusals: a bare `../` traversal read (V15) and
  discovery SQL `read_text` (V22). Evidence: `redbefore_head` probe and `tests/fileaccess/test_char_*`
  (88 tests, green on base and head).
- [x] Output semantics preserved: publish, failure, and zero-record behavior of the table sink pinned
  by characterization tests on the base and still green.
- [x] Route parity: all 11 corpus jobs match the golden, with and without the native companion, under
  the ambient policy and under a real `RootedFileAccess` (`openat2`) and the forced descriptor walk.
- [x] No path or locator text in errors or logs for denials and read errors, including an undecodable
  secret file (`test_policy_errors.py`); the log-interpolation sentry passes. Ambient CLI messages
  keep their paths, as before.
- [x] Sentries pass on 3.10 and 3.11 after the final commit; registry has 163 reviewed entries.
- [x] Module sizes within the census; `_pipeline.py` unchanged at 684.
- [ ] Platform wiring (`forbid_ambient()`, `PrincipalFileAccess`, platform sentries): not in this PR.
- [ ] Version bump, tag, publish: not done; the 0.9.0 release needs Cam's go.
- [ ] Coverage and mutation on the changed units; dennis and Codex gates: not run.

## Rev 3.3 delta (X1-X4) finish: remaining gaps closed (2026-10-08)

A prior session built most of the rev 3.3 delta (X1 secret decode, X3 classification, most X2,
opaque scratch ids in `_runner.py`/`_relation.py`, the census and seam-permit updates) and was cut
off by a rate limit. This session closed the remaining AT-X2 gaps and investigated the AT-X4
holdouts.

Fixes (one commit each, on `feat/file-access-policy`):

- **X2 descriptor-path leak in discovery.** `fileaccess.redact_locators` replaced only the exact
  locator/display pairs it was given. A third-party message on an authorized file can name the
  descriptor path by a different fd number than the handle we recorded, so the pair miss left
  `/proc/self/fd/<n>` in the text. `redact_locators` now also collapses any `/proc/self/fd/<n>` or
  `/dev/fd/<n>` form to a fixed marker after the pair pass, which its own docstring already promised.
  Ambient text is unchanged (the ambient policy never hands a library a descriptor path).
  Closes `run_discovery_sql` at both execute and fetch time.
- **X2 strict denial in the name-hint loader.** Under a strict `DirHandle` a missing member raises
  `FileAccessDenied` (deny-by-default, no existence oracle; D5), not `FileNotFoundError`. The
  loader's `_is_file` caught only the ambient filesystem errors, so the denial escaped instead of
  the loader raising its own `NameHintLoaderError` at the config position. `_is_file` now also
  catches `FileAccessDenied`.
- **X2 lockfile test ordering.** `assert_lock_matches_installed` is not an entry point, so its
  ambient positive control went through `capture()`. The test placed one ambient control AFTER a
  strict `scope` block, and installing a strict policy latches the process one-way (M4), so that
  control was refused with `FileAccessUnavailable`. Reordered so both ambient controls run before
  the strict checks, as every other AT-X2 test already does. No assertion changed; only the
  sequence. (`dp_provenance.py` already named the config position via `fileaccess.shown`, so no
  source change was needed there.)

Codex-final finding -> now-passing test:

| Codex final finding | Requirement | Tests (all green unless noted) |
|---|---|---|
| 1 (config-derived scratch names) | X4 opaque scratch ids | `test_x4_scratch_and_names.py::test_scratch_stays_inside_its_root_and_the_output_is_unchanged`, `test_relations_sharing_one_parent_do_not_collide`, `test_the_isolated_source_scratch_names_are_opaque` PASS; `test_the_reorder_driver_names_nothing_from_a_column_or_edge` and `test_the_pool_spool_name_is_opaque_and_the_site_ran` left RED (test issues, see below) |
| 2 (shipped-name resolvers) | X4 membership-first | `test_shipped_corpus_resolvers_reject_unknown_names_without_a_file_system_call`, `test_the_unknown_corpus_message_is_fixed`, `test_shipped_names_still_load`, `test_reference_tables_check_membership_before_any_file_system_call` PASS |
| 3 (secret material in an error) | X1 | `test_x1_secret.py` (all 10) PASS |
| 4 (locators, execute/fetch) | X2 | `test_x2_locators.py::test_discovery_errors_at_execute_and_fetch_name_no_locator` PASS (descriptor-path fix) |
| 5 (locators; YAML tag content) | X2 + D5 scope | all `test_x2_locators.py` PASS; content from an authorized file may appear (D5), only locators may not |
| 6 (classification survives a translating handler) | X3 | `test_x3_classification.py` (all 5) PASS |

Two AT-X4 tests left RED, reported to Cam, NOT edited to force green:

- `test_the_pool_spool_name_is_opaque_and_the_site_ran`. The spool already uses an opaque per-run
  name (`pairs_<uuid>.parquet`, `_pool_quality.py` ~244) and the site executes for every column.
  The failing assertion is `assert column not in path.name` for the WEIRD column `"."`, which is
  unsatisfiable alongside the required `path.name.endswith(".parquet")`: every Parquet filename
  contains `.`. This is a test-authoring bug (the WEIRD list from the confinement test is reused for
  a substring-in-filename check). Recommended fix: assert against `path.stem` (the part the code
  constructs), or drop `.`/`..` from the spool-name embedding loop. Needs sign-off.
- `test_the_reorder_driver_names_nothing_from_a_column_or_edge`. The probe records the output's
  PARENT dir (`tmp_path`) and the sibling `_decoy_stage_*` staging dir. Both come from the
  `ParquetTransactionalSink` output transaction: `LocalOutputTxn.publish()` does
  `target.parent.mkdir(parents=True, exist_ok=True)` before the atomic rename, and the stage is a
  `tempfile.mkdtemp(dir=target.parent)` beside the target (same filesystem, so publication is one
  rename). This is legitimate output-staging near the declared output dir, identical on main, with
  an opaque name derived from no column or edge. It is NOT a scratch-confinement escape. The test's
  assertion (every probed path under `work` or `tmp_path/out`) is too strict. Recommended fix:
  point the sink target under `work`, or allow the output-parent interactions in the assertion.
  Left RED pending Cam's sign-off (a security test must not be reinterpreted to pass).

Verification (head after these commits; `~/bin/pytest-one`, native 3.11 venv, `STRACE` set to a
scratch strace binary since the devbox has none):

| Check | Result |
|---|---|
| `ruff check` / `ruff format --check` | clean (1398 files) |
| `mypy src` (3.10 `.venv`) | 5 pre-existing errors in native modules (`_operator_step.py`, `_faker_null_mask.py`, `_shadow_operators.py`: `decoy_engine_native` attr-defined), unrelated to the changed files; the 3.10 venv lacks the native companion. The three changed files are clean |
| `tests/fileaccess` + `tests/sentry` (3.11) | 5 failed, 3154 passed, 1 skipped. The 5 are the two AT-X4 tests above (the reorder one across 4 params). strace selftests PASS with the scratch binary (no environmental strace skips this run) |
| `scripts/test_flight.py` (check) | 53/53 invariant checks, 5/5 fingerprints match golden |
