# SBOM (CycloneDX)

**Status:** generator implemented (release readiness R1-c). The SBOM is not signed,
published or attached to any release; that wiring belongs to the release workflow.

`scripts/release/sbom.sh` writes a CycloneDX 1.6 JSON SBOM for the runtime closure of
the `decoy-engine` wheel: the `[project].dependencies` packages pinned in `uv.lock`
(base profile). Extras (`dev`, `lint`, `mutation`, `xdist`, ...) are not included.

```bash
scripts/release/sbom.sh uv.lock sbom.cdx.json
```

Needs `uv` on PATH and network access to PyPI on the first run (the tools below run
through `uvx`; nothing is added to `pyproject.toml` or `uv.lock`).

## How it is built

`uv export --locked --no-dev --no-emit-project` turns the lock into a hashed pin list,
then the upstream `cyclonedx-py requirements` tool (`cyclonedx-bom==7.6.0`, with
`cyclonedx-python-lib==11.12.0`, both pinned in the script) writes the SBOM and
validates it against the CycloneDX schema. The format is never hand-written.

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

The script exits non-zero and writes nothing when the lock is missing or stale against
`pyproject.toml`, or names any source other than the public PyPI index (private index,
path, git or URL pins), because those would put infrastructure locations in the SBOM or
mislabel a package as `pkg:pypi`.

## Tests

`tests/packaging/test_sbom.py` (marker `packaging`): schema validation, completeness
against an independent walk of the lock, lock hashes, byte-identical reruns, leak canaries,
and the refusals above. It needs `uv`.
