# Dennis review: x7b r1

Verdict: GO. 0 BLOCKER / 0 HIGH / 0 MEDIUM / 5 LOW. V1 0 / V2-LOG 5.

Mode B. Repo decoy-engine. HEAD 071f5ab85cb9960f341d8bc90dbe4176c40ec979. BASE a749172e921b73dfae1a2089135d86aeb9eb8d75. Reviewed on merged tree a749172e921b73dfae1a2089135d86aeb9eb8d75 + HEAD (scratch merge 496e1c70, never pushed).
Main moved since BASE: 0 commits; overlapping files: none.
Review skills run: none. `code-review` and `security-review` exist on this box but target the session working directory, not the scratch merged tree; the diff was cold-read by hand instead (see "What I did NOT verify").

## Findings

### F1. LOW, V2-LOG, class 2 (mutation survives)
Where: src/decoy_engine/fileaccess/_output.py:225
What is wrong: nothing pins the strict stage's create mode before its `fchmod`. If `mode_bits` is 0o666 for private labels, the strict stage exists at 0666 under umask 000 until line 249 narrows it, and every test stays green. AT-P1a checks the moment before `fchmod` only on the ambient policy. AT-P1g reads the mode after `create_file` returns, which is after the `fchmod`.
Evidence: mutation O6 `mode_bits = 0o666 if self._private else 0o666` gave `41 passed` on test_write_private.py, test_private_files.py and TestPayloadFilePermissions. The current code is correct: 0o600 at create.
Fix: parametrize AT-P1a over the strict policy (RootRelative). Under umask 000, spy `os.fchmod` and assert that the first observed mode is 0600.

### F2. LOW, V2-LOG, class 2 (second rule hides a mutation)
Where: src/decoy_engine/fileaccess/_local.py:254
What is wrong: `self.abort()` on the ambient `fchmod` failure path is not pinned. Two other cleanups also remove the stage: `write_private`'s outer `txn.abort()` (`_private.py`) and `quarantine.py:379-380`. Removing line 254 changes no test result. Today this is harmless, because both callers abort.
Evidence: mutation O2 (drop `self.abort()`, keep `os.close(fd)`) gave `41 passed`.
Fix: add a direct `LocalOutputTxn.create_file` test with `os.fchmod` raising. Do not call `abort()`. Assert that no `_decoy_isolated_stage_*` entry remains. Or drop the inner abort and rely on the caller's abort.

### F3. LOW, V2-LOG, class 8 (registry disposition granularity)
Where: tests/sentry/file_access_registry.toml:1068-1071 (`LocalOutputTxn.create_file::os.open::2`, `disposition = "public"`)
What is wrong: this one row is the ordinary ambient stage for every non-private label. That includes `vault` (exclusive, `vault.py:345-348`), which the record treats as private on the `vault_direct` row (ordinal 0), and `quarantine_direct`. A per-row disposition cannot express per-label privacy. A future private file staged under a non-private label lands on this public row, and P2 never sees it. The record describes the row only as "the ordinary stage `os.open`". This is not a leak today: vault chunks are Fernet-encrypted, and on base the quarantine sidecar is a declared output.
Evidence: `_local.py:258-260` creates the stage at 0o666 for any label not in `PRIVATE_LABELS`. `vault.py:345-348` uses the `vault` label for an OutputDir argument.
Fix: name this as a limit in the sentry docstring and the record. Or add a check that every label whose content is private is in `PRIVATE_LABELS`.

### F4. LOW, V2-LOG, class 11 and class 4 (size cap and a miscounted claim)
Where: docs/records/2026-11-04-x7b-build.md:119
What is wrong: the record says "Changed lines without the two baseline JSON files: 998". Without the two JSON files the diff is 1142 lines. The 998 also leaves out the record's own 144 lines, and SPEC's budget counts the record ("record about 45"). SPEC's 800 cap is exceeded by about 340, not 198. SPEC says to trim the record first, and the record was not trimmed.
Evidence: `git diff --numstat a749172e 071f5ab8 | grep -v -e private-file-sinks.json -e file-access-registry.json` sums to 1108 added + 34 deleted = 1142.
Fix: correct the count in the record and get the owner's waiver for the overage.

### F5. LOW, V2-LOG, class 13 (unstated behavior change on an existing label)
Where: src/decoy_engine/fileaccess/_local.py:250-255, src/decoy_engine/fileaccess/_output.py:247-252
What is wrong: `PRIVATE_LABELS` contains `quarantine`, so the existing quarantine stage now gets an `fchmod(0o600)` on both policies. This changes two things. Under a umask that masks the owner bits, the stage was 000 and is now 0600. And an `fchmod` failure, for example on a filesystem that refuses chmod, now fails a quarantine write that used to succeed. SPEC requires this change, but SPEC and the record both say "no other label changes behavior". Under the normal umasks 000 and 022 the stage mode is 0600 before and after (probe below).
Evidence: probe `quarantine[ambient] umask022 stage mode=0o600`, `quarantine[strict] umask022 stage mode=0o600`. The code paths are quoted above.
Fix: say it in the record. No code change needed.

## Prior findings (mode B rounds 2-3 only)
N/A, round 1.

## What I ran

All commands ran from ~/review/x7b-r1/merged with DECOY_VENV=~/review/x7b-r1/venv. The run used one venv built from the merged tree's uv.lock, and gates.sh ran the base in the same venv.

- STEP ZERO: `git rev-parse origin/feat/x7b` = 071f5ab8 (matches). BASE exists and is an ancestor of HEAD. MAIN = a749172e = BASE. Report branch absent. Push dry-run OK.
- `bash scripts/dev/bootstrap.sh`: exit 0, `RESULT:     OK, environment matches uv.lock`, native companion not installed.
- `timeout 1800 bash scripts/dev/gates.sh --keep-going --baseline`: exit 0.
  - Branch: env, ruff, format and mypy PASS. sentry passed=4067 failed=0 skipped=2. licenses 63/0. tests (41 selected files) passed=934 failed=0 skipped=1.
  - Baseline: ruff, format and mypy PASS. sentry 4046/0. tests 893/0, 1 skipped.
  - `FAILING TEST IDS (branch=0 baseline=0 branch-only=0)`.
- `venv/bin/python scripts/dev/ratchet_check.py`: rc 0. It prints `file-access-registry: 201 key(s)` and `private-file-sinks: first inventory, 23 key(s), review required`, with no other finding. No registry key was gained and the Fallback was not used. `grep -c '^\[\[entry\]\]'` = 201. 57 creating rows: 29 read, 23 private, 5 public.
- Named tests: `pytest -q -rfE -p no:cacheprovider tests/fileaccess/test_write_private.py tests/sentry/test_private_files.py tests/unit/execution/test_isolated_run.py::TestPayloadFilePermissions tests/unit/test_public_api.py tests/sentry/test_public_import_boundary.py tests/sentry/test_module_size.py tests/sentry/test_file_access_boundary.py`: 951 passed.
- Kept list and impact list: every test file that mentions quarantine, plus the SPEC `git grep` set and test_log_interpolation.py (108 files), run with `-m "not benchmark and not packaging and not codspeed"`: 4822 passed, 108 skipped, 0 failed.
- Red for the right reason. I checked out `ec81bb2` (commit 1) `src`, the registry and the registry baseline into the scratch tree, ran the final tests, then restored (`git diff --quiet HEAD` exit 0). Result: 24 failed. Every failure was an AssertionError or a pytest.raises "DID NOT RAISE": `[438]` (AT-P1a), `assert 0 == 384` (P1b x2), DID NOT RAISE (P1b2, P1c[fchmod], P1f[mkstemp, fchmod]), sentinel escaped (P1c[zero], P1f[zero]), `420 == 384` (P1d), `b'secret' == b'old bytes'` (P1e), pathname in str(exc) (P1f begin, write, partial, fsync, close, replace, abort), `438 == 384` (P1g, P1h, AT-P3), plus the P1, stale-baseline and P4 sentry tests. None was an ImportError, TypeError or AttributeError. This matches the record.
- Mutations. Each ran on test_write_private.py + test_private_files.py + TestPayloadFilePermissions, was restored with `git checkout --`, and `git diff --quiet HEAD` was clean after each.
  - E1 private mkstemp -> os.open 0o666, fchmod kept: red, AT-P1a, P1f[mkstemp].
  - E2 drop "isolated" from PRIVATE_LABELS: red, 13 tests including AT-P1a, b x2, b2, g, h, AT-P3.
  - E3 drop ambient fchmod: red, P1b[ambient], P1c[fchmod], P1f[fchmod].
  - E4 drop strict `_call(os.fchmod)`: red, P1b[strict], P1b2.
  - E5 private labels take write-through: red, P1e.
  - E6 skip txn.abort(): red, P1c[write, partial, zero, fsync, replace].
  - E7 `written < 0`: red, P1c[zero], P1f[zero].
  - E8 raw OSError at replace only (re-raise when filename2 is set): red, P1f[replace] only.
  - E9 abort error replaces the first: red, P1f[abort].
  - E10 payload site back to write_text + os.chmod: red, P4 and AT-P3.
  - E11 P1 accepts a missing disposition: red, seeded master_key.
  - E12 P2 treats every row as baselined: red, seeded key_dir and the save private row.
  - E13 P3 ignores fchmod: red, seeded swallowed_fchmod.
  - O1 (mine) translate inside the handler, without `from None`: red, all 10 P1f cases.
  - O2 (mine) drop `self.abort()` at _local.py:254: SURVIVED (F2).
  - O3 (mine) drop `os.close(fd)` on the ambient fchmod failure: red, P1c[fchmod].
  - O4 (mine) drop `_close(fd)` on the strict fchmod failure: red, P1b2.
  - O5 (mine) drop the fd close on the write_private failure path: red, P1c[write, partial, zero, fsync].
  - O6 (mine) strict mode_bits 0o666 for private: SURVIVED (F1).
  - O7 (mine) P4 treats a non-constant mode as safe: red, seeded module_alias_variable_mode.
  - O8 (mine) P3 ignores an `...` handler body: red, seeded swallowed_fchmod.
- Probe script (scratch, outside the tree) on the merged tree:
  - Strict exit matrix (write, partial, zero, fsync, replace). Each raised an errno-only OSError with no marker, filename None and cause None. The target was intact (bytes and 0644). There was no stage file and no unclosed fd.
  - Non-root (uid 65534, forked) under umask 0777, ambient and strict: mode 0o600 and `read=b'secret'` on both.
  - Quarantine stage under umask 022: 0600 on both policies.
- Commits: 8 commits. Every subject is `type(scope): summary`, at most 65 characters and lowercase after the colon. Every message's last line is `cam`, with no other trailers. Em-dash count in the diff and in the messages: 0 and 0.
- Size and surfaces:
  - `_isolated_run.py` has 651 lines. test_module_size.py is untouched.
  - The only new files are `fileaccess/_private.py`, tests, fixtures, the baseline and the record. Nothing new under execution/.
  - Untouched: `_types.py` (StageLabel, FileRole, CONTRACT_VERSION), `decoy_engine/__init__.py`, sdk.py, _ratchet.py, ratchet_check.py, pyproject, uv.lock, CHANGELOG and file_access_modules.toml.
  - Every changed test file only adds lines. No assertion was removed and no skip or xfail was added.
- Line anchors in the record checked: `_local.py:236`, `_isolated_run.py:411`, `_isolated_worker.py:70,133,192`.

## What I did NOT verify

- The full suite. That is the orchestrator's T3 on the merge head.
- Postgres and native-companion tests. They skip on this box.
- Sphinx. I did not run sphinx-build, so the record's "342 warnings on base and head" claim is unchecked.
- The review skills (`code-review`, `security-review`). They were not run because they target the session's working directory, not this scratch tree.
- macOS. Everything ran on Linux.
- DuckDB spill files under `temp_directory` that `read`-classified execute rows may create. P1 cannot read SQL, a named limit. I spot-checked the COPY sites only.
- `os.lchmod` and `.chmod()` on receivers the scan does not type as `Path`. These are named limits of the sentry.
