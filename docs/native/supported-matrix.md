# Supported production matrix

> **Status:** reference. Defines what decoy-engine supports today across Python core
> install, the compiled `decoy-engine-native` companion, and the official production
> image, and where each fact comes from. This is Task 3.1 of the native-engine
> program; it does not change kernel behavior, the ABI tag, or any loader logic.

This page separates three questions that are easy to conflate: does the pure-Python
core install and run here, does a prebuilt compiled companion wheel exist for this
platform, and what does the official production image ship. Each has its own matrix
below, and none of the three should be read as evidence for either of the others.

## 1. Portable core / CLI support

`decoy-engine` and `decoy-cli` are pure Python (`py3-none-any` wheel, hatchling build,
no Rust toolchain required) and install wherever their Python dependencies do:
Python 3.10, 3.11, or 3.12, with pandas, Polars, and PyArrow available for the
platform. This is not bounded to a fixed OS/architecture list the way the companion
wheels are.

The compiled companion is optional. Where it is absent, every keyed-derivation and
deterministic-faker-index column reroutes to the pandas oracle at preflight, before
any output is produced. This is a **supported, correct, slower** mode, not a
degraded one: the oracle produces byte-identical logical output to the compiled
kernel, just without the throughput gain (see [section 7](#7-performance-evidence-kept-separate)).
Absence of the companion is never silently swallowed mid-run.

## 2. Prebuilt companion wheels (approved starter pack)

Four targets, each built for CPython 3.10, 3.11, and 3.12 (twelve wheel rows total),
via [`native-companion.yml`](https://github.com/louiskeep/decoy-engine/blob/main/.github/workflows/native-companion.yml). All four
use `PyO3/maturin-action` (pinned by commit SHA and exact `maturin-version`, with
`--locked --compatibility pypi`); this repo does not use cibuildwheel anywhere.

| Target | Runner | Built via | ABI + parity smoke |
| --- | --- | --- | --- |
| Linux x86-64 | `ubuntu-latest`, manylinux_2_28 container | maturin-action, native build | inside the pinned `manylinux_2_28_x86_64` container |
| Linux ARM64 | `ubuntu-latest`, cross-compile container | maturin-action, cross-compile (no QEMU needed to build) | inside the pinned `manylinux_2_28_aarch64` container, under QEMU |
| Windows x86-64 | `windows-latest` | maturin-action, native build | native, on the runner |
| macOS arm64 | `macos-15` (Apple Silicon) | maturin-action, native build | native, on the runner |

Every row runs the same reusable script,
[`decoy-engine-native/scripts/parity_smoke.py`](https://github.com/louiskeep/decoy-engine/blob/main/decoy-engine-native/scripts/parity_smoke.py),
against the built wheel: it asserts `abi_version() == "decoy-native-abi-2"`, then the
hash KAT and the index KAT (both below), including the exact Arrow return type.

**Hash KAT** (`derive_batch`, `people.ssn` namespace):

```
derive_batch(["alice"], mask_key=bytes(range(32)), namespace="people.ssn",
             truncate=None, native_threads=1)
== ["0e0f7092a5bfbb5b1719ff096993a5169d585c0916277d18746eb21c8b246acd"]  # SEED_PROTOCOL_VERSION 7
Arrow type: string
```

**Index KAT** (`derive_index_batch`, `pool.city` namespace):

```
derive_index_batch(["alice"], mask_key=bytes(range(32)), namespace="pool.city",
                    pool_size=97, native_threads=1)
== [72]  # SEED_PROTOCOL_VERSION 7
Arrow type: uint64
```

Windows and macOS wheel builds cannot run on this environment's hosts; they are
verified in CI on the pull request, not locally.

## 3. Official production image

Linux x86-64 only, today. ARM64 is explicitly deferred, not a near-term commitment;
revisit only if Cam changes the approved target list. Bundling the compiled companion
into the production image is Task 3.2 work, done in the platform repo
(`decoy-platform`); this page documents the target state and does not claim the
image already ships the companion.

## 4. GLIBC / manylinux baseline: three separate facts

These three are easy to blur into one claim. They are not the same fact and this repo
keeps them separately stated:

1. **Wheel-compatibility policy.** `native-companion.yml` pins `manylinux: "2_28"` on
   every Linux wheel build (`companion-wheel-linux` job). This sets the floor at
   glibc 2.28. It is a build-time compatibility policy applied by maturin's
   `auditwheel`-equivalent tagging, not a claim about any particular distro or
   container.
2. **Oldest tested runtime.** The Linux ABI + parity smoke (both x86-64 and ARM64)
   runs *inside* the official `quay.io/pypa/manylinux_2_28_x86_64` and
   `quay.io/pypa/manylinux_2_28_aarch64` container images, pinned by digest in
   `native-companion.yml`. That pinned container is the real, verifiable floor: if a
   wheel imports and passes both KATs there, glibc 2.28 is provably sufficient. The
   ARM64 *build* itself runs in a separate cross-compilation container
   (`ghcr.io/rust-cross/manylinux_2_28-cross`, also pinned by digest); that image is a
   build tool and is not the compatibility floor being tested.
3. **Planned production base.** The production image is Linux x86-64, glibc at or
   above the manylinux_2_28 floor. The exact pinned base image is owned by the
   platform repo's production Dockerfile
   (`decoy-platform/deploy/Dockerfile.production`), which is Task 3.2 scope. As of
   this writing that Dockerfile floats more than one base: two Python-stage `FROM`
   lines and a separate Node build-stage base, none pinned to a digest. Task 3.1 does
   not edit that Dockerfile and does not name a digest it cannot own or verify;
   pinning all of its floating bases is recommended platform work for Task 3.2.

## 5. Core-to-companion compatibility rule

The loader (`_crypto_ext.load_compiled_crypto_kernel`, `_index_ext.load_compiled_index_kernel`)
enforces five rules:

1. **Binary compatibility is exact ABI-tag equality.** The companion's
   `abi_version()` must equal the core's pinned `_EXPECTED_ABI_VERSION`
   (`"decoy-native-abi-2"`) exactly. There is no partial or range compatibility.
2. **ABI match is necessary but not sufficient.** Past the tag check, the loader also
   requires the needed symbol/callable to exist and be callable, and a known-answer
   test to pass. A companion that reports the right ABI tag but fails its KAT is still
   rejected.
3. **Breaking an existing ABI or its semantics requires an ABI-tag bump.** Any change
   that would alter the meaning of an existing entry point for existing callers (for
   example, a breaking change to how `native_threads` is interpreted) moves the tag,
   so mismatched pairs fail loudly rather than silently disagreeing.
4. **Additive capabilities stay on the same tag.** A new capability (the index kernel
   landing alongside the hash kernel is the precedent) is capability-detected at load
   time rather than forcing an ABI bump, since it adds without changing existing
   behavior.
5. **Core and companion versions are independent.** Their PEP 440 versions are not
   required to match; the ABI tag is the compatibility contract, not the version
   string. A production release records and pins the exact companion artifact used,
   for provenance, regardless of version-number alignment.

## 6. Fallback behavior

Where the portable core itself is supported and installable, absence of a compatible
companion causes a preflight whole-table oracle reroute (identical output, slower).
This is distinct from Task 3.2's official production worker, which will reject
startup outright rather than reroute, since a production deployment is expected to
have the companion present. Do not read this page as claiming the core and its heavy
dependencies install on every OS, architecture, and Python combination; [section
1](#1-portable-core--cli-support) states only what is actually guaranteed.

## 7. Performance evidence, kept separate

Wheel availability, install/import/KAT/ABI evidence, and measured-performance
evidence are three different claims. This page keeps them apart rather than letting a
correctness pass stand in for a speed claim:

- **Wheel availability:** section 2's table, current as of this page.
- **Install/import/ABI/KAT evidence:** the same `parity_smoke.py` run on every row in
  section 2's table, including under QEMU for Linux ARM64. QEMU proves the compiled
  kernel is *correct* under emulation; it is not a hardware performance measurement,
  and none is claimed for ARM64 from it.
- **Measured performance:** the compiled kernel's throughput gain over the
  pure-Python oracle has been measured only on x86-64 Linux hardware, for example a
  ~2.5x wall-clock speedup and per-hash-column throughput rising from roughly 235k to
  1.04M rows/s in one dev-box run (see
  `docs/plans/native-throughput-phase1-task1.2-record.md`), plus the streaming-route
  memory and wall-clock comparisons in
  `docs/plans/native-phase3-C1-gate.md`. Windows, macOS, and Linux ARM64 have no
  companion performance evidence yet; those platforms have correctness evidence only
  (the ABI + KAT smoke). Do not generalize the x86-64 speedup figures to other
  targets.
