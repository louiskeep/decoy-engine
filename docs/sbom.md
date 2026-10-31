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
plus every transitive dependency (CPython 3.12, universal markers). Each run installs it into a
throwaway venv with `uv pip install --require-hashes --no-deps`, so nothing floats at run time
and a changed or tampered release fails the install. Regenerate it, after editing the pins in
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

- the lock is missing, or stale against `pyproject.toml` (`uv export --locked`);
- a package in the **runtime closure** (the root project's non-dev dependencies, per
  name and version) comes from anything but the public PyPI index. Dev groups and extras
  are out of scope, so a dev-only git or editable source does not block the SBOM;
- the exported pin list is anything but well-formed `name==version [; marker]` lines, each
  with at least one `sha256` hash line (options, comments, URLs, paths, orphan hashes);
- the tool requirements file is missing, lacks a hash, or any hash does not match.

Error messages name files by basename only.

## Tests

`tests/packaging/test_sbom.py` (marker `packaging`): schema validation (`sbom.sh --validate`),
completeness against an independent walk of the lock (PEP 503 names, `pkg:pypi` purls), lock
hashes, byte-identical reruns, leak canaries, and every refusal above. It needs `uv` and
network on first use.
