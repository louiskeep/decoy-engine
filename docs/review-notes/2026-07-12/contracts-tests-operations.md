# Contracts, Tests, Packaging, and Operations Review

> Lead re-anchor: the frozen reviewed revision is
> `c1c4f2c2b33af39e1de4874788a7df78a352970c`. It incorporates the previously
> user-owned pandas/mypy/annotation changes described below. A final clean-revision test run
> passed (`6281 passed, 38 skipped, 13 deselected`), as did ruff check, ruff format check, and mypy
> over 375 source files. The artifact findings were rechecked against a build using the same final
> tree content.

**Review date:** 2026-07-12
**Settled source snapshot:** `1e80016bdbf1aa100f7f21215c919df4c69f41b1`
(`chore/engine-ci-hardening`, based on `origin/main`)
**Initial artifact snapshot:** `1a7eee31172f5339ed3925a6cc5e42d689771c18`
(`bench/oom-route-e2e-probe`; the worktree changed during the review)
**Working-tree boundary:** user-owned changes to `pyproject.toml` and `uv.lock`, plus
untracked `.claude/worktrees/` and review documents, were observed and not edited.

## Problem Statement

Determine whether the engine's published Python contract, evidence, tests,
dependencies, documentation, and release process support its functionality and
production-readiness claims. Prior reports were treated as hypotheses and
rechecked against the current tree. This review is read-only except for this note.

## Executive Verdict

The source-level quality gates are unusually strong for a pre-GA library: focused
contract/sentry tests, ruff, and mypy are green; config and route failures generally
use typed, coded errors; compatibility and methodology sentries exist. The release
boundary is materially weaker than the runtime boundary. A local sdist is
non-hermetic and captures a nested agent worktree and test caches; no CI release
artifact gate or publish workflow exists to catch it. Evidence version stamps are
caller-controlled and currently stale. Supported Python versions, optional extras,
and built artifacts are not tested as the public metadata claims. Performance CI
also omits eight benchmark-marked job/OOM gates while its one collected benchmark
has no regression assertion.

Do not publish an sdist or call the current evidence/release process production
ready until F1-F4 are closed.

## Findings

### F1 - BLOCKER - The sdist captures local worktrees, caches, and review material

**Evidence**

- `pyproject.toml:226-227` restricts the wheel to `src/decoy_engine`, but defines
  no sdist include/exclude policy.
- `.gitignore:1-30` does not exclude `.claude/worktrees/` or `.hypothesis/`.
- A successful `uv build` at the initial snapshot produced a 1.1 MB wheel and a
  **49 MB sdist**. The sdist contained 1,279 paths under
  `.claude/worktrees/agent-a376dbe54727f4579/`, 1,152 paths matching
  `.hypothesis/` (root plus nested worktree), and two untracked review paths. It
  therefore included a second repository snapshot, hooks/settings, CI files,
  caches, and local review state.
- Relevant Hatch configuration is unchanged between the artifact snapshot and
  settled HEAD; the current user-owned diff changes pandas constraints and mypy
  overrides, not build inclusion.

**Reproduction**

```bash
uv build --out-dir /tmp/decoy-engine-review-dist
ls -lh /tmp/decoy-engine-review-dist/*
tar -tzf /tmp/decoy-engine-review-dist/decoy_engine-0.3.0.tar.gz \
  | awk -F/ 'NR>1 {c[$2]++} END {for (k in c) print c[k], k}' | sort -nr
tar -tzf /tmp/decoy-engine-review-dist/decoy_engine-0.3.0.tar.gz \
  | rg -c '/\.claude/worktrees/'
tar -tzf /tmp/decoy-engine-review-dist/decoy_engine-0.3.0.tar.gz \
  | rg -c '/\.hypothesis/'
```

**Impact:** A manual source release is non-reproducible, needlessly large, and can
publish local-only or sensitive development material. Because no artifact workflow
exists, the failure is invisible to the current CI suite.

**Remediation:** Define an explicit Hatch sdist allowlist (package source, required
build metadata, license/readme, and intentionally shipped docs only), exclude all
agent/cache/build directories, and ignore `.claude/worktrees/` and `.hypothesis/`.
Build releases only from a clean CI checkout.

**Verification gate:** On every PR, build wheel and sdist, run `twine check`, install
the wheel and the sdist-built wheel in fresh environments, and assert archive members
against an allowlist. Explicitly reject `.git`, `.claude`, `.hypothesis`, review notes,
`.env`, caches, and nested copies of the repository.

### F2 - HIGH - Evidence and audit version provenance is caller-controlled and stale

**Evidence**

- Package version is `0.3.0` in `pyproject.toml:10` and
  `src/decoy_engine/__init__.py:262`.
- `run_pipeline()` requires an arbitrary caller-supplied `engine_version`
  (`src/decoy_engine/execution/_pipeline.py:153-180`) and forwards it into the
  compiled audit plan at line 315 without checking it against the installed
  distribution.
- The canonical README passes `engine_version="0.1.0"` (`README.md:49-56`).
- The proof generator hard-codes `ENGINE_VERSION = "0.2.0"`
  (`scripts/gen_proof_manifest.py:54-59`) and stamps it into every real current-code
  run (`scripts/gen_proof_manifest.py:1188-1197`). The committed manifest repeats
  `0.2.0` (`docs/proof-manifest.json:2`).
- `tests/sentry/test_proof_manifest.py:136-143` compares the manifest to the same
  stale generator, so the sentry is green while the provenance is false.
- `tests/unit/test_public_api.py:207-208` checks only that `__version__` is a string;
  no gate ties it to project metadata, proof artifacts, tags, or changelog.

**Impact:** Audit/evidence output can attribute results to the wrong engine version,
whether by stale sample code or any caller-supplied value. That breaks incident
reproduction and weakens the evidentiary value of manifests intended for downstream
marketing/compliance use.

**Remediation:** Make the installed distribution version the engine-owned source of
truth (`importlib.metadata.version("decoy-engine")`, with one source-tree fallback).
Remove the public override or restrict it to a clearly test-only injection. Generate
proof metadata from that source and require pyproject, runtime, changelog/release tag,
and proof manifest versions to agree.

**Verification gate:** Build/install the wheel, run one pipeline, and assert every
version stamp equals wheel metadata. Mutating a caller-provided version must either be
impossible or fail. Add a sentry that deliberately changes one version source and
proves the gate turns red.

### F3 - HIGH - CI does not test the supported compatibility contract reproducibly

**Evidence**

- Public metadata claims Python 3.10, 3.11, and 3.12 support
  (`pyproject.toml:18-38`; `README.md:19-20`).
- Correctness, typing, parity, substrate, security, regression-proof, and test-flight
  workflows all use Python 3.10; docs/benchmarks use 3.11. No workflow uses 3.12.
  The primary examples are `.github/workflows/ci.yml:32-35,51-54,87-90`.
- Nearly all workflows install broad dependency ranges with
  `pip install -e ".[dev]"` (`ci.yml:55-64,91-107` and the parity/benchmark
  workflows). They ignore the committed hashed `uv.lock`; only test-flight uses
  `uv sync`, and it omits `--frozen` (`testflight.yml:45-54`).
- The user-owned in-flight hardening narrows pandas after pandas 3 resolved on
  Python 3.11+ and changed golden fingerprints. That is direct evidence that the
  current floating CI environment can change determinism without a source change.
- No minimum-supported-dependency job and no latest-dependency compatibility job
  exist. Tests run editable source, not an installed wheel/sdist.

**Impact:** A green build proves one Python/runtime resolution, not the advertised
support window or the artifact users install. Dependency releases can silently alter
goldens, collection, or behavior; Python 3.12 incompatibility can ship undetected.

**Remediation:** Use a frozen, reviewed environment for determinism/golden gates;
add Python 3.10/3.11/3.12 wheel-install smoke/integration jobs; separately run
minimum and latest allowed dependencies so library ranges remain honest. Audit the
resolved release lock/artifact, not a fresh unrelated resolution. GitHub documents a
Python-version matrix as the standard workflow pattern:
https://docs.github.com/en/actions/tutorials/build-and-test-code/python

**Verification gate:** A matrix installs the built wheel on every claimed Python,
runs public API/golden/compat tests, and `pip check`s it. Frozen and latest/minimum
lanes have distinct purposes and names.

### F4 - HIGH - Performance/OOM gates have no CI owner; the benchmark job cannot detect its stated regression

**Evidence**

- Default tests exclude `benchmark` (`pyproject.toml:229-244`;
  `.github/workflows/ci.yml:106-107`).
- `pytest --collect-only -q tests -m benchmark` collects 13 tests. Eight live under
  `tests/perf`: six job-level gates at
  `tests/perf/test_job_performance_gates.py:200,302,387,474,521,645` and two OOM
  capability gates at `tests/perf/test_out_of_core_memory_sentinel.py:162,204`.
- The benchmark workflow runs only `tests/benchmark/`
  (`.github/workflows/benchmark.yml:79-95`), so those eight tests run in neither
  default CI nor the dedicated benchmark workflow.
- The only collected benchmark module in that workflow records one timing per path
  and checks row/column shape only (`tests/benchmark/test_arrow_to_pandas_conversion.py:62-101`).
  Its summary explicitly "Always passes" (`:104-109`). This cannot enforce the
  workflow's claimed 4%-to-30% catastrophic-regression guard
  (`benchmark.yml:1-11`).
- Benchmark jobs run on `ubuntu-latest` but describe the image/hardware as pinned
  and free from machine variance (`benchmark.yml:1-3,137-142`). It is neither a
  pinned image label nor dedicated hardware.

**Impact:** The strongest public-entrypoint throughput, memory, and OOM capability
assertions are dead in automation. The visible benchmark status can remain green
after a material regression.

**Remediation:** Give benchmark-marked tests one explicit workflow owner (for example,
`pytest tests -m benchmark`), split informational measurements from enforceable
performance gates, and calibrate hard gates on a pinned/dedicated runner. Store raw
JSON, environment identity, repetitions/distribution, commit SHA, and comparison
baseline; do not call a single noisy timing a gate.

**Verification gate:** Add a meta-test that every registered marker is selected by at
least one workflow. Plant an intentionally impossible threshold and prove the
benchmark workflow fails.

### F5 - MEDIUM - The wheel claims inline typing but does not ship `py.typed`

**Evidence**

- `Typing :: Typed` is advertised at `pyproject.toml:38`.
- `find src/decoy_engine -name py.typed` returns zero files.
- The successfully built wheel contains no `decoy_engine/py.typed` entry.

**Impact:** Downstream type checkers treat the installed distribution as untyped,
despite the classifier and extensive annotations. Source-tree mypy does not catch
the consumer failure.

**Remediation:** Add/package `src/decoy_engine/py.typed` and decide whether the whole
public package is type-complete. PEP 561 requires the marker for packages publishing
inline types: https://peps.python.org/pep-0561/.

**Verification gate:** Install the wheel in a clean environment and type-check a
small external consumer with `--disallow-any-unimported`; assert the marker is in
the wheel.

### F6 - MEDIUM - Polars capability and telemetry misclassify pandas ports as native

**Evidence**

- `PandasStrategyPort.run()` converts a Polars column to pandas and back for every
  invocation (`src/decoy_engine/execution/polars/_strategies/_pandas_port.py:26-47`).
- Seven strategies are wrapped this way
  (`execution/polars/_strategies/__init__.py:63-89`).
- `_POLARS_NATIVE_STRATEGIES` is nevertheless defined as every registry key and
  `supports_strategy()` returns true for all (`_polars_adapter.py:70-98`).
- The Polars loop stamps every such strategy as `"polars"`
  (`_polars_adapter.py:230-239`).
- The generated authoritative matrix labels these strategies
  `Polars-accelerated: yes` (`docs/capability-matrix.md:12-34`), and its sentry only
  verifies generator equality (`tests/sentry/test_capability_matrix.py:35-43`).
- Prior consultant F5 identified this; it remains explicitly backlogged in
  `docs/plans/2026-07-07-next-up-roadmap.md:468`.

**Impact:** Admission and performance analysis cannot distinguish true Polars
execution from per-column pandas round trips. Telemetry is factually wrong and the
conversion can multiply memory/copy cost on wide jobs.

**Remediation:** Use a machine-readable enum per strategy: `polars_native`,
`pandas_port`, `pandas_job_fallback`, or `unsupported`. Drive `supports_strategy`,
capability docs, routing, and telemetry from it; report mixed execution accurately.

**Verification gate:** One test per classification, plus a pandas-port test that
asserts telemetry says `pandas_port` and that performance claims never treat it as
native.

### F7 - MEDIUM - Top-level public API documentation names an export that does not exist

**Evidence**

- `src/decoy_engine/__init__.py:5-20` and `README.md:66-82` define `__all__` as the
  contract and list `PolarsExecutionAdapter` among its public pieces.
- `PolarsExecutionAdapter` is neither imported nor present in top-level `__all__`
  (`src/decoy_engine/__init__.py:264-398`); runtime evidence:
  `hasattr(decoy_engine, "PolarsExecutionAdapter") == False`.
- The exact-set public contract test intentionally expects only
  `PandasExecutionAdapter` (`tests/unit/test_public_api.py:179-204`), so the test
  blesses the implementation while docs promise another surface.
- Default semantics are also split: `select_execution_adapter()` defaults Polars
  (`execution/_substrate.py:24-41`), while public `run_pipeline()` defaults pandas
  (`execution/_pipeline.py:153-180,231-240`). The docs rarely qualify which default
  they mean.

**Impact:** The documented top-level import fails, and callers can make incorrect
performance/routing assumptions.

**Remediation:** Either top-level export the adapter and freeze it, or document the
real `decoy_engine.execution.PolarsExecutionAdapter` path. Document selector and
pipeline defaults separately.

**Verification gate:** Execute every README import/example against the installed
wheel and compare documented top-level names to `__all__`.

### F8 - MEDIUM - Optional-capability tests skip instead of proving supported extras

**Evidence**

- The default `dev` extra installs pytest, moto, and Hypothesis only
  (`pyproject.toml:127-138`). CI does not install `geo`, `mimesis`, `ner`, `sftp`,
  `vault`, or `ml` except test-flight's `geo` extra.
- Tests conditionally skip/import-skip these capabilities: SFTP
  (`tests/connectors/test_sftp.py:30`), Mimesis
  (`tests/unit/providers_v2/mimesis/test_mimesis_adapter.py:16`), H3
  (`tests/unit/transforms/test_h3_geo_generalize.py`), vault crypto
  (`tests/unit/test_vault.py:41`), ML/lightgbm, and spaCy NER. The authoritative
  capability matrix still advertises SFTP and optional providers
  (`docs/capability-matrix.md:43-91`).
- Current collection reported four module-level skips before selection; the focused
  contract run reported one skip.
- Coverage configuration exists (`pyproject.toml:773-789`), but neither `coverage`
  nor `pytest-cov` is a dev dependency or CI gate; `.venv/bin/coverage` was absent.

**Impact:** Green CI can mean "not installed," not "supported," for advertised
extras. There is no measured branch/line coverage to reveal silent blind spots.

**Remediation:** Add an extras matrix with hard import/behavior tests and zero
unexpected skips per lane. Pin the spaCy model separately. Add coverage tooling and
ratchet branch coverage on risk-heavy modules; use coverage as a blind-spot signal,
not a quality target.

**Verification gate:** `pytest -ra` output is parsed against a reviewed skip
allowlist; each advertised extra has a required lane. Coverage reports are archived
and cannot decrease without an explicit review note.

### F9 - LOW - Release and operator documentation contains stale state

**Evidence**

- `SECURITY.md:38` says the engine is `0.1.0`; package metadata is `0.3.0`.
- `README.md:56` also stamps the canonical run as `0.1.0`.
- `CODEMAP.md:52,139` still calls out-of-core opt-in/not auto-routed, while
  `run_pipeline` routes it in auto mode (`execution/_pipeline.py:205-222,328-357`).
- `CODEMAP.md:162` says `docs/ml-benchmarking-and-privacy.md` is missing; the file
  exists.

**Impact:** Onboarding, incident triage, and architecture navigation start from
contradictory facts.

**Remediation:** Generate stable facts (version, exports, route matrix, file
existence) and keep narrative commentary separate. Run link/path/example checks in
docs CI.

**Verification gate:** A docs sentry checks referenced paths, versions, public
imports, and route/capability tables against code.

### F10 - LOW - Release mechanics are absent despite PyPI/release language

**Evidence**

- `CHANGELOG.md:1-10` describes a PyPI distribution and `README.md:15-17` directs
  users to `pip install decoy-engine`.
- No workflow builds/tests distribution artifacts or publishes a release; searching
  `.github/workflows` finds no PyPI/publish/release job.
- The repository has no SemVer-style `v*` release tags. Project/runtime versions are
  duplicated manually (`pyproject.toml:10`, `__init__.py:262`).

**Impact:** There is no auditable path from reviewed commit to immutable user
artifact, and F1/F2 have no final containment boundary.

**Remediation:** Add a tag-triggered, environment-approved Trusted Publishing flow
that builds once from a clean checkout, verifies artifacts, generates provenance,
and publishes the exact tested bytes. PyPA's current official pattern uses tagged
commits and Trusted Publishing:
https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/.

**Verification gate:** TestPyPI dry run, tag/version/changelog equality, installed
artifact smoke tests, archive allowlist, SHA-256 checks, and hosted attestation.

## Notable Strengths

- Focused public-contract/sentry/release/substrate tests passed:
  `1543 passed, 1 skipped in 79.27s`.
- `ruff check` and `ruff format --check` passed (`761 files already formatted`).
- Current user-owned stricter mypy configuration passed:
  `Success: no issues found in 375 source files`.
- Public `__all__` is exact-set tested; GA stub behavior, mypy override targets,
  deprecation lifetimes, capability drift, source hygiene, methodology citations,
  and proof regeneration all have sentries.
- Package build itself succeeds and the wheel contains required YAML, Lark grammar,
  Parquet corpora, license, and notice data.
- Runtime route/config validation generally fails early with stable error codes;
  default tests retain parity, privacy, security, compatibility, and property suites.

## Prior-Finding Reconciliation

- **Consultant F1/F2 (eager profiling/reject after read): materially remediated.**
  SC7a/SC7b bounded profiling and profile-derived route admission are present.
  `CHANGELOG.md:26` still correctly says the end-to-end SC7c memory-cap proof has
  not landed, so production boundedness remains a coverage limit rather than the
  original eager-read defect.
- **Consultant F4 (public stubs): mitigated, not implemented.**
  `tests/sentry/test_ga_stub_exports.py:44-74` blocks flipping to GA while exact
  fake behaviors remain. Stubs are still real pre-GA limitations.
- **Consultant F8 (dangling mypy overrides): remediated.**
  `tests/sentry/test_mypy_override_targets.py:49-65` parses every override and the
  current test passes.
- **Consultant F5 (Polars is not fully native): still live.** See F6 above.
- **Prior "coverage unavailable" note: still live.** Configuration exists, tooling
  and CI enforcement do not.

## Verification Record and Coverage Limits

Observed commands on the settled tree unless noted:

```text
.venv/bin/pytest -q tests/sentry tests/unit/test_public_api.py \
  tests/unit/test_release_phase.py tests/unit/execution/test_polars_adapter.py \
  tests/unit/execution/test_run_pipeline_substrate.py
=> 1543 passed, 1 skipped in 79.27s

.venv/bin/ruff check src tests testflight scripts
.venv/bin/ruff format --check src tests testflight scripts
=> pass; 761 files formatted

uv run --frozen --extra lint mypy src/decoy_engine testflight
=> Success: no issues found in 375 source files

.venv/bin/pytest --collect-only -q tests -m benchmark
=> 13/6326 tests collected; 8 are outside tests/benchmark

uv build --out-dir /tmp/decoy-engine-review-dist  # initial artifact snapshot
=> wheel and sdist built; wheel 1.1 MB, sdist 49 MB
```

The full settled non-benchmark suite was started but stopped at roughly 5% to meet
the review handoff deadline; no full-suite pass is claimed. An earlier run became
invalid when the shared worktree changed revisions mid-run and was discarded.
Python 3.11/3.12, Windows/macOS, optional-extra lanes, installed-wheel consumer
typing, live cloud services, long benchmarks, TestPyPI publishing, and end-to-end
SC7c memory caps were not executed. A second build at settled HEAD was blocked by
tool-approval usage limits, not by repository code; F1's relevant build config did
not change between the successful artifact snapshot and settled HEAD.

## Recommended Order

1. Hermetic artifact allowlist + clean build/install CI (F1).
2. Engine-owned version/proof provenance + version consistency (F2).
3. Frozen proof lane, Python/artifact matrix, and release workflow (F3/F10).
4. Give every benchmark/OOM test a real CI owner (F4).
5. Correct `py.typed`, Polars classification/telemetry, and public imports (F5-F7).
6. Add extras/coverage lanes and regenerate stale docs (F8-F9).
