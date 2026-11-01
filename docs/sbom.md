# SBOM (CycloneDX)

**Status:** generator implemented (release readiness R1-c). The SBOM is not signed,
published or attached to any release; that wiring belongs to the release workflow.

`scripts/release/sbom.sh` writes a CycloneDX 1.6 JSON SBOM for the runtime closure of
the `decoy-engine` wheel: the `[project].dependencies` packages pinned in `uv.lock`
(base profile). Extras (`dev`, `lint`, `mutation`, `xdist`, ...) are not included.

```bash
scripts/release/sbom.sh uv.lock sbom.cdx.json
```

Needs `uv` on PATH. **Network:** the first run downloads the tool's wheels from PyPI (and
CPython 3.12 if uv does not already have it); later runs reuse uv's cache, and an offline
host with a warm cache works. Nothing is added to `pyproject.toml` or `uv.lock`.

## How it is built

`uv export --locked --no-dev --no-emit-project` turns the lock into a hashed pin list, then
the upstream `cyclonedx-py requirements` tool writes the SBOM and validates it against the
CycloneDX schema. The format is never hand-written.

The tool itself is pinned end to end. `scripts/release/sbom-tool-requirements.txt` is the
fully resolved, hashed environment of `cyclonedx-bom==7.6.0` and `cyclonedx-python-lib==11.12.0`
plus every transitive dependency (CPython 3.12, universal markers, wheels only). Each run:

- creates a throwaway venv and refuses any interpreter but CPython 3.12;
- checks the tool file's grammar with that bare interpreter before uv reads it (hashed
  `name==version` pins and comments only: no index, find-links or trusted-host options,
  URLs or paths);
- installs it with `uv pip install --require-hashes --no-deps --only-binary :all:`, so
  nothing is resolved or built at run time and a changed or tampered release fails;
- runs the tool as `python -I -m cyclonedx_py`, so `PYTHONPATH` and user site cannot
  shadow the hashed packages.

Every `uv` call (venv, install, export) passes `--no-config`, and the script drops every
`UV_*` variable except `UV_CACHE_DIR`, `UV_NATIVE_TLS` and `UV_HTTP_TIMEOUT` (`--no-config`
skips `uv.toml` files but not `UV_*` variables such as `UV_INDEX_URL` or `UV_FROZEN`).
Regenerate the tool file, after editing the pins in
`scripts/release/sbom-tool-requirements.in`, with:

```bash
scripts/release/sbom-tool-lock.sh
```

(needs network; review the diff and rerun `tests/packaging/test_sbom.py`.)

## What the output holds

- Root component: `decoy-engine`, its version and declared license (from `pyproject.toml`).
- One component per locked pin: name, version, `pkg:pypi` purl, and the SHA-256 of every
  sdist and wheel the lock records, as hashes on the component's distribution reference.
  A package locked at several versions for different Python versions (for example numpy)
  appears once per version.
- No license for third-party components: `uv.lock` carries none and the generator reads the
  lock only. The dependency graph is flat (the requirements input has no edges).
- Package names only: no paths, hostnames, environment values, timestamps or serial number.

## Reproducibility

`--output-reproducible` removes `serialNumber` and `metadata.timestamp`, and the script
unsets `SOURCE_DATE_EPOCH` so the environment cannot put a timestamp back. Same lock and
same pins give byte-identical output.

## Refusals

The script exits non-zero and writes nothing when:

- the arguments are not `[LOCK [OUT]]` or `--validate FILE` (a usage message);
- the lock is missing, or stale against `pyproject.toml` (`uv export --locked`);
- a package in the **runtime closure** comes from anything but the public PyPI index, or
  has no version (a git or path source). The closure is the root project's non-dev
  dependencies, walked by name, version **and source**, so when the lock holds two entries
  for one name and version (say PyPI and git), the edge's `source` decides which one ships
  and a non-public twin cannot hide behind a public one. Extras that a runtime edge requests
  are followed. Dev groups and the root's own extras are out of scope, so a dev-only git or
  editable source does not block the SBOM;
- the exported pin list is anything but well-formed `name==version [; marker]` lines, each
  with at least one `sha256` hash line (options, comments, URLs, paths, orphan hashes), holds
  any character outside printable ASCII and `\n` (`\r` so CRLF, and the other separators
  `str.splitlines()` honours), has a non-canonical PEP 440 version, or has a pin that does
  not re-parse with `packaging.requirements.Requirement` as exactly `name==version [; marker]`;
- the generated SBOM's components are not exactly the exported pins (PEP 503 name and
  PEP 440 version, counted), or an exported pin lies outside the checked runtime closure;
- the tool requirements file is missing, breaks the grammar above, lacks a hash, any hash
  does not match, or a dependency has no wheel; or the venv interpreter is not CPython 3.12.

Error messages name files by basename only.

## Tests

`tests/packaging/test_sbom.py` (marker `packaging`): schema validation (`sbom.sh --validate`),
completeness against an independent walk of the lock (component count equals pin count,
PEP 503 names, `pkg:pypi` purls), lock hashes, byte-identical reruns, leak canaries, every
refusal above, and run-time isolation (`PYTHONPATH` shadowing, `uv.toml` and `UV_*`
overrides, a different interpreter, an sdist-only dependency, `CDPATH`). It needs `uv` and
network on first use.
