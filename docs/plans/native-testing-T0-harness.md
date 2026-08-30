Status: record

# T0 harness pilot: native-efficiency cross-language coverage + mutation

- **Plan:** `docs/plans/2026-08-29-native-efficiency-test-plan.md`, batch T0
  (section 4) and the method in section 3.
- **Scope:** prove the harness, not grade the surface. Every number below is a
  pilot on a small slice; T1-T6 run the same commands at full scope.
- **Branch:** `docs/native-efficiency-test-plan`, worktree
  `.claude/worktrees/native-test-plan`.
- **Touched:** `scripts/native-testing/` (new) and this report only. No
  production code or existing test changed.

## Tool versions (bootstrap)

```
source ~/.cargo/env
cargo --version                 # cargo 1.98.0 (797e8a9bc 2026-08-05)
rustc --version                 # rustc 1.98.0 (88d9e12ae 2026-08-18)
cargo llvm-cov --version        # cargo-llvm-cov 0.9.0
cargo mutants --version         # cargo-mutants 27.1.0
.venv/bin/python -m mutmut version    # mutmut 3.6.0
.venv/bin/python -m coverage --version # coverage 7.15.2
```

`rustup component add llvm-tools-preview` was already installed per the brief.
Engine venv: `/home/cam/vscode/decoy-engine/.venv` (shared across the stacked
`native-*` worktrees; see the Lane B section for why that matters).

## Lane A: Rust-only coverage + mutation

Runner: `scripts/native-testing/lane_a_rust_coverage_mutants.sh`.

### Coverage baseline

```
scripts/native-testing/lane_a_rust_coverage_mutants.sh coverage
# == cd decoy-engine-native && cargo llvm-cov --show-missing-lines
```

| File              | Region cover | Line cover | Function cover |
|-------------------|-------------:|-----------:|----------------:|
| derive.rs         |       96.08% |     93.49% |          86.36% |
| canonicalize.rs   |       92.24% |     94.35% |          87.50% |
| batch.rs          |       95.69% |     91.92% |          91.67% |
| arrow_ffi.rs      |       49.44% |     45.82% |          36.84% |
| lib.rs            |        0.00% |      0.00% |           0.00% |

arrow_ffi.rs and lib.rs are low because most of their code only runs behind a
live Python call into the compiled extension (`abi_version`, `derive_batch`,
`export_string_array`, `extract_truncate`, module registration): a plain
`cargo test` never reaches it. That gap is what Lane B measures.

### Mutation pilot

```
scripts/native-testing/lane_a_rust_coverage_mutants.sh mutants-pilot
```

**Pilot 1: `batch.rs` (11 mutants, ~45s):**

```
Found 11 mutants to test
ok       Unmutated baseline in 21s build + 2s test
MISSED   src/batch.rs:79:20: replace match guard !k.is_empty() with true in derive_array in 3s build + 3s test
11 mutants tested in 47s: 1 missed, 7 caught, 3 unviable
```

- **Caught (killed), example:** `src/batch.rs:46:9: replace BatchError::code ->
  &str with ""`, caught by the existing `seed_wrong_length`/`namespace_empty`
  code-matching tests.
- **Missed (survived), real gap:** the match guard
  ```rust
  let mask_key = match mask_key {
      Some(k) if !k.is_empty() => k,
      _ => return Err(BatchError::MaskKeyRequired),
  };
  ```
  mutated to `if true` still passes every existing test, because no test
  calls `derive_array` with `Some(&[])` (a present-but-empty mask key), only
  `None` and non-empty keys are covered. `MaskKeyRequired`'s fail-before-output
  contract is untested for the empty-but-`Some` case. This is a genuine gap,
  not equivalent or unreachable; T1 should add it under crypto's
  zero-unadjudicated-survivor bar.
- **Unviable (3):** `Default::default()` replacement bodies for
  `BatchError`/`CanonError`/`DeriveError`, none of which implement `Default`.
  cargo-mutants reports these as build failures, not a killed/survived split:
  correct and expected, not a harness problem.

**Pilot 2: `import_ffi` only, `arrow_ffi.rs` (the PyO3-free path):**

```
cargo mutants -f 'src/arrow_ffi.rs' -F 'import_ffi' --timeout 120 -j 2
Found 1 mutant to test
ok       Unmutated baseline in 18s build + 2s test
1 mutant tested in 21s: 1 unviable
```

The one mutant cargo-mutants can generate for `import_ffi`
(`Ok(Default::default())`, since the function's only mutable shape is its
return value) doesn't compile: `ArrayRef` has no `Default`. So this
particular function yields no killed/survived signal from cargo-mutants'
default operators; its three hand-written unit tests (unadmitted-type,
null-buffer, implausible-length) already prove the behavior directly. This is
a legitimate result: cargo-mutants runs, applies the same `-f`/`-F` scoping,
and terminates cleanly with zero attempts at the PyO3-bound functions. It is
just that this one function has no viable mutant to show for it. Pilot 1
supplies the actual killed-vs-survived proof.

### Confirming the scoping keeps cargo-mutants off the PyO3 layer

Unscoped, `cargo mutants --list -f 'src/arrow_ffi.rs'` finds 58 mutants, most
on `export_string_array`, `extract_truncate`, `derive_batch_checked`,
`derive_batch`, and `register`, functions that only do anything under a live
Python interpreter, so cargo-mutants running `cargo test` against them would
either fail to build (their return types have no `Default`) or report a false
"survived" (no test in `cargo test` calls them at all). The scoping recipe
`scripts/native-testing/lane_a_rust_coverage_mutants.sh mutants` uses:

```
cargo mutants -f 'src/derive.rs' -f 'src/canonicalize.rs' -f 'src/batch.rs' \
  -f 'src/arrow_ffi.rs' \
  --exclude-re 'export_string_array|extract_truncate|derive_batch_checked|derive_batch ->|register ->'
```

narrows that down to 11 mutants on `KernelError::code`/`detail`, the `From`
impls, `to_py_err`, `import_array`, and `import_ffi`: all Rust-only. This is
the recipe T1 should extend, not re-derive.

## Lane B: instrumented extension coverage

Runner: `scripts/native-testing/lane_b_instrumented_extension.sh`.

### Why this needed two fixes before it worked

1. **Python ABI mismatch.** A plain `cargo build` lets pyo3's build script
   auto-detect the active `python3` on `PATH`, which was CPython 3.11.2 here
   while the engine venv runs 3.10.20. The resulting `.so` referenced
   `PyType_GetName` (added in 3.11) and failed to import under the venv with
   `undefined symbol`. Fix: `export PYO3_PYTHON=<venv>/bin/python` before
   `cargo build`, and touch `src/lib.rs` to force pyo3's build script to
   re-run under the new interpreter.
2. **Shared-venv shadowing without touching the shared venv.** This venv is
   shared across every stacked `native-*` worktree; its
   `decoy_engine_native.pth` currently points at
   `native-phase2-task2.3/decoy-engine-native`. Running `maturin develop` here
   would repoint that shared file at this worktree, which could break
   whichever other worktree is relying on it. Instead the instrumented `.so`
   is staged in a private scratch directory (`decoy_engine_native/_kernel*.so`
   plus a copy of `__init__.py`) and that directory goes first on
   `PYTHONPATH`, so Python resolves the package from there before ever
   reaching the venv's `.pth` entry. Verified: `git status` on the venv's
   `.pth`/dist-info was untouched before and after every Lane B run.
   - One more trap in the same vein: `python -c`/`pytest` both prepend the
     current working directory to `sys.path` ahead of `PYTHONPATH`. This
     crate's own `decoy_engine_native/` (the maturin-generated stub with no
     compiled extension) lives inside `decoy-engine-native/`, so running the
     sanity check from that directory let the stub shadow the staged,
     instrumented package. Fixed by moving to the repo root before any Python
     invocation.

### Commands

```
source ~/.cargo/env
cd decoy-engine-native
eval "$(cargo llvm-cov show-env --sh)"
export PYO3_PYTHON=<venv>/bin/python
touch src/lib.rs && cargo build       # instrumented cdylib at target/debug/lib_kernel.so

# stage it as an importable package (never touches the shared venv)
mkdir -p <scratch>/decoy_engine_native
cp target/debug/lib_kernel.so <scratch>/decoy_engine_native/_kernel.cpython-310-x86_64-linux-gnu.so
cp decoy_engine_native/__init__.py <scratch>/decoy_engine_native/__init__.py

cd <repo-root>
PYTHONPATH="<scratch>:<repo-root>/src" <venv>/bin/python -m pytest -q \
  tests/native/test_native_ext_abi.py \
  tests/native/test_keyed_derivation_kernel_parity.py \
  tests/native/test_crypto_ext_loader.py
# 74 passed, 1 skipped

cd decoy-engine-native
cargo llvm-cov report --show-missing-lines   # merges the profraw these tests just wrote
```

### Result: real instrumented-extension coverage, not the fallback

| File              | Lane A (cargo test) | Lane B (these 3 Python files) |
|-------------------|---------------------:|-------------------------------:|
| arrow_ffi.rs      |                49.44% |                         79.44% |
| lib.rs            |                 0.00% |                         80.00% (100% lines) |
| batch.rs          |                95.69% |                         65.71% |
| canonicalize.rs   |                92.24% |                         86.81% |
| derive.rs         |                96.08% |                         84.33% |

arrow_ffi.rs and lib.rs both read materially higher under Lane B, confirming
it reaches the capsule import/export path, the panic-catching boundary, and
module registration/`abi_version`: exactly what Lane A structurally cannot
reach. batch.rs/canonicalize.rs/derive.rs read lower under Lane B, which is
expected and correct: these three Python test files exercise the happy/parity
paths, not the dedicated Rust unit and property tests that already cover
those files' edge cases in Lane A. Lane B is not a replacement for Lane A on
those three files, only the PyO3-boundary supplement.

No fallback was needed. This is a real, measured instrumented build, not an
inspection-based substitute.

## Python mutation pilot

The plan calls for a standalone-pytest-per-mutant runner. One already exists:
`scripts/tq_mutate.py`, built for the earlier Test-Quality Program to solve
this exact false-timeout pathology (its docstring cites the same root cause:
mutmut forks a per-mutant child that runs `pytest.main()` in-process under a
thread/CPU-time-based timeout sized from a near-zero baseline, which
misfires on Arrow/pandas-substrate suites). It re-runs every
non-trustworthy mutmut verdict (timeout, rc-3, and by default the whole
survived bucket, since a mutmut "survived" only means its own coverage-map
subset didn't kill it) against the full test selection in a fresh subprocess
with a timeout sized off a measured baseline, and refuses to report a score
if anything stays unresolved.

The one gap it doesn't cover: `mutmut run` itself always reads
`[tool.mutmut]` from `./pyproject.toml`, with no override, and that block is
permanently pointed at `_codeset_index.py` for the existing TQ work.
`scripts/native-testing/python_mutation_pilot.py` closes that gap: it
refuses to run if `pyproject.toml` has uncommitted changes, patches only the
`[tool.mutmut]` block's three relevant keys, runs `mutmut run` then
`tq_mutate.py`, and restores `pyproject.toml` from git on a normal exit
(`finally`) AND on SIGINT/SIGTERM (a signal handler). The signal handler
matters because `finally` does NOT run when the process is signalled, and
SIGTERM is exactly what `timeout(1)` and CI time-caps send -- the realistic way
a long crypto/RI run ends. A hard SIGKILL (`kill -9`, and what the Linux
OOM-killer sends) cannot
be trapped and WILL leave the temporary block in place; that is caught, not
silently committed, because the next run refuses to start on a dirty
`pyproject.toml` and the injected block carries a loud revert-by-hand comment
(recover with `git checkout -- pyproject.toml`). Verified clean
(`git status --short pyproject.toml` empty) after a normal run, a `return 2`
early-exit, and a deliberate SIGTERM mid-run.

Target: `native_truncate` in
`src/decoy_engine/execution/native/_kernels_scalar.py`, against
`tests/native/test_kernels_scalar.py` (37 tests, 0.35-0.4s to run).

### Real run (default timeout budget)

```
.venv/bin/python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_kernels_scalar.py \
  --tests tests/native/test_kernels_scalar.py \
  --timeout 30
```

```
68 mutants: 58 trusted, 10 suspect (re-adjudicating with timeout=30.0s, jobs=1)
LOGIC score:       85.29%  (58/68)
INCLUSIVE score:   85.29%  (58/68)  [+0 unresolved]
```

- **Killed, example:** `x_native_passthrough__mutmut_1` (mutmut's raw run):
  any mutation to `native_passthrough`'s body breaks the identity-match
  assertions in `test_kernels_scalar.py`.
- **Survived, all in `native_truncate`.** The survivors are TWO distinct shapes,
  not one (a diff of the mutant bodies):
  1. **Default-argument mutants of `keep`** (a small number), e.g.
     ```diff
     -    keep: str = "head",
     +    keep: str = "XXheadXX",
     ```
     Every test passes `keep=` explicitly, so the default value is never
     exercised: a REAL, untested-default gap, not equivalent. T3 closes it with
     one test that omits `keep` (see carry-forward). This is the shape T1/T3
     should act on.
  2. **Error-MESSAGE-text mutants** (the majority), e.g. mutating the `message=`
     string, `message=None`, or `type(None)` inside the f-string of
     `native_truncate`'s fail-closed `StrategyError` raises. These are EQUIVALENT
     BY CONTRACT: the module's contract, and every test asserting it, checks
     `.code` and `.strategy`, never the message wording (matching the sibling
     `TruncateHandler` tests). Mutating the message changes no observable
     behavior, so these are adjudicated equivalent (field (c)), not "closed by a
     test". A single default-`keep` test would close NONE of them.
  tq_mutate.py re-adjudicating the survived bucket confirms both shapes are
  genuinely uncaught (a full-selection re-run reproduces the same survivors), not
  a coverage-map artifact.

### Forcing and correcting the false timeout

This module is light enough (sub-second full-selection run) that it doesn't
spontaneously trigger mutmut's in-process misfire. To demonstrate the
pathology and its correction on demand rather than leaving it unshown, the
wrapper's `--force-false-timeout` flag sets `timeout_constant =
timeout_multiplier = 0`, which makes any real execution exceed the budget:

```
.venv/bin/python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/native/_kernels_scalar.py \
  --tests tests/native/test_kernels_scalar.py \
  --timeout 30 --force-false-timeout
```

```
mutmut raw:        {'timeout': 68}          # every mutant, falsely
  [   timeout -> killed      ] rc=1   2.1s  ...x_native_truncate__mutmut_26
  [   timeout -> survived    ] rc=0   2.1s  ...x_native_truncate__mutmut_28
  ...
LOGIC score:       85.29%  (58/68)
INCLUSIVE score:   85.29%  (58/68)  [+0 unresolved]
```

mutmut's raw run reports all 68 mutants as `timeout` (a complete misfire).
`tq_mutate.py`'s readjudication step re-runs each one standalone against the
full selection with a generous, baseline-derived timeout, and recovers the
exact same tally as the honest default-budget run above: 58 killed, 10
survived, 0 unresolved. That exact match is the proof the "timeout" verdicts
were false, not real: a genuinely slow or hanging mutant would not resolve to
the same numbers on rerun.

## Acceptance against the plan's T0 gate

> Gate T0 on: the harness demonstrably distinguishes a killed from a
> surviving mutant in BOTH languages, AND the known Python false-timeout is
> reproduced and correctly classified via a standalone rerun, all with
> recorded commands.

- Rust killed vs. survived: batch.rs pilot (7 caught, 1 missed), commands
  above. Met.
- Python killed vs. survived: `_kernels_scalar.py` pilot (58 killed, 10
  survived), commands above. Met.
- False-timeout reproduced and correctly classified via standalone rerun:
  forced-timeout run above, corrected by `tq_mutate.py`'s per-mutant
  subprocess rerun to the exact honest tally. Met.
- Lane B instrumented-extension coverage of the PyO3 boundary: achieved for
  real (arrow_ffi.rs 49.44% to 79.44%, lib.rs 0% to 80%/100% lines); no
  fallback needed.

## Carried forward for T1-T6

- **`derive.rs`/`build_frame`'s length-overflow branches** (lines 96-98 and
  99-104, the `u32::try_from(...).map_err(...)` arms) are uncovered by both
  Lane A coverage and cargo-mutants (no realistic byte slice/string reaches
  a >4GiB length, and cargo-mutants generates no operator-level mutant inside
  those `map_err` closures, only a whole-function replacement, which the
  KAT/HMAC tests kill immediately regardless). Per the plan, this is NOT an
  unreachable-by-contract adjudication; T1 must extract a checked-length
  helper taking a synthetic size and assert both branches directly. Not done
  here: T0 does not touch production code.
- **The `MaskKeyRequired`-with-empty-`Some`-key gap** in `batch.rs::derive_array`
  found by the pilot is a real, demonstrated crypto-surface gap under the
  zero-unadjudicated-survivor bar; T1 should add the missing test rather than
  re-discover this finding.
- **cargo-mutants' default operators produce no viable mutant for
  `import_ffi`** in isolation (its only mutable shape needs a `Default` the
  return type doesn't have). T1's Lane A should rely on the
  `--exclude-re`-based four-file scoping (proven above) rather than trying to
  isolate `import_ffi` alone again.
- **`native_truncate`'s survivors are two shapes, handled differently.** The
  default-`keep` mutant(s) are a real untested-default gap T3 closes with ONE
  test that omits `keep`. The error-message-text mutants are equivalent by
  contract (the tests assert `.code`/`.strategy`, never the message) and are
  adjudicated equivalent, not closed by a test. T3 must not chase the
  message-text mutants with tests.
- **Mutation-tally trust (crypto/RI bar).** mutmut's in-process runner can
  FLAKILY report a genuine survivor as killed, and tq_mutate.py trusts killed as
  monotonic -- so a flaky-kill would UNDER-count survivors, the dangerous
  direction for the zero-unadjudicated-survivor bar. The wrapper now sets
  `PYTHONHASHSEED=0` in its OWN process environment before spawning anything, so
  EVERY adjudication child inherits it -- `mutmut run`, the `tq_mutate.py`
  subprocess, and the killed-bucket re-adjudication (whose pytest children copy
  os.environ). Seeding only the initial run would leave the re-adjudication
  unseeded, letting a seed-0 survivor flip to killed on rerun and vanish. There
  is no pytest test-order randomizer installed, so the hash seed is the only
  nondeterminism source. Its `--readjudicate-killed` mode re-runs the killed
  bucket standalone and fails on any flaky-kill. T1-T2
  (the crypto/RI surfaces) MUST run with `--readjudicate-killed`. Verified on
  this module: the tally is reproducible run-to-run and all 58 killed mutants
  re-confirm killed standalone (0 flaky-kills).
- No blocker found for T1-T6: both Rust lanes and the Python standalone
  runner are proven, scoped, and documented with exact commands above.
