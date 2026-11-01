# Engine release R-b: facts for the platform and CLI repin

Status: prepared, not tagged. Branch `chore/release-rb-prep` off engine main `da1a522f`, which carries
#361 (A-4 file-access policy and secret providers reconciled onto main, F13, E1 owner-only secret
files), #360 (typed out-of-core temporal FK seam) and #359 (closed-set SBOM errors). The tag is cut by
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
permission bits is refused; `quarantine.write_jsonl_staged` returns an `OutputTxn`; registry 1.2.0).
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
| SBOM of this lock (CycloneDX 1.6, reproducible) | sha256 `d9e8bb93d59ce00849e19bd7385d38709e08b23d298237348502906ff808d37a`, root `decoy-engine 0.18.0`, 40 components | `scripts/release/sbom.sh uv.lock /tmp/sbom.cdx.json && sha256sum /tmp/sbom.cdx.json` |

The engine row was installed: setting only `decoy-engine` back to `0.17.0` in the 77-dist set
reproduces the old row `5a76ea92...` exactly. The CLI row is derived, not installed: the 63 pins in the
CLI's `requirements-certified.txt` (CLI main `a5d36b4`) plus `decoy-engine 0.17.0` and `decoy-cli 0.1.0`
hash to the old row `a7b40d54...` exactly, and the same set with `decoy-engine 0.18.0` hashes to
`b643894d...`. `cert_smoke.py` on a real install is the proof of record.

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
| `keyprovider.key_provider_from_ref(ref, *, key_version=None, file_access=None)` | `file_access=` is new; `key_version=None` is unchanged since 0.17.0; a `file:` ref must be owner-only | `git grep -n -A3 'def key_provider_from_ref' v0.17.0 HEAD -- src/decoy_engine/keyprovider.py` |
| 42 entry points (`fileaccess.ENTRY_POINTS`: `run_pipeline`, `load_vault`, `vault_writer_for_config`, `VaultWriter`, `unmask_pipeline`, `apply_quarantine`, `profile_columns`, `generate_rows`, `iter_parquet_chunks` / `iter_csv_chunks` (also `field_key=`), ...) | keyword-only `file_access=None`; nothing to do without an installed policy | `python -c "import decoy_engine.fileaccess as f; print(*sorted(f.ENTRY_POINTS), sep='\n')"` |
| `ProviderSpec.seed`, `ValuePool.seed`, `SeedEnvelope.job_seed`, `StrategyContext.job_seed` / `.mask_key`, `ShadowContext.job_seed` / `.mask_key` | `repr=False`: no longer in `repr()` | `git grep -n 'repr=False' v0.17.0 HEAD -- src` (1 site at v0.17.0) |
| Out-of-core temporal FK columns | publish the resolved unit of the key (were always `timestamp[us]`); an instant outside it is refused with `out_of_core_fk_key_dtype_unsupported` | `git grep -n 'def fk_output_array' HEAD -- src/decoy_engine/execution/out_of_core/_join.py`; CHANGELOG `[0.18.0]` "typed temporal FK seam" |

Behavioral, not a Python symbol: `env:` and `file:` secrets are never cached even with a positive
`SecretCachePolicy` TTL; under an installed strict policy `FileAccessDenied`, `FileAccessUnavailable`,
`MemoryError` and `ENOMEM`/`ENOSPC`/`EDQUOT` keep their class across secret admission.

Additive since v0.17.0 (no repin action): `decoy_engine.fileaccess` (+ `.adapters.arrow`, `.duckdb`,
`.pandas`, `.polars`, `.serial`), `fileaccess.reset_for_tests()`, `context.OpaqueBox`,
`LazySource.iter_batches(columns=)` / `to_table(columns=)`.

## Not in R-b

- `fix/cdf-exact-arithmetic` (the `test_skewed_weights_skew_cdf` failure on 3.12) is not merged: only
  `plan/cdf-exact-arithmetic` exists on the remote (`git ls-remote origin | grep -i cdf`).

## Not run here

- The real tag push (`APPLY=1 scripts/release/cut-release.sh <sha>`), a GitHub release, PyPI upload.
- The native companion build and the GitHub native release workflow (`native-companion-release.yml`).
- The CLI-profile DP row on a real install (`cert_smoke.py`); derived by hash only, as stated above.
- The platform and CLI repins themselves.
