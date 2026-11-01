# Engine release R-a: facts for the platform and CLI repin

Status: prepared, not tagged. Branch `chore/release-ra-prep` off engine main `1ee89e68` (which carries
#353 B-2 native release script fixes, #354 out-of-core correctness harvest, #355 CycloneDX SBOM). The tag
is cut by `scripts/release/cut-release.sh` from main after this branch merges; until then the sha below
is a placeholder.

Every value is followed by the command that prints it, run from the engine checkout root at the release
commit with the lock environment active (`uv sync --frozen --extra dev --extra lint --extra vault --extra
cloud --python 3.10.20`, then `.venv/bin/python`).

## The release

| Fact | Value | Command |
|---|---|---|
| Engine version | `0.17.0` | `sed -n '/^\[project\]$/,/^\[/{s/^version = "\(.*\)"$/\1/p;}' pyproject.toml` |
| Same, as the module reports it | `0.17.0` | `python -c "import decoy_engine; print(decoy_engine.__version__)"` |
| Tag | `v0.17.0` (annotated, message `decoy-engine 0.17.0`) | `git ls-remote --tags origin refs/tags/v0.17.0` (empty until tagged) |
| Release commit | `<R-a main sha: the commit on main that carries this branch>` | `git rev-parse origin/main` before tagging (the `EXPECTED_SHA` given to cut-release.sh); after: `git rev-parse 'v0.17.0^{commit}'` |
| `PIN_PREFIX` for the platform | first 8 hex of the release commit | `git rev-parse 'v0.17.0^{commit}' \| cut -c1-8` |
| CHANGELOG section | `## [0.17.0] - 2026-11-01` | `grep -m2 '^## \[' CHANGELOG.md` |
| Release phase | `pre-ga` | `python -c "import decoy_engine; print(decoy_engine.RELEASE_PHASE)"` |

Why 0.17.0. The newest tag is `v0.16.0` (`git ls-remote --tags origin \| sort -V -k2 \| tail -1`), so
0.17.0 is the next minor. This release changes frozen surface (pipeline config `version: 2`, the
`run_storm` field, the `storm-post-mask/v1` artifact tag, removed `keyprovider` functions, the keyed
`job_seed` fallback, a new reserved `when` name). Compatibility contract section 5 calls for a deprecation
path or a major bump for those, but section 1 puts the project in pre-GA, where engineering best
practices section 8.1 (pre-GA = hard delete) needs no shims, and section 7 accepts that a 0.x minor may
break. 1.0.0 is kept for GA (`RELEASE_PHASE` flips to `"ga"` in that same change). So: minor bump, no
major. The reason registry needs no bump of its own: it has never shipped (v0.16.0 has no
`decoy_engine/reasons`: `git cat-file -e v0.16.0:src/decoy_engine/reasons/__init__.py` fails), and A-4,
which carries registry 1.2.0, is not on main (`git merge-base --is-ancestor
origin/claude/jolly-carson-3k5stf origin/main` is false), so R-a ships 1.1.0.

Version single source: `pyproject.toml` `[project].version`. `tests/unit/test_version_agreement.py`
holds `decoy_engine.__version__`, the installed metadata, the `uv.lock` root entry, the newest dated
CHANGELOG section and the `docs/proof-manifest.json` stamp to it (`python -m pytest tests/unit/test_version_agreement.py`).

## Contract versions the consumers hold

| Fact | Value | Command |
|---|---|---|
| Reason registry version | `1.1.0` | `python -c "from decoy_engine import reasons; print(reasons.REGISTRY_VERSION)"` |
| Reason identities | `891` (884 `since` 1.0.0, 7 `since` 1.1.0; 2 of them tombstones) | `python -c "from decoy_engine import reasons; print(len(reasons.manifest()['entries']))"` |
| Registry catalog digest | `0dc162a3c38b37fb1ad4944e8cc77b70c141051ee92cbc20b3134de4e6b89df4` | `cat tests/fixtures/f29/reason-registry-release.json`; `python scripts/release/reason_release_pin.py --check` |
| Reason / event schema versions | `1` / `1` | `python -c "from decoy_engine.reasons import _registry as r; print(r.REASON_SCHEMA_VERSION, r.EVENT_SCHEMA_VERSION)"` |
| `SEED_PROTOCOL_VERSION` | `7` (unchanged since v0.16.0) | `git grep -n '^SEED_PROTOCOL_VERSION' v0.16.0 HEAD -- src` |
| Native ABI the engine loads | `decoy-native-abi-3` (unchanged) | `git grep -n '^_EXPECTED_ABI_VERSION' v0.16.0 HEAD -- src` |
| Native companion version | `0.1.0` (own line, unpublished, not bumped by R-a) | `grep -m1 '^version' decoy-engine-native/pyproject.toml decoy-engine-native/Cargo.toml` |
| Compatibility contract | no version number; blob `a1cd1a0d80becb1a83b42d360e5e09b806c04013`, last changed in `0090f6f9` | `git rev-parse HEAD:docs/compatibility-contract.md`; `git log -1 --format=%h -- docs/compatibility-contract.md` |
| Compat contract changes since v0.16.0 | F13 and `when`-reserved-name pre-GA notes; artifact tag `storm-post-mask/v1` -> `post-scan-integrity/v2` | `git diff v0.16.0 HEAD -- docs/compatibility-contract.md` |
| DP certified row, engine dev/CI profile (77 dists, 3.10.20) | `5a76ea92769d62cb0124d89d24c2ec0e4116828e34e0611d80bf5e36edef1a23` | `python -c "from decoy_engine.quality import dp_provenance as d; print(d.compute_lock_fingerprint(d.installed_distribution_set())); d.check_fit_environment()"` |
| DP certified row, CLI runtime profile (65 dists) | `a7b40d54f68488ecbcc253c35a03517eefc456625a9b0240470e765e2bbc4476` | in the CLI repo, after the repin: `.venv-certified/bin/python scripts/cert_smoke.py` |
| SBOM of this lock (CycloneDX 1.6, reproducible) | sha256 `a78848f9f91c9c84aff28fa1814db6193fd63c0d259981ca3996c41e44b04bc0`, root `decoy-engine 0.17.0`, 40 components | `scripts/release/sbom.sh uv.lock /tmp/sbom.cdx.json && sha256sum /tmp/sbom.cdx.json` |

The CLI-profile row is derived, not installed here: the 63 pins in the CLI's `requirements-certified.txt`
plus `decoy-engine 0.7.0` and `decoy-cli 0.1.0` hash to the old row `f2a5910f...` exactly, and the same set
with `decoy-engine 0.17.0` hashes to `a7b40d54...`. `cert_smoke.py` on a real install is the proof of
record.

## Pin sites the consumers hold

Platform (read at platform main `c7bc3dc4`):

| Site | Now | Becomes |
|---|---|---|
| `pyproject.toml` dependency | `decoy-engine==0.16.0` | `decoy-engine==0.17.0` |
| `api/engine_compat.py` `ENGINE_TRANSFORM_OWNERSHIP_SPECIFIER` | `"==0.16.0"` | `"==0.17.0"` |
| `deploy/Dockerfile.production` `ARG DECOY_ENGINE_EXPECTED_VERSION` | `0.16.0` | `0.17.0` |
| `.github/workflows/backend.yml` and `fixtures-verify.yml` `ENGINE_PIN_SHA` / `ENGINE_PIN_VERSION` | `5da40c2c6ce67db86703996dd8cf245955f05eca` / `"0.16.0"` | release commit / `"0.17.0"` |
| `tests/test_engine_version_fence_packaging.py` `PIN_PREFIX` / `PIN_VERSION` | `"5da40c2c"` / `"0.16.0"` | first 8 hex of the release commit / `"0.17.0"` |
| `tests/test_engine_ownership_gate.py` `PASSING` | `0.16.0` variants | `0.17.0` variants |

Command: `grep -rn '0\.16\.0\|5da40c2c' pyproject.toml api deploy .github tests --include=*.py --include=*.toml --include=*.yml --include=Dockerfile*`.

CLI (read at CLI main `df4c0691`):

| Site | Now | Becomes |
|---|---|---|
| `pyproject.toml` dependency | `decoy-engine==0.15.1` | `decoy-engine==0.17.0` |
| `tests/unit/test_pyproject_metadata.py` exact-pin assertion | `==0.15.1` | `==0.17.0` |
| `.github/workflows/ci.yml` (3 sites), `release-smoke.yml` | `git+https://github.com/louiskeep/decoy-engine@v0.15.1` | `@v0.17.0` |
| `requirements-certified.txt` header fingerprint (and its `decoy-engine 0.7.0` prose), `scripts/cert_smoke.py` docstring | `f2a5910f...` on engine 0.7.0 | `a7b40d54...` on engine 0.17.0 |

Command: `grep -rn '0\.15\.1\|f2a5910f\|engine 0\.7\.0\|decoy-engine==0\.7\.0' pyproject.toml tests .github requirements-certified.txt scripts src`.
Note: the CLI has been pinned to 0.15.1, so its repin crosses 0.16.0 as well (`TokenVaultWriter.put`
returns the authoritative token; see CHANGELOG `[0.16.0]`).

## Public surface removed or renamed since v0.16.0

Surface = every `decoy_engine` module with no `_`-prefixed or `internal` path segment, by `__all__` (or
public attributes where a module has none), plus constructor and function signatures. No public module
was removed (`decoy_engine.storm` was already gone at v0.16.0: `git ls-tree -r --name-only v0.16.0
src/decoy_engine | grep -ci storm` prints `0`).

| Removed or changed | Replacement | Command that shows it |
|---|---|---|
| `decoy_engine.keyprovider.resolve_key_provider` | `decoy_engine.secret_providers.admit_mask_key` | `git grep -n 'def resolve_key_provider' v0.16.0 HEAD -- src/decoy_engine/keyprovider.py` |
| `decoy_engine.keyprovider.resolve_mask_secret_ref` | `decoy_engine.secret_providers.admit_mask_key` | `git grep -n 'def resolve_mask_secret_ref' v0.16.0 HEAD -- src/decoy_engine/keyprovider.py` |
| `keyprovider.resolve_mask_key(..., mask_secret_ref=)` | parameter removed; admit the secret through `secret_providers` | `git grep -n -A8 'def resolve_mask_key' v0.16.0 HEAD -- src/decoy_engine/keyprovider.py` |
| `keyprovider.key_provider_from_ref` | returns `KeyProvider` (was `SecretKeyProvider`); `key_version` defaults to `None` (was `"v1"`) | `git grep -n -A6 'def key_provider_from_ref' v0.16.0 HEAD -- src/decoy_engine/keyprovider.py` |
| `keyprovider.is_pre_ga` (incidental re-export) | `decoy_engine.is_pre_ga` | `git grep -n 'is_pre_ga' v0.16.0 HEAD -- src/decoy_engine/keyprovider.py` |
| `PipelineConfig.version: Literal[1]`, `PipelineConfig.run_storm` | `version: Literal[2]`, `PipelineConfig.post_scan` (`PostScanConfig`, `integrity: bool`) | `git grep -n 'run_storm\|post_scan' v0.16.0 HEAD -- src/decoy_engine/config/` |
| `PostMaskReport.residual_pii` / `fk_preservation` / `policy_validation` lists | tuples | `git grep -n 'residual_pii:' v0.16.0 HEAD -- src/decoy_engine/verification/postmask/types.py` |
| `PostValidationRunner.run` wrote `quality_metrics` | returns the summary; the caller records it | `git grep -n -A3 'def run' v0.16.0 HEAD -- src/decoy_engine/verification/postmask/runner.py` |

Wire values renamed (not Python symbols): `quality_metrics["storm_postmask"]` ->
`["post_scan_integrity"]`; schema `storm-post-mask/v1` -> `post-scan-integrity/v2`; event `storm.scan`
-> `pre_scan.profile`; route reason `run_storm_requested` -> `post_scan_integrity_requested` and
resident reason `run_storm` -> `post_scan_integrity` (both old identities stay resolvable as registry
tombstones); error code `bad_secret_ref` -> `secret_provider_unknown` for an unknown ref scheme.
Command: `for p in run_storm storm_postmask storm-post-mask storm.scan; do echo "$p $(git grep -l -F "$p"
v0.16.0 -- src | wc -l) $(git grep -l -F "$p" HEAD -- src | wc -l)"; done` (only the tombstone file
keeps `run_storm` on HEAD).

Additive since v0.16.0 (no repin action): `decoy_engine.reasons`, `decoy_engine.phase`,
`decoy_engine.reason_adapter`, `decoy_engine.secret_providers` (+ `.vault`), keyword-only fields on the
progress events, `run_pipeline(secret_registry=, secret_cache_policy=)`,
`IsolatedRunResult.reason`, new `ProgressChannel` methods.

## Not run here

- The real tag push (`APPLY=1 scripts/release/cut-release.sh <sha>`), a GitHub release, PyPI upload.
- The native companion build and the GitHub native release workflow (`native-companion-release.yml`).
- The CLI-profile DP row on a real install (`cert_smoke.py`); derived by hash only, as stated above.
