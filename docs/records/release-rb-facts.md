# Engine release R-b: facts for the platform and CLI repin

Status: prepared, not tagged. Branch `chore/release-rb-prep`, cut off engine main `da1a522f` and brought up
to date by merging main `0173c054`. It carries #363 (exact-integer categorical CDF builder), #362
(generated NOTICE, license allowlist gate, SDV citation fixes and import sentry), #361 (A-4 file-access
policy and secret providers reconciled onto main, F13, E1 owner-only secret files), #360 (typed
out-of-core temporal FK seam) and #359 (closed-set SBOM errors). The tag is cut by
`scripts/release/cut-release.sh` from main after this branch merges; until then the sha below is a
placeholder.

Every value is followed by the command that prints it, run from the engine checkout root at the release
commit with the lock environment active (`uv sync --frozen --extra dev --extra lint --extra vault --extra
cloud --python 3.10.20`, then `.venv/bin/python`).

## The release

| Fact | Value | Command |
|---|---|---|
| Engine version | `0.18.0` | `sed -n '/^\[project\]$/,/^\[/{s/^version = "\(.*\)"$/\1/p;}' pyproject.toml` |
| Same, as the module reports it | `0.18.0` | `python -c "import decoy_engine; print(decoy_engine.__version__)"` |
| Tag | `v0.18.0` (annotated, message `decoy-engine 0.18.0`) | `git ls-remote --tags origin refs/tags/v0.18.0` (empty until tagged) |
| Release commit | `<R-b main sha: the commit on main that carries this branch>` | `git rev-parse origin/main` before tagging (the `EXPECTED_SHA` given to cut-release.sh); after: `git rev-parse 'v0.18.0^{commit}'` |
| `PIN_PREFIX` for the platform | `<first 8 hex of the release commit>` | `git rev-parse 'v0.18.0^{commit}' \| cut -c1-8` |
| CHANGELOG section | `## [0.18.0] - 2026-11-01` | `grep -m2 '^## \[' CHANGELOG.md` |
| Release phase | `pre-ga` | `python -c "import decoy_engine; print(decoy_engine.RELEASE_PHASE)"` |

Why 0.18.0. The newest tag is `v0.17.0` (`git ls-remote --tags origin \| sort -V -k2 \| tail -1`), so
0.18.0 is the next minor. This release breaks consumers (a `file:` mask secret with group or other
permission bits is refused; `quarantine.write_jsonl_staged` returns an `OutputTxn`; registry 1.2.0;
weighted categorical masked output moves).
Compatibility contract section 5 calls for a deprecation path or a major bump for those, but section 1
puts the project in pre-GA, where engineering best practices section 8.1 (pre-GA = hard delete) needs no
shims, and section 7 accepts that a 0.x minor may break. 1.0.0 is kept for GA (`RELEASE_PHASE` flips to
`"ga"` in that same change). A patch (0.17.1) is ruled out: the changes are not fixes only.

Version single source: `pyproject.toml` `[project].version`. `tests/unit/test_version_agreement.py`
holds `decoy_engine.__version__`, the installed metadata, the `uv.lock` root entry, the newest dated
CHANGELOG section and the `docs/proof-manifest.json` stamp to it (`python -m pytest tests/unit/test_version_agreement.py`).
Three more pins hash the engine's own version or catalog and move with it: the reason release pin, both
DP certified rows, and the Phase F FR0 manifest (`engine_minor`).

## Contract versions the consumers hold

| Fact | Value | Command |
|---|---|---|
| Reason registry version | `1.2.0` (was `1.1.0` in v0.17.0) | `python -c "from decoy_engine import reasons; print(reasons.REGISTRY_VERSION)"` |
| Reason identities | `893` (884 `since` 1.0.0, 7 `since` 1.1.0, 2 `since` 1.2.0; 2 of them tombstones) | `python -c "from decoy_engine import reasons; print(len(reasons.manifest()['entries']))"` |
| New identities | `runtime.secret_file_permissions_unsafe`, `decoy.file_access_denied` | `python -c "from decoy_engine import reasons; [print(e['namespace'], e['code']) for e in reasons.manifest()['entries'] if e['since'] == '1.2.0']"` |
| Registry catalog digest | `8effa84c02204d7a35965af2643db0d45df2b87d518226e33dd7134a7c92fc26` | `cat tests/fixtures/f29/reason-registry-release.json`; `python scripts/release/reason_release_pin.py --check` |
| Reason / event schema versions | `1` / `1` (unchanged) | `python -c "from decoy_engine.reasons import _registry as r; print(r.REASON_SCHEMA_VERSION, r.EVENT_SCHEMA_VERSION)"` |
| `SEED_PROTOCOL_VERSION` | `7` (unchanged since v0.16.0) | `git grep -n '^SEED_PROTOCOL_VERSION' v0.17.0 HEAD -- src` |
| Native ABI the engine loads | `decoy-native-abi-3` (unchanged) | `git grep -n '^_EXPECTED_ABI_VERSION' v0.17.0 HEAD -- src` |
| Native companion version | `0.1.0` (own line, unpublished, not bumped by R-b) | `grep -m1 '^version' decoy-engine-native/pyproject.toml decoy-engine-native/Cargo.toml` |
| Compatibility contract | no version number; blob `771281a9bd77dd05b604da5fd89a65cdcd37b14b`, last changed in `da1a522f` | `git rev-parse HEAD:docs/compatibility-contract.md`; `git log -1 --format=%h -- docs/compatibility-contract.md` |
| Compat contract changes since v0.17.0 | one pre-GA note (A-4 merged with F13): `file_access=` on every entry point, the two new reasons, owner-only `file:` secrets, no caching of `env:`/`file:` secrets | `git diff v0.17.0 HEAD -- docs/compatibility-contract.md` |
| File-access entry points | `42` | `python -c "import decoy_engine.fileaccess as f; print(len(f.ENTRY_POINTS))"` |
| DP certified row, engine dev/CI profile (77 dists, 3.10.20) | `31ecf94503eff107631191426072eee757af72a2229ab594d2fc2e8e6bfac7b1` | `python -c "from decoy_engine.quality import dp_provenance as d; print(d.compute_lock_fingerprint(d.installed_distribution_set())); d.check_fit_environment()"` |
| DP certified row, CLI runtime profile (65 dists) | `b643894ded2ab84c4a0a9d9032a1de3d2330db89161f231e509024428b7533f3` | in the CLI repo, after the repin: `.venv-certified/bin/python scripts/cert_smoke.py` |
| `NOTICE` (generated from the lock, 103 pins) | sha256 `6e9be238acf9f205677b6d74019d872998fa5492dd2aab2838f46f64dd65469a`; regenerates byte-identical | `DECOY_VENV=.venv scripts/dev/licenses_write.sh && git diff --exit-code NOTICE` (prints `licenses: NOTICE matches the lock and every license is allowed`) |
| License gate | `63 passed` | `DECOY_LICENSE_STRICT=1 python -m pytest tests/sentry/test_license_policy.py tests/sentry/test_sdv_import_sentry.py` |
| SBOM of this lock (CycloneDX 1.6, reproducible) | sha256 `d9e8bb93d59ce00849e19bd7385d38709e08b23d298237348502906ff808d37a`, root `decoy-engine 0.18.0`, 40 components | `scripts/release/sbom.sh uv.lock /tmp/sbom.cdx.json && sha256sum /tmp/sbom.cdx.json` |

The engine row was installed: setting only `decoy-engine` back to `0.17.0` in the 77-dist set
reproduces the old row `5a76ea92...` exactly. The CLI row is derived, not installed: the 63 pins in the
CLI's `requirements-certified.txt` (CLI main `a5d36b4`) plus `decoy-engine 0.17.0` and `decoy-cli 0.1.0`
hash to the old row `a7b40d54...` exactly, and the same set with `decoy-engine 0.18.0` hashes to
`b643894d...`. `cert_smoke.py` on a real install is the proof of record.

Merging main `0173c054` (#362, #363) moved none of the pins: it changes no dependency, `pyproject.toml`,
`uv.lock` or reason identity. Re-derived on the merged head with the lock environment: the version
agreement test (8 passed), `reason_release_pin.py --check` (`reason release pin current: registry 1.2.0,
893`), the engine DP row (`31ecf945...`, and `check_fit_environment()` passes), `python
scripts/gen_proof_manifest.py` and `python -m tests.parity._fr0_refreeze > tests/fixtures/phase_f_fr0/manifest.json`
(both rewrite their files byte-identical: `git status` clean afterwards). The CLI-profile DP row hashes
distribution names and versions only, so it does not move either.

## Pin sites the consumers hold

Platform (read at platform main `d72bbec`). The platform is still on 0.16.0: its repin crosses 0.17.0 as
well, so the 0.17.0 items in `docs/records/release-ra-facts.md` apply too.

| Site | Now | Becomes |
|---|---|---|
| `pyproject.toml` dependency | `decoy-engine==0.16.0` | `decoy-engine==0.18.0` |
| `api/engine_compat.py` `ENGINE_TRANSFORM_OWNERSHIP_SPECIFIER` | `"==0.16.0"` | `"==0.18.0"` |
| `deploy/Dockerfile.production` `ARG DECOY_ENGINE_EXPECTED_VERSION` | `0.16.0` | `0.18.0` |
| `.github/workflows/backend.yml` and `fixtures-verify.yml` `ENGINE_PIN_SHA` / `ENGINE_PIN_VERSION` | `5da40c2c6ce67db86703996dd8cf245955f05eca` / `"0.16.0"` | release commit / `"0.18.0"` |
| `tests/test_engine_version_fence_packaging.py` `PIN_PREFIX` / `PIN_VERSION` | `"5da40c2c"` / `"0.16.0"` | `PIN_PREFIX` above / `"0.18.0"` |
| `tests/test_engine_ownership_gate.py` `PASSING` (and the `rc1`/`post1`/`dev0` failing variants) | `0.16.0` variants | `0.18.0` variants |
| `tests/test_ds2_postgres_pull.py` installed-version and pyproject assertions | `"0.16.0"` | `"0.18.0"` |

Command: `grep -rn '0\.16\.0\|5da40c2c' pyproject.toml api deploy .github tests --include=*.py --include=*.toml --include=*.yml --include=Dockerfile*`.

CLI (`louiskeep/decoy`, read at CLI main `a5d36b4`; already on 0.17.0):

| Site | Now | Becomes |
|---|---|---|
| `pyproject.toml` dependency | `decoy-engine==0.17.0` | `decoy-engine==0.18.0` |
| `tests/unit/test_pyproject_metadata.py` exact-pin assertion | `==0.17.0` | `==0.18.0` |
| `.github/workflows/ci.yml` (3 sites), `release-smoke.yml` | `git+https://github.com/louiskeep/decoy-engine@v0.17.0` | `@v0.18.0` |
| `.github/workflows/regen-diagrams.yml` | `decoy-engine@1132d29e40a0c947174fff65185fb95168ce504f` | `@<release commit>` |
| `requirements-certified.txt` header fingerprint (and its `decoy-engine 0.17.0` prose, install line), `scripts/cert_smoke.py` docstring | `a7b40d54...` on engine 0.17.0 | `b643894d...` on engine 0.18.0 (the 63 pins do not change) |

Command: `grep -rn '0\.17\.0\|a7b40d54\|1132d29e' pyproject.toml tests .github requirements-certified.txt scripts src`.

## What consumers must change

| Consumer | Change | Why (engine side) | How to find it |
|---|---|---|---|
| Platform | Every `file:` mask secret the platform writes or mounts must be owner-only (0600 or 0400). No deployed `file:` mask secret was found in `deploy/` (seeds use `cloud_ref` env vars); the one test fixture is `tests/test_s6a_bounded_preview.py:970` (`TestMaskSecretRefNeverLeaks`), which writes the secret under the default umask. Its assertions still hold, but it now exercises the `secret_file_permissions_unsafe` refusal instead of a keyed run: `chmod 0o600` it to keep covering the run path. | `runtime.secret_file_permissions_unsafe` | `git grep -n 'mask_secret_ref\|"file:' -- api tests deploy` |
| Platform | Map or allow the two new reason codes wherever reason codes are enumerated or pinned; registry version 1.2.0. | registry 1.1.0 -> 1.2.0 | `git grep -n 'registry_version\|REGISTRY_VERSION\|891' -- api tests` |
| Platform | The pin sites above. The 3a key-provider work (moving off `SecretKeyProvider(..., key_version="v1")` in `api/engine_a8_guard.py` and the vault tests) is separate and not required by R-b: `SecretKeyProvider` is unchanged. | — | — |
| CLI | Test fixtures that write a `file:` secret must `chmod 0o600` it: `tests/e2e/test_tokenize_cli.py` `_mask_secret_ref` (line 46) and the weak-secret cases (lines 344, 705), plus any other `file:` secret a test writes. The weak-secret cases expect `weak_mask_secret`; a group- or world-readable file is refused for its mode first. | `runtime.secret_file_permissions_unsafe` | `grep -rn 'file:{' tests` |
| CLI | User guidance that suggests `file:/PATH` (`src/decoy/_mask_secret_guidance.py`, the gdpr/pci/hipaa templates) should say the file must be owner-only (`chmod 600`). | same | `grep -rn 'file:/PATH' src` |
| Platform, CLI | Any golden, snapshot or expected value of weighted categorical masked output (`strategy: categorical` with `weights`) must be re-captured: at least one CDF threshold moves in about 99% of weight vectors (at most one slot each; a row changes category with probability about 5e-5; `[1, 1, 1]` gives `[333333, 666667, 1000000]`). Output is now the same on 3.10, 3.11 and 3.12. Configs with a `bool`, `str`, `Decimal`, `Fraction` or subclass weight (on every route), a weight under one slot after rounding, or a total above the largest double are refused. A grep of platform main `0ace724` found no pinned weighted categorical output. | CDF thresholds round half up over exact integers (#363) | `git grep -n -i 'categorical' -- tests \| grep -i weight` |
| CLI | The pin sites above, including the certified fingerprint. Without the new header and an engine-0.18.0 install, `fit_dp_snapshot` refuses with `dp_stack_uncertified`. | DP row `b643894d...` | `grep -n a7b40d54 requirements-certified.txt scripts/cert_smoke.py` |

## Public surface removed or changed since v0.17.0

Surface = every `decoy_engine` module with no `_`-prefixed or `internal` path segment, by `__all__` (or
public attributes where a module has none), plus function, method and dataclass-field signatures,
including names those modules re-export from private modules. Compared by AST over `git archive
v0.17.0 src` and `git archive HEAD src`.

No public module and no exported symbol was removed or renamed. `CodeSetConfig` moved to
`transforms/_codeset_config.py` and is still importable from `decoy_engine.transforms.code_set`
(`git grep -n 'CodeSetConfig' HEAD -- src/decoy_engine/transforms/code_set.py`).

| Changed | What a consumer does | Command that shows it |
|---|---|---|
| `quarantine.write_jsonl_staged(final_path, records)` | returns the staged `OutputTxn` (was the final `Path`); publish it | `git grep -n -A2 'def write_jsonl_staged' v0.17.0 HEAD -- src/decoy_engine/quarantine.py` |
| `quarantine.publish_staged_jsonl(staged_path, final_path)`, `quarantine.discard_staged_jsonl(staged_path)` | `staged_path` is the `OutputTxn` from `write_jsonl_staged` (was a `Path`; `discard` also takes `None`) | `git grep -n -A1 -e 'def publish_staged_jsonl' -e 'def discard_staged_jsonl' v0.17.0 HEAD -- src/decoy_engine/quarantine.py` |
| `keyprovider.key_provider_from_ref(ref, *, key_version=None, file_access=None)` | `file_access=` is new; `key_version=None` is unchanged since 0.17.0; a `file:` ref must be owner-only | `git grep -n -A3 'def key_provider_from_ref' v0.17.0 HEAD -- src/decoy_engine/keyprovider.py` |
| 42 entry points (`fileaccess.ENTRY_POINTS`: `run_pipeline`, `load_vault`, `vault_writer_for_config`, `VaultWriter`, `unmask_pipeline`, `apply_quarantine`, `profile_columns`, `generate_rows`, `iter_parquet_chunks` / `iter_csv_chunks` (also `field_key=`), ...) | keyword-only `file_access=None`; nothing to do without an installed policy | `python -c "import decoy_engine.fileaccess as f; print(*sorted(f.ENTRY_POINTS), sep='\n')"` |
| `ProviderSpec.seed`, `ValuePool.seed`, `SeedEnvelope.job_seed`, `StrategyContext.job_seed` / `.mask_key`, `ShadowContext.job_seed` / `.mask_key` | `repr=False`: no longer in `repr()` | `git grep -n 'repr=False' v0.17.0 HEAD -- src` (1 site at v0.17.0) |
| Out-of-core temporal FK columns | publish the resolved unit of the key (were always `timestamp[us]`); an instant outside it is refused with `out_of_core_fk_key_dtype_unsupported` | `git grep -n 'def fk_output_array' HEAD -- src/decoy_engine/execution/out_of_core/_join.py`; CHANGELOG `[0.18.0]` "typed temporal FK seam" |

Behavioral, not a Python symbol: weighted categorical masking moves output as described under "What
consumers must change" (`_build_cdf` is private; its refusals now carry the index only, no weight value). `env:` and `file:` secrets are never cached even with a positive
`SecretCachePolicy` TTL; under an installed strict policy `FileAccessDenied`, `FileAccessUnavailable`,
`MemoryError` and `ENOMEM`/`ENOSPC`/`EDQUOT` keep their class across secret admission.

Additive since v0.17.0 (no repin action): `decoy_engine.fileaccess` (+ `.adapters.arrow`, `.duckdb`,
`.pandas`, `.polars`, `.serial`), `fileaccess.reset_for_tests()`, `context.OpaqueBox`,
`LazySource.iter_batches(columns=)` / `to_table(columns=)`.

## Suite baseline on 3.10 and 3.12

Full suite, branch head `d9c17afa` against merged main `0173c054`, compared by test id (junit XML). Lock
venvs from `uv sync --frozen --extra dev --extra lint --extra vault --extra cloud --extra xdist --python
3.10.20` (and `--python 3.12.3`), one per tree, run with `python -m pytest -n 4 tests`. The `xdist` extra
takes the venv out of the certified DP profile, so 84 certified-DP tests skip there; their seven files were
re-run serially in the plain lock venv (3.10.20, no xdist): `221 passed`.

| Interpreter | Main `0173c054` | Branch `d9c17afa` | Differences by test id |
|---|---|---|---|
| CPython 3.10.20 | 2 failed, 32093 passed, 5371 skipped, 6 xfailed, 65 xpassed | 32096 passed, 5371 skipped, 6 xfailed, 65 xpassed | the two census tests below (failed -> passed); one new id, `test_perf_claims_cite_a_measurement[docs/records/release-rb-facts.md]` (passed) |
| CPython 3.12.3 | 2 failed, 32093 passed, 5371 skipped, 6 xfailed, 65 xpassed | 32096 passed, 5371 skipped, 6 xfailed, 65 xpassed | same as 3.10 |

| Row | 3.10.20 | 3.12.3 |
|---|---|---|
| Categorical CDF (`tests/unit/execution/test_categorical_weighted.py::TestCdfBuilder::test_skewed_weights_skew_cdf`) | passes | passes (failed on 3.12 before #363) |
| `tests/fileaccess/test_char_sink_publish.py::test_nul_and_overlong_names_fail_at_write_not_at_validation` | passes | passes; also passes on CPython 3.12.13, where it failed before this branch: 3.12.13 and 3.13 word the error `unlink: embedded null character in path`, 3.10 to 3.12.3 `embedded null byte`. The match accepts both; the `ValueError` and nothing-written assertions are unchanged |
| `tests/sentry/test_secret_material_census.py` (2 tests) | fail on main, pass on the branch | same |

The census failure is on main `0173c054` itself: #362 turned one-line SDV citation comments in
`context.py` and `transforms/grouped_series.py` into two lines, which moved six census bindings down one
line. The branch re-derives `tests/sentry/secret_material_census.json` with `python
scripts/secret_material_census.py --write` (line numbers only; 258 entries, 0 unreviewed).

The 3.12 rows are from the lock resolved with `--python 3.12` in this container (3.12.3). A full suite on
3.12.13 was not run.

## Release review fixes (Codex review of `39a0fe46`: four V1)

| Finding | Fix | Red-first test, failing line on `39a0fe46` |
|---|---|---|
| Production categorical routes coerced weights with `float(w)` before `_build_cdf`, so `bool`, numeric `str`, `Decimal`, `Fraction` and subclasses were accepted | `_admit_weights` is the one admission: the resident handler, out-of-core group B and the native prepared artifact pass raw weights through it; `_build_cdf` calls it too | `tests/unit/execution/test_categorical_weight_admission_routes.py`: :119 (handler), :127 (out-of-core), :134 (out-of-core FK route), :149 (native prepared, subclasses), :157 (`run_mask_chunked`), :173 (numpy and float32 weights declined natively); 27 red |
| The 0.18.0 re-pin replaced the 0.17.0 DP rows, so a 0.17.0 snapshot failed compilation with `dp_snapshot_provenance_uncertified` | the 0.17.0 rows `5a76ea92...` and `a7b40d54...` stay certified beside the 0.18.0 rows | `tests/unit/generation/test_dp_prior_release_rows.py`: `src/decoy_engine/plan/_checks_dp.py:203` (raised from :303) and `quality/dp_provenance.py:404`; 4 red |
| Three post-0.17.0 breaks sat under `[0.9.0]`, missing from the 0.18.0 Breaking list | moved into 0.18.0; Breaking lists bare `joint_mask.reference` names, shipped code-set names, discovery SQL file functions | `tests/unit/test_changelog_breaking_ratchet.py::test_released_sections_match_their_pins` :161 (`changed=['0.9.0']`) |
| `cut-release.sh` validated the fetch URL only; a `pushurl` could send the tag to a fork | every `git remote get-url --all` and `--push --all` URL must match, before and after the checks | `tests/unit/test_cut_release_script.py`: 5 red, the tag landed in the scratch fork in each |

Probed against `v0.17.0` (same lock venv, `v0.17.0` source on `sys.path`): a bare absolute `joint_mask.reference`
path and an absolute code-set stem loaded in 0.17.0 and are refused now; discovery `read_text`/`glob` read files in
0.17.0 and fail now; multiple statements, `VALUES`, `TABLE` and CTEs behave the same in both.

Suites at `4efe7b6c` against `39a0fe46` by test id (junit XML), lock venvs `--extra dev --extra lint --extra vault
--extra cloud --extra xdist`, `python -m pytest -n 4 tests`:

| Interpreter | `39a0fe46` | `4efe7b6c` | Differences by test id |
|---|---|---|---|
| CPython 3.10.20 | 32096 passed, 5371 skipped, 6 xfailed, 65 xpassed | 4 failed, 32145 passed, 5371 skipped, 6 xfailed, 65 xpassed | 53 new ids, all passed; 4 `test_isolated_memwatch_run.py::TestRealChild` ids failed under host load and pass on re-run (3 of 3 on each tree) |
| CPython 3.12.3 | 32096 passed, 5371 skipped, 6 xfailed, 65 xpassed | 32149 passed, 5371 skipped, 6 xfailed, 65 xpassed | 53 new ids, all passed |

Certified-DP files serially in the plain lock venv (3.10.20): `242 passed` on `39a0fe46`, `250 passed` on the
branch (the 8 new ids). Re-run on the branch: `release_checks.sh 0.18.0` passes (SBOM sha256 `d9e8bb93...`
unchanged); the dry run in a scratch clone whose `main` is the branch head passes and creates no tag; proof
manifest, FR0 witness, secret census (three line numbers re-derived) and module size pass on 3.10 and 3.12.

## Not run here

- The real tag push (`APPLY=1 scripts/release/cut-release.sh <sha>`), a GitHub release, PyPI upload.
- The native companion build and the GitHub native release workflow (`native-companion-release.yml`).
- The CLI-profile DP row on a real install (`cert_smoke.py`); derived by hash only, as stated above.
- The platform and CLI repins themselves.
- The full suite on CPython 3.12.13 (only `tests/fileaccess/test_char_sink_publish.py` ran there).
- The native companion is not built in this container, so native-only tests skip on both trees alike.
- The platform main revision cited in the pin-site and weighted-categorical rows was not re-read.
