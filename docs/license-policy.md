# License policy

What installing `decoy-engine` may pull in, and how `NOTICE` (shipped in the wheel as a
license file) is kept true. The engine itself is FSL-1.1-ALv2 (`LICENSE`); this policy covers
third-party packages only, so the FSL entry under "Not allowed" below is about dependencies, not
the engine or `decoy-engine-native`, which are first party.

## Scope

Every package in `uv.lock` reachable from `[project].dependencies` (marked `[runtime]` in
`NOTICE`) or from an optional runtime extra (marked `[extra: ...]`), over every Python
version and platform the lock covers. Which extras count as runtime and which as tooling
is recorded in `scripts/dev/license_policy.json` (`extras`); a new extra that is in
neither list fails the gate.

- Runtime extras: `cloud`, `gcs`, `geo`, `hashicorp-vault`, `mimesis`, `ner`,
  `sftp`, `vault`.
- Tooling extras, out of scope: `dev`, `docs`, `lint`, `mutation`, `perf`, `xdist`.
- Not covered: the separate `decoy-engine-native` companion (its Rust crates), system
  libraries, and anything a caller installs alongside the engine.

## Allowed

- **Permissive licenses**: Apache-2.0, BSD-2-Clause, BSD-3-Clause, ISC, MIT, MIT-0,
  PSF-2.0, Unlicense, and BSD-family metadata that names no clause count
  (`BSD-family-unspecified-clause`). The committed list is `allowed` in
  `scripts/dev/license_policy.json`.
- **Named exceptions**, each pinned to a package name, its exact license expression and a
  scope (`runtime`, or `extra` for a package that must stay reachable only through an
  optional extra), with a written reason (`exceptions` in the same file):
  - `paramiko` (LGPL-2.1, `sftp` extra) and `tqdm` (MPL-2.0 AND MIT, `ner` extra, pulled by
    spaCy): optional only. If either became reachable from the runtime dependencies, the
    gate fails.
  - `certifi` (MPL-2.0; `cloud`, `gcs`, `hashicorp-vault`, `ner` extras): accepted
    because MPL-2.0 copyleft is file-level and binds only changes to certifi's own files.
    It is installed unmodified as the CA bundle, so nothing of ours becomes MPL. Editing
    or vendoring its `cacert.pem` or modules is forbidden; use a separate CA bundle path
    instead.
  - `python-stdnum` (LGPL v2 or later per its metadata, runtime): the same exception
    decoy-platform's policy records for the same library.
  - `numpy` (`BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0`, runtime): numpy's own
    SPDX expression for the 3.11 and 3.12 pins; every term is permissive, but 0BSD, Zlib
    and CC0-1.0 are not on the allowlist, so the exact expression is pinned instead.
- **LGPL and MPL packages**: used unmodified, pip-installed as separate libraries that a
  user can replace with their own build. A vendored, copied, patched or forked copy of any
  of them in this repo or a built artifact is forbidden; that needs a new owner decision
  first. `NOTICE` says so for each.

## Not allowed

AGPL, SSPL, BUSL, FSL, GPL (non-L) and open-core enterprise licenses, in the runtime
closure or any runtime extra.

SDV and sdmetrics are not dependencies. sdmetrics is MIT, but SDV is BUSL-1.1 with an
exclusion for a Synthetic Data Service offered to third parties. The engine cites SDV methods
(HMA1: Patki, Wedge and Veeramachaneni, "The Synthetic Data Vault", IEEE DSAA 2016; PAR:
Zhang, Patki and Veeramachaneni, arXiv:2207.14406) as methods only, and uses no SDV code.
`tests/sentry/test_sdv_import_sentry.py` fails on any import of either package in `src/`,
`scripts/`, `testflight/`, `custom_providers/` or `examples/` (including
`importlib.import_module` and `__import__` literals; comments and docstrings that say
`sdv` are fine), on either appearing in `uv.lock` or an extra, and on any citation that
labels SDV MIT, Apache, BSD, ISC or PSF without saying BUSL.

## How it is enforced

- `NOTICE` lists name, version, license and scope for every pin in scope.
  `scripts/dev/licenses_write.sh` regenerates it: it exports the scoped pins from the lock
  with their hashes, installs the locked wheels (`--no-deps --require-hashes
  --only-binary`) into throwaway directories for CPython 3.10, 3.11 and 3.12 on Linux and
  Windows, and runs `scripts/dev/licenses.py --write` over their metadata. Every scoped
  pin must be found and every copy must classify the same.
- License metadata is read as PEP 639 `License-Expression`, else the `License` field,
  else the OSI classifier. Metadata that cannot be classified fails closed. A package
  whose metadata states nothing at all can get an override for its exact `name==version`
  (`overrides`) after reading its license file; today only `google-crc32c==1.8.0`
  (its `LICENSE` file is Apache-2.0).
- `python scripts/dev/licenses.py --check` (files only) fails when `NOTICE` is stale
  against the lock, was hand-edited, lists a license outside the policy, or the policy
  names an exception that matches nothing. `--verify-env` also compares `NOTICE` with the
  installed metadata of the current venv.
- `tests/sentry/test_license_policy.py` runs the same checks in the normal test run,
  with seeded fixtures (an AGPL runtime dependency, an LGPL package excepted for extras
  only, a dev-only AGPL package that must not count). Its installed-metadata tests compare
  `NOTICE` with what is installed; in a venv that drifted from `uv.lock` they skip, and
  with `DECOY_LICENSE_STRICT=1` they fail instead.
- `scripts/dev/gates.sh` runs both sentries as the `licenses` step with
  `DECOY_LICENSE_STRICT=1` in the lock-exact venv, and the `dp-certified-gate` CI job
  runs them the same way.

## Changing a dependency

1. Change `pyproject.toml` and the lock, run `scripts/dev/bootstrap.sh`, then
   `scripts/dev/licenses_write.sh`, and commit `NOTICE`.
2. A license outside the allowlist needs an owner decision recorded in
   `license_policy.json` with a reason. Do not loosen `allowed` to make a check pass.

## Owner items

- The licensor entity name: `NOTICE` carries a marked placeholder until it is decided.
