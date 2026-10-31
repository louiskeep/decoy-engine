# Main health audit: decoy-engine (2026-10-31)

Status: record (survey only; no code changed)

Audited head: `origin/main` = `a6e5c252ec55e63f86ca24342112d485080d78eb` (H-1 Wave A, #348, 2026-10-30).
Branch: `review/main-health-engine`, cut from that head. This record is the only change on it.

## Verdict

| Python | Verdict | Reds |
|---|---|---|
| 3.10.20 (lock profile, CI's version) | main is RED | 3 failed tests; 2 are owned by the STORM close-out, 1 is root-only. As a non-root user the only reds are the owned pair. mypy: 16 errors. |
| 3.12.3 | main is RED | 8 failed tests (the 3 above plus 5 that fail only on 3.12, 3 of them real behaviour differences). The pinned mypy gate cannot run on 3.12 at all. |

ruff check and ruff format --check are green on both. Nothing flaked: every failing test failed 4 of 4 reruns (alone, then three more times) on both interpreters.

## Environment (clean, from the lock)

Both environments were synced from `uv.lock` into fresh venvs with `uv sync --frozen --extra dev --extra lint --extra vault --extra cloud` (the profile `scripts/dev/bootstrap.sh` and the dp-certified-gate use). CI itself installs with unpinned `pip install -e ".[dev,cloud]"`, so CI's resolution is not what was measured here; the lock is.

| | 3.10 | 3.12 |
|---|---|---|
| Python | 3.10.20 (uv-managed) | 3.12.3 (system) |
| DuckDB / PyArrow / pandas / polars | 1.5.4 / 24.0.0 / 2.3.3 / 1.42.0 | same |
| numpy (lock resolves per Python) | 2.2.6 | 2.5.0 |
| pytest / mypy / ruff | 9.1.1 / 2.3.0 / 0.15.22 | same |
| extras installed | dev, lint, vault, cloud | same |
| not installed (skips) | decoy-engine-native, ml, h3, spacy, mimesis, postgres | same |
| OS / user | Ubuntu 24.04.5, 4 cores, 15 GB, uid 0 (root) | same |

Command (CI's regression-gate line, run serially, one runner at a time):
`python -m pytest tests -m "not benchmark and not packaging and not codspeed" --tb=short --durations=25`.
Note: this marker expression overrides `addopts`, so unlike a bare `pytest` it also runs the `determinism_harness` matrix (789 s) and `testflight` stays excluded only by path. That is CI's actual behaviour.

| Gate | 3.10 | 3.12 |
|---|---|---|
| pytest | 3 failed, 28318 passed, 5275 skipped, 45 deselected, 4 xfailed, 60 xpassed, 69 warnings, 1:24:36 | 8 failed, 28229 passed, 5359 skipped, 45 deselected, 4 xfailed, 60 xpassed, 11118 warnings, 1:17:05 |
| ruff check src tests testflight scripts | All checks passed | All checks passed |
| ruff format --check (same paths) | 1767 files already formatted | 1767 files already formatted |
| mypy src/decoy_engine testflight | Found 16 errors in 7 files (checked 655) | Found 2 errors in 2 files (errors prevented further checking) |

The 84 extra skips on 3.12 are the `dp_certified` suites (`requires the certified DP proof-stack; this env is not it`, 73) plus marker-level differences; on 3.10 the lock profile IS the certified stack and those suites ran and passed. Nearly all of the 11049 extra warnings on 3.12 (the largest blocks: `test_gen_window.py` 2711, `test_fr1_windowed_date.py` 1724, `test_dgrn_windowed_date.py` 1635, `test_pinned_generation.py` 683) are one numpy 2.5 `DeprecationWarning` ("The 'generic' unit for NumPy timedelta is deprecated, and will raise an error in the future") raised from two source lines, `transforms/windowed_date.py:267` and `execution/native/_bucket_perturb_ext.py:105`. Not red today; it becomes a hard error in a later numpy.

## Failing tests

Reruns: each id below was run alone and then three more times on the Python it fails on (`pytest -p no:cacheprovider -q <id>`): 4/4 failed every time. "First red" was found by running the single test in a detached worktree at earlier first-parent commits. Main's history begins at the squashed import `7795fc2f` (2026-10-18, 2260 files), so "predates history" means red at that import.

| # | Test id | Fails on | Exact failure line | Category | Class | First red |
|---|---|---|---|---|---|---|
| 1 | `tests/unit/execution/test_r3_failure_publication_trace.py::test_forced_out_of_core_on_an_ineligible_job_is_a_config_error_before_any_write` | 3.10, 3.12 | `AssertionError: assert 'OocIneligibleError' == 'ConfigError'` (`:79`) | REAL REGRESSION, OWNED | 10 (frozen contract drift) and 8 (overclaimed evidence) | `fdd08a89` F29 (#336, 2026-10-29): passes at its parent `33ef629b`, fails at `fdd08a89` |
| 2 | `...test_r3_failure_publication_trace.py::test_failure_and_publication_trace_is_unchanged[f_forced_ooc_incompatible]` | 3.10, 3.12 | `{'exception': {'type': 'OocIneligibleError', 'module': 'decoy_engine.execution._errors', ...}} != {... 'module': 'decoy_engine.errors', 'type': 'ConfigError'}` (`:49`) | REAL REGRESSION, OWNED | 10 and 8 | same as 1 |
| 3 | `tests/physical/test_shadow_snapshot.py::test_fixture_is_written_then_made_read_only` | 3.10, 3.12 (as root) | `Failed: DID NOT RAISE PermissionError` (`:61`) | ENVIRONMENT (root-only) | NEW: test assumes an unprivileged user | predates history (fails at `7795fc2f` as root) |
| 4 | `tests/unit/execution/test_categorical_weighted.py::TestCdfBuilder::test_skewed_weights_skew_cdf` | 3.12 only | `assert 899999 == 900000` (`:77`) | REAL REGRESSION (interpreter-version numeric drift) | NEW: determinism across interpreter versions | predates history |
| 5 | `tests/unit/execution/test_out_of_core_stream_join_reorder.py::test_close_releases_run_file_fd_on_partial_consumption` | 3.12 only | `assert 19 < 19` (`:547`) | REAL REGRESSION (fd not released on `close()`) | 2 (cleanup missing on an exit path) | predates history |
| 6 | `tests/native/test_chunked_entry_rev9_read.py::test_read_set_undecodable_string_literal_reads_every_passthrough_column` | 3.12 only | `assert frozenset({'x'}) == {'x', 'y'}` (`:457`) | REAL REGRESSION (documented invariant broken on 3.12) | 4 (route parity and silent fallback) | predates history |
| 7 | `tests/parity/test_phase_f_f2_mixed_admission.py::test_an_unparseable_sibling_when_declines_rather_than_guessing` | 3.12 only | `assert ({...'when': "f'{x}' == 'a'"}...} is None)` (`:536`) | REAL REGRESSION, same root cause as 6 | 4 | predates history |
| 8 | `tests/unit/test_c8_i_when_grammar.py::test_the_reserved_list_contains_numexpr_pandas_and_python_names` | 3.12 only | `AssertionError: ['type']` ... `Extra items in the left set: 'type'` (`:276`) | STALE EXPECTATION (frozen list older than 3.12's keyword set) | 10 | predates history |

### 1 and 2: forced out-of-core on an ineligible job (owned by the STORM close-out)

Proof: F29 (`fdd08a89`) added `class OocIneligibleError(ConfigError)` in `execution/_errors.py` and raises it from `_pipeline_routing.py`; the frozen trace baseline `tests/unit/execution/r3_failure_trace_baseline.json` pins `"type": "ConfigError"`, `"module": "decoy_engine.errors"`. The two tests pass at `33ef629b` and fail at `fdd08a89` (`2 passed` then `2 failed` in the bisect worktree). F29's own build record (`docs/records/2026-10-29-f29-engine-build.md`, line 21) reports a full-suite run with 3 failures that does not list these two, so the merge landed red without saying so (class 8). Owner: branch `claude/eager-clarke-14deh1` ("exact ConfigError on forced-route rejection", 2026-10-31) replaces the subclass with `ooc_ineligible()` returning an exact `ConfigError` tagged with `route.out_of_core_ineligible`; the test file is unchanged there. Audited on main only; not re-run on that branch. Smallest fix if the close-out does not land: raise exactly `ConfigError` carrying the registry reason, as that branch does. Do not change the baseline: the pinned type is the frozen contract.

The same branch also owns the Python 3.10 `add_note` canaries (its `tests/unit/phase/test_v1_canaries.py` sets `__notes__` directly); that module does not exist on main, so there is nothing to audit here.

### 3: read-only fixture guard (root-only)

Proof: `write_read_only_fixture` chmods the parquet to `0o444` and the test expects `path.write_bytes` to raise; root bypasses mode bits (CAP_DAC_OVERRIDE). As uid 65534 the test passes 3/3 (run with the 3.12 site-packages and `/usr/bin/python3.12`, since the uv-managed 3.10 interpreter lives under `/root`); as root it fails at every commit back to the import. Counter-case checked: it is not a code regression, the non-root pass and the root-only failure are the same tree. Product behaviour: unaffected; the guard protects the test harness's fixture, not a product path. What the test should do: `pytest.skip("file mode bits do not restrict root; run the suite as an unprivileged user")` when `os.geteuid() == 0`, so the assertion is never weakened for the user it is meant for. The companion `tests/physical/_shadow_helpers.py` fixtures with the same chmod do not assert on it and pass as root. Note for the gates: F29's record and the STORM close-out record both list this test as "pre-existing"; it has been baseline noise since at least 2026-10-18 because the local gates run as root.

### 4: weighted categorical CDF (interpreter-version numeric drift)

Proof: CPython 3.12 changed `sum()` of floats to compensated (Neumaier) summation. `_build_cdf` (`execution/_strategies/_categorical.py:75,92-94`) computes `total = sum(weights)` and a plain running sum, then `int(running / total * 1_000_000)`. For `[0.6, 0.3, 0.1]`: 3.10 gives `total = 0.9999999999999999` and thresholds `[600000, 900000, 1000000]`; 3.12 gives `total = 1.0`, running `0.8999999999999999`, and `[600000, 899999, 999999]` (last entry then pinned to the resolution by `cdf[-1] = _WEIGHTED_CDF_RES`). Checked directly in both venvs with a 5-line script. Product behaviour: affected. The band boundary moves by one slot in a million, so the same seed and plan can draw a different category for one derive_index value per boundary depending on the interpreter version. The compatibility contract pins library versions (Faker, pandas) but says nothing about the interpreter, so this is an unlisted determinism hazard. `math.fsum` alone does not fix it (exact rational arithmetic also truncates to 899999). Smallest correct fix: accumulate `running` and `total` as `fractions.Fraction` (exact) and round the threshold to the nearest slot instead of truncating, keeping the last-entry pin; then the thresholds are identical on every CPython and the pinned expectation `900000` holds. That is a determinism-visible change for some weight sets on 3.10 too, so it needs the golden and snapshot suites re-run as the acceptance test (red-first: a parametrised test asserting `_build_cdf([0.6, 0.3, 0.1])` on `sys.version_info >= (3, 12)` is red today).

### 5: ordered-join run-file descriptor not released by `close()` on 3.12

Proof: diagnostic script (same steps as the test, listing `/proc/self/fd` with `readlink`): on 3.10 `rows.close()` releases fd 5 (`.../reorder/run_00000000.arrow`); on 3.12 the fd stays open as `run_00000000.arrow (deleted)` after `close()` and after `gc.collect()`, and is released only on `del rows`. A 12-line pure-Python repro shows the interpreter difference: a generator whose `for` loop iterates an inner generator runs the inner `finally` on `outer.close()` in 3.10 but only on deallocation of the outer generator in 3.12. `_guarded_reorder_iter` (`out_of_core/_stream_join_cursors.py:72`) is exactly that shape over `sorter.iter_ordered()`, whose `pa.OSFile` context holds the fd. Product behaviour: affected on 3.12. The spill is unlinked but one fd per partially consumed ordered join stays open until the `_OrderedJoinRows` object is collected; the documented `close()` contract ("Release the ordered-run FD") does not hold. Smallest correct fix: in `_guarded_reorder_iter`, bind `inner = sorter.iter_ordered()` and wrap the loop in `try: ... finally: inner.close()`, so the fd release is explicit rather than a frame-teardown side effect. The existing test is the red-first test on 3.12.

### 6 and 7: f-string in a `when` predicate is no longer "undecodable" on 3.12

Proof: `predicate_names` (`execution/_column_access.py:73-97`) tokenises with pandas' tokenizer and returns `None` ("read every passthrough column") when a `STRING` token is not a plain str literal; its docstring names the f-string prefix as the case. On 3.12 (PEP 701) an f-string tokenises as `FSTRING_START` / `FSTRING_MIDDLE` / `FSTRING_END` with the braces' `NAME` tokens exposed, so no `STRING` token is seen and `predicate_names("s == f'{x}'")` returns `{'x', 's'}` instead of `None` (checked in both venvs). Test 7 is the same drift seen through `_mixed_static._mask_table_config`, which now narrows the config instead of declining with `fk_key_column_read_unknown`. Product behaviour: the closed grammar rejects f-strings on both interpreters (`parse_when` raises `ValidationError when_outside_closed_grammar`, checked), so a real config fails at validation before routing; the drift is confined to the conservative read-set fallback and the mixed-route admission step, but it is a documented fail-closed invariant that silently became fail-open on 3.12 (class 4). Smallest correct fix: in `predicate_names`, also `return None` when `kind == getattr(tokenize, "FSTRING_START", -1)`, keeping the 3.10 and 3.11 path unchanged. Tests 6 and 7 are the red-first tests on 3.12.

### 8: reserved-name list lacks the 3.12 soft keyword `type`

Proof: `keyword.softkwlist` is `['_', 'case', 'match']` on 3.10 and `['_', 'case', 'match', 'type']` on 3.12 (checked). `RESERVED_NAMES` (`expressions/_when_parser.py:54`) is "Frozen on purpose" and documents its Python section as "keywords and soft keywords". Product behaviour: a column named `type` is accepted as a `when` reference on every Python today; after the fix it is reserved. Smallest correct fix: add `"type"` to the frozen set (pre-GA, so no adapter) and note it in the changelog, since any config using a `type` column in a `when` predicate then fails validation. The alternative, asserting only against `keyword.kwlist`, weakens the sentry and is not recommended.

## Type-check errors

The pinned command (`mypy src/decoy_engine testflight`, mypy 2.3.0, `python_version = "3.10"` in pyproject) only measures anything on the 3.10 lock profile.

| Environment | Result | Why |
|---|---|---|
| 3.10, lock profile (dev, lint, vault, cloud) | 16 errors in 7 files | all `Module has no attribute ...  [attr-defined]` on `pyarrow.compute` functions: `execution/_mixed_fk_bridge.py:139-152` (6), `execution/native/_operator_step.py:523-543` (4), `generation/_streamed.py:265` (2), `execution/native/_date_shift_ext.py:179`, `execution/native/_bucket_perturb_ext.py:258`, `execution/native/_faker_null_mask.py:43`, `execution/physical/_shadow_operators.py:421` |
| 3.10, without the cloud extra | 18 errors | the 16 above plus `connectors/gcs.py:55` `google.api_core` and `:112` `google.oauth2` `[import-not-found]` |
| 3.12, lock profile, pinned command | 2 errors, then "errors prevented further checking" | `quality/dp_provenance.py:514` `Cannot find implementation or library stub for module named "tomli"` (the lock installs tomli only below 3.11, and mypy under `python_version = 3.10` follows the `else` branch) and `numpy/__init__.pyi:737: Type statement is only supported in Python 3.12 and greater [syntax]` (numpy 2.5.0 stubs, resolved by the lock on 3.12) |
| 3.12, diagnostic `--python-version 3.12` (not the pinned command) | 17 errors in 8 files | the 16 above plus `testflight/test_testflight_teeth.py:1206` `"int" has no attribute "item"` from numpy 2.5 stubs |

That is the 16 / 17 / 18 drift: 16 is the lock profile, 17 is a 3.11 or 3.12 interpreter with numpy 2.5 stubs, 18 is a 3.10 environment without `cloud`. The 16 are ENVIRONMENT (dependency stub gap): pyarrow 24.0.0 (released 2026-04-21) is the first release shipping `py.typed` (checked wheels 20.0.0 through 23.0.0: none ship it) and `pyarrow.compute` populates its functions at import, so the stubs do not declare them. Introducing commit: `7795fc2f` (2026-10-18), the first `uv.lock`, pins pyarrow 24.0.0; before the lock the unpinned CI install turned red the day pyarrow 24 was released. Product behaviour: unaffected. Smallest correct fix: the repo already uses `# type: ignore[attr-defined, unused-ignore]` for `pc.*` calls (`_stream_join_cursors.py:83`); apply the same to the 16 sites. Adding `pyarrow.compute` to the missing-stubs override would not help because the module IS found. The 3.12 abort is ENVIRONMENT too: the gate is defined for 3.10 and should be documented as 3.10-only, or the lock should pin numpy below 2.5 for 3.12 if a 3.12 type run is wanted.

## Candidates from the brief that are not red here

- `tests/unit/test_storm_eval_bands.py::TestLatencyBenchmark` (50 ms wall-clock budget): passed on both Pythons in the serial run and in 3 idle measurements per Python the slowest column was 21.2 ms on 3.10 and 16.1 ms on 3.12 (`benchmark_all_fixture_columns`, 17 columns). It is a wall-clock assertion inside the regression gate and F29's record reports it failing under 4-worker xdist load, so it is FLAKY TIMING under load only. Category proof: no failure in 2 full serial runs plus 6 measurements. Counter-case (it is slow for a real reason): rejected by the 2.4x idle margin. Smallest change: give the two latency tests the `benchmark` marker so the regression gate carries no wall-clock assertion; do not raise the 50 ms budget.
- `tests/unit/test_v2_cloud_sources.py::TestCloudSourceEndToEnd::test_profile_gcs_source_via_mocked_client`: passes with the lock profile (cloud installed). Without the cloud extra it fails with `ModuleNotFoundError: No module named 'google'` (checked in a dev+lint+vault venv). CI installs `cloud` for that reason (ci.yml comment). The test mocks `google.cloud.storage.Client` directly and has no `importorskip`; it should `pytest.importorskip("google.cloud.storage")` with the reason "cloud extra not installed", matching `tests/connectors/test_gcs.py`. Product behaviour: unaffected (the connector imports lazily).
- `tests/sentry/test_proof_manifest.py::test_proof_manifest_is_up_to_date`: passes on both lock environments. The STORM merge record reports it stale "by 18 Faker sample values in this environment"; that environment is not the lock (Faker is exact-pinned at 40.23.0 and the manifest regenerates identically here).
- `tests/unit/execution/test_byte_estimate_routing.py::...::test_width_change_flips_route_at_a_fixed_budget_and_row_count`: reported flaky under xdist in F29's record; passed serially on both Pythons here. Not reproduced.

## Fixes ordered by risk (lowest first)

1. Skip `test_fixture_is_written_then_made_read_only` when `os.geteuid() == 0` with a stated reason (test-only).
2. `# type: ignore[attr-defined, unused-ignore]` on the 16 `pyarrow.compute` sites (repo pattern; no runtime change).
3. `pytest.importorskip("google.cloud.storage")` in `test_v2_cloud_sources.py` (test-only).
4. Mark the two `TestLatencyBenchmark` tests `benchmark` (test-only; the gate loses a wall-clock assertion, the budget is unchanged).
5. `predicate_names`: return `None` on `FSTRING_START` (restores a documented fail-closed path on 3.12; no change on 3.10).
6. `_guarded_reorder_iter`: explicit `inner.close()` in a `finally` (fd release on 3.12; no behaviour change on 3.10).
7. Add `"type"` to `RESERVED_NAMES` (config-visible: a `when` referencing a column named `type` is rejected on every Python; pre-GA hard change, changelog entry).
8. Land the STORM close-out's exact `ConfigError` for forced-route rejection (owned; fixes 1 and 2).
9. `_build_cdf` exact arithmetic with round-to-nearest (determinism-visible; needs golden and snapshot re-runs and a compatibility note).
10. Replace the generic-unit timedelta usage at `windowed_date.py:267` and `_bucket_perturb_ext.py:105` before numpy turns the deprecation into an error (not red today).

## Not run or not verifiable here

- The native-companion suites (`decoy-engine-native` not installed: 376 + 110 + 109 + 68 + 60 + 46 + ... skips), the `ml` extra suites, `h3`, `spacy`, `mimesis`, the Postgres container suites (`cannot start postgres container`, 24), `packaging`, `codspeed`, `benchmark`, `testflight`.
- CI's own unpinned `pip install -e ".[dev,cloud]"` resolution; everything here is from `uv.lock`.
- The `dp_certified` suites ran only on 3.10 (the lock profile is the certified stack); on 3.12 they skip by design.
- Load conditions (xdist, parallel suites): all runs were serial; the latency and byte-estimate flakes reported by F29 did not reproduce serially.
- The STORM close-out branch itself was read, not executed.
- Python 3.11 (inside `requires-python` but not asked for).

## Commands (reproduce)

```
uv sync --frozen --extra dev --extra lint --extra vault --extra cloud --python 3.10.20   # and --python 3.12
python -m pytest tests -m "not benchmark and not packaging and not codspeed" --tb=short --durations=25 -p no:cacheprovider
ruff check src tests testflight scripts && ruff format --check src tests testflight scripts
mypy src/decoy_engine testflight
python -m pytest -p no:cacheprovider -q "<failing id>"        # alone, then three more times
git worktree add --detach <dir> <sha>; (cd <dir> && PYTHONPATH=src:scripts python -m pytest -q "<id>")   # first-red bisect
setpriv --reuid=65534 --regid=65534 --clear-groups env PYTHONPATH=<site-packages>:src:scripts /usr/bin/python3.12 -m pytest -c pyproject.toml "<id>"   # non-root proof
```
