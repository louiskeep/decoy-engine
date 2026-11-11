Status: plan

# GEN-TYPES E1b: portable sampling for the deferred `distribution` families and the fitted histogram draw (roadmap row 24q)

Owner: Cam. Author: Opus plan agent (claudeworkerbee, task T0548). Date: 2026-11-11. Revision 1.
Scope: engine only. Risk R2 (the cross-machine byte promise in `docs/determinism.md`, a new runtime
reason code, new public config values). Approver: Cam at plan approval.

Rules consulted: `dev-rules/README.md` (routing), `00-universal.md`, `risk-and-exceptions.md`,
`development-loop.md` (Proportion; PLAN: guarantee, design notes, choke point, Placement,
Established solutions), `feature-dev.md`, `reuse-first.md`, `testing.md` (Test depth, full-suite
triggers, Hypothesis for batching invariants), `architecture.md` (layout, fan-out),
`cross-cutting-concerns.md` (choke point, guard walk, seeded bypass), `api-and-compatibility.md`
(config surface, reason catalog), `performance.md`, `catalogs/recurring-defects.md` (classes 3, 5,
9, 11); engine `CLAUDE.md` (survey established methods, cite the source in the module docstring).

Builds on: the GEN-TYPES plan (`plan/gen-types`, `docs/plans/2026-11-10-gen-types.md`, section
"Deferred to E1b"), E1 (engine #467, `45533490`), E2 (engine #477, `82d00f4e`). Plan base:
engine `origin/main` `6e2e57bd`.

## 1. Frame

### Problem

E1 shipped `distribution` with `family: uniform` only. The six other families in the GEN-TYPES
table (`normal`, `lognormal`, `exponential`, `poisson`, `beta`, `zipf`) were refused at compile
because numpy's samplers for them call platform libm (`exp`, `log`, `pow`, the ziggurat tails,
the gamma and poisson samplers). The results differ in the last bit between arm64 and x86-64 for
the same seed, which breaks the promise in `docs/determinism.md` ("The same master key plus the
same key label always yield bitwise-identical generated output across runs and machines"). The
owner's standing answer (2026-11-10): drop a family that is not portable rather than add a
per-architecture caveat. E1b makes the six families portable so they can ship.

While measuring for this plan, a second site broke the same promise. `decoy fit` snapshots feed the
histogram sampler in `generators/_distribution.py:166`, which calls
`rng.uniform(bin_lo, bin_hi)`. numpy computes that in C as `low + (high - low) * u`, and the
arm64 build fuses it into one FMA. A 2M-draw probe (section 3) gives different bytes on arm64 and
x86-64 for the same seed, under all three locked numpy versions. That is the fit-side sampling the
task names, and it gets its own small slice (E1b-0).

### Scope note: fan-out sampling

The task text says "fit's per-key fan-out sampling". The fan-out sampler
(`generation/statistical/_fanout.py`, fk-fanout plan) is already portable: its allocator is
integer arithmetic, and its only draws are two `random.Random.shuffle` calls. Those use
`getrandbits` (MT19937, integer only), so the order is the same on every architecture. Its one
dependency is the Python version (CPython does not promise `shuffle` stays the same across
versions), and the pinned runtime identity already records the Python minor
(`generation/_pinned_plan.py:182`). E1b changes nothing in fan-out. The non-portable fit-side
draw is the histogram `rng.uniform` above.

### Definition of done

- All six families are accepted by validation, `compile_plan` and `run_config_only_checks`, and
  generate on the resident, streamed, legacy and pinned routes.
- Each family's 2,000,000-draw bit digest is the same on macOS arm64 and Linux x86-64, under each
  locked numpy (2.2.6, 2.4.6, 2.5.0), and is pinned in the test.
- `uniform` bytes do not move. The histogram sampler's Linux x86-64 bytes do not move, and its
  arm64 bytes now equal them.
- A guard fails when a draw path reaches libm, a numpy distribution method, an order-dependent
  float reduction, an unstable sort or the `**` operator.

## 2. Guarantee and non-goals

**Guarantee.** For every `distribution` family and for the fitted histogram draw, the generated
column is a pure function of (seed or master key, table and column identity, column config). Its
bytes are the same on Linux x86-64 and macOS arm64 under every numpy version in `uv.lock`, and do
not depend on batch sizes. Every value is finite. A config whose parameters could produce an
infinite or NaN value is refused at validation with `generate_option_range`, and the refusal
names the key, never a value.

**Non-goals** (findings here become follow-ups, not blockers):

- Linux arm64 and Windows x86-64 are not tested (no runner). The design uses only operations
  IEEE 754 defines exactly, so they should match. Section 9 lists this as residual risk.
- Other draw sites outside `distribution` and the fitted histogram. The census found one: the
  formula `gauss` helper (`generators/_formula.py:325`, Python `random.gauss`, which calls libm).
  It is follow-up F-1.
- Making the families partitionable (counter-based per-row streams). Recorded as F-2.
- Truncation by resampling. `min` and `max` still clip, as in E1.
- Bit equality with numpy's own samplers. The families follow numpy's distributions, not its
  streams.

## 3. Measured evidence (probe, 2026-11-11)

Probe script `arch_probe.py` (this task's scratch): 2,000,000 draws per call, seeded
`default_rng`, SHA-256 of the float64 bytes (first 16 hex digits). Run on the Mac mini (macOS
arm64) and `decoy-test-6` (Linux x86-64), each under numpy 2.2.6 (Python 3.10), 2.4.6 (3.11) and
2.5.0 (3.12).

| Call | arm64 (all three numpy) | x86-64 (all three numpy) | Portable |
|---|---|---|---|
| `rng.uniform(lo_array, hi_array)` (the histogram site) | `e918168eab3858d7` | `c221c9e6b83262d7` | no |
| `rng.uniform(-3.7, 11.9, n)` | `4d33fe3ec5d88116` | `429e7eed95b4a475` | no |
| `rng.choice(57, n, p=p)` | `50a9dccdb8130693` | `50a9dccdb8130693` | yes |
| `rng.normal(size=n)` | `87ab2a69ea5426d7` | `28085af36fb30b04` | no |
| `np.log(u)` | `0437763f4ef4c707` | `a6d4049073c703d0` | no |
| `np.exp(x)` | `781c51bf68d55e8d` | `19a025c89ec83eaa` | no |
| `np.sum(u)` | 2.2.6: `...ade69p+19`; 2.4.6 and 2.5.0: `...ade6ep+19` | same split by numpy version | no (moves with numpy) |
| `np.cumsum(u)[-1]` | `...adc4cp+19` | `...adc4cp+19` | yes |

Also probed on arm64 under numpy 2.2.6: `Generator(PCG64(s)).random(n)` equals
`(PCG64(s).random_raw(n) >> 11) * 2**-53` exactly, and draws of 3 then 5 concatenate to one draw
of 8. So `random()` is the raw PCG64 stream with an exact conversion. E1 relies on this.

The `np.sum` row matters for design: numpy's pairwise summation changed between 2.2 and 2.4, so
the same values sum to different bits depending on the installed numpy, even on one machine.

## 4. Established solutions

Searches run (2026-11-11), with what each found:

1. numpy RNG policy. NEP 19 and the numpy "Compatibility policy" page: stream compatibility is
   promised only for the same build, environment and machine. `Generator` distribution methods
   may change between releases. BitGenerators carry the stronger guarantee
   (https://numpy.org/neps/nep-0019-rng-policy.html,
   https://numpy.org/devdocs/reference/random/compatibility.html). Consequence: build every family
   from the bit stream (`random()`), never from a `Generator` distribution method.
2. Reproducible elementary functions. Java's `StrictMath` gets bit-identical results on every
   platform by requiring the fdlibm algorithms for `exp`, `log`, `pow` and the others, while
   `Math` may differ in the last bit (OpenJDK JDK-8308777 and JDK-8375059; Apache HIVE-23133
   reports `Math.log`/`exp` differences between ARM and x86, fixed by `StrictMath`). fdlibm's
   algorithms use only IEEE basic operations, so a faithful port is deterministic wherever those
   operations are correctly rounded. **The design follows this pattern.**
3. Correctly rounded libm (CORE-MATH, LLVM libc): also portable, but it needs double-double
   arithmetic and larger tables, and is slower to vectorize in numpy. Not needed: the promise is
   determinism, not correct rounding.
4. Why numpy's own `exp`/`log` differ: numpy dispatches them to SIMD kernels per CPU (the AVX-512
   SVML port, documented at up to 4 ULP), and macOS arm64 uses its own kernels (numpy issue
   #20356 and the numpy-discussion thread on the SVML merge). Section 3 confirms this.
5. FMA contraction: GCC's default `-ffp-contract=fast` and clang's `-ffp-contract=on` both fuse
   `a + b*c` within an expression on aarch64, where FMA is baseline (GCC list thread "Concerns
   regarding the -ffp-contract=fast default", 2023; GCC PR 106440 on an M1). Separate numpy ufuncs
   cannot be fused, because each ufunc loop does one operation. Python scalar floats are not fused
   either, because CPython evaluates each bytecode op on its own.
6. Normal quantile: Wichura, Algorithm AS 241 (PPND16), Applied Statistics 37 (1988). Accurate to
   about 16 digits in the centre and fully accurate on the probability scale down to the smallest
   doubles (Mächler, R `DPQ` vignette "qnorm-asymp"). R's `qnorm` has used it since 2000. It uses
   only rational functions, `log` and `sqrt`.
7. Gamma variates: Marsaglia and Tsang, "A simple method for generating gamma variables", ACM
   TOMS 26(3), 2000. It uses one normal and one uniform per attempt, with a squeeze that skips the
   logs most of the time, and boosts shapes below 1 by `U^(1/a)`. numpy uses it.
8. Poisson for large means: Hörmann, "The transformed rejection method for generating Poisson
   random variables", Insurance: Mathematics and Economics 12(1), 1993 (PTRS), which numpy uses
   for `lam >= 10` together with its `random_loggam` Stirling series (numpy
   `random/src/distributions/distributions.c`).
9. Discrete inversion by table: the standard table-lookup inversion (Devroye, *Non-Uniform Random
   Variate Generation*, 1986, ch. III.2). Used here for bounded Zipf and small-mean Poisson:
   cumulative table plus binary search (`numpy.searchsorted`, exact comparisons).
10. Stable sorting: numpy's default `argsort` is not stable, and on x86 it dispatches to
    x86-simd-sort, whose tie order can differ from the scalar path. Only `kind="stable"` promises
    an order (numpy `argsort` docs; intel/x86-simd-sort README). Rule: no sort in a draw path,
    and any sort used to build a table is `kind="stable"`.
11. Counter-based RNGs (Salmon et al., "Parallel random numbers: as easy as 1, 2, 3", SC11;
    Philox) give per-row streams that are machine-independent and partitionable. Rejected for
    now: it would move E1's `uniform` bytes or add a second seeding scheme. Recorded as F-2.
12. Flush-to-zero: a shared library built with `-ffast-math` can set FTZ/DAZ for the whole
    process when loaded, which changes subnormal results ("Someone's been messing with my
    subnormals!", B. Dolan-Gavitt, 2022). The design keeps intermediates out of the subnormal
    range where it can, and checks the process once with a known-answer test (section 5.6).

Why custom code remains: no maintained Python library offers vectorized fdlibm `exp`/`log` with a
stated cross-platform bit promise. Python's `math` module and numpy both call platform code, and
`mpmath`/`decimal` are exact but far too slow for a million rows. The port is small (about 150
lines), and the algorithm is frozen: its output is part of the byte promise, so it will never be
"updated". The fdlibm notice ("Permission to use, copy, modify, and distribute this software is
freely granted, provided that this notice is preserved") is kept in the module docstring.

## 5. Behavior

### 5.1 One word source per column (unchanged from E1)

Every family draws from the column's existing `default_rng(base_int("dist"))` through
`rng.random(m)` only. No new RNG family, no change to seed derivation, no change to E1 `uniform`
bytes. `random()` is the exact conversion `(raw >> 11) * 2**-53` (section 3), so it is as portable
as the raw PCG64 stream.

### 5.2 Portable kernel

A new private module holds vectorized float64 functions built only from numpy `add`, `subtract`,
`multiply`, `divide`, `sqrt`, `floor`, comparisons, `where`, integer bit views and `cumsum`:

- `log(x)` and `exp(x)`: line-for-line ports of fdlibm 5.3 `e_log.c` and `e_exp.c` (argument
  reduction, the `Lg1..Lg7` and `P1..P5` polynomials, `ln2_hi`/`ln2_lo`, exponent handling by
  integer bit view). Branches become `where` over branches that are all computed. Each
  multiply-then-add stays two ufunc calls.
- `ndtri(p)`: AS 241 PPND16 with its published coefficients as Python float literals (Python
  parses decimal literals correctly rounded, so the constants are identical everywhere).
- `loggam(x)`: numpy's `random_loggam` series, using the kernel `log`.

### 5.3 Families

| family | keys | algorithm (words per draw) | Arrow type | range rule (`generate_option_range`) |
|---|---|---|---|---|
| uniform | `low`, `high` | unchanged (1) | float64 | unchanged |
| normal | `mean`, `std` | `mean + std * ndtri(p)` (1) | float64 | `std > 0`; `mean ± 8.5 * std` finite |
| lognormal | `mu`, `sigma` | `exp(mu + sigma * ndtri(p))` (1) | float64 | `sigma > 0`; `mu + 8.5 * sigma <= 709` |
| exponential | `mean` | `mean * -log(1 - u)` (1) | float64 | `mean > 0`; `37 * mean` finite |
| zipf | `s`, `n` | table inversion over 1..n (1) | int64 | `s > 0`; `n` an int, `1 <= n <= 1,000,000` |
| poisson | `mean` | `mean < 10`: table inversion (1); `mean >= 10`: PTRS (2 per attempt) | int64 | `0 < mean <= 1e9` |
| beta | `alpha`, `beta` | Marsaglia-Tsang gamma pair, ratio in log space (4 or more per attempt) | float64 | `0.001 <= alpha, beta <= 1e9` |

The normal grid. `ndtri` must never see 0 or 1, and the obvious `u + 2**-54` can round to 1.0 at
the top of the range (ties-to-even). So `p` is the midpoint grid `(k + 1/2) * 2**-53`, formed
exactly: for `u < 0.5`, `p = u + 2**-54` (exact, since `k < 2**52`). For `u >= 0.5` the kernel
works on the upper tail `r = (1 - u) - 2**-54`, which is exact by Sterbenz's lemma and the same
bound. The draw is symmetric, and `|z|` stays at or below about 8.3. That bound is where the 8.5
in the range rules comes from.

Exponential uses `1 - u`, which is exact and lies in `[2**-53, 1]`, so `log` never sees 0. The
largest draw is `53 * ln 2 * mean`, about `36.74 * mean`.

Table inversion (zipf, small-mean poisson). Build the table once per stream:

- weights: zipf `w_k = exp(-s * log(k))`; poisson `p_0 = exp(-mean)`, `p_k = p_{k-1} * mean / k`
  for k up to 80;
- `cdf = cumsum(w)`, which is sequential and portable (section 3);
- per draw, `x = u * cdf[-1]` and `idx = searchsorted(cdf, x, side="right")`, clamped to the last
  index. `u * cdf[-1]` can round up to `cdf[-1]`; the clamp turns that into the last value instead
  of an out-of-range index.

There is no normalising division and no `np.sum`. The zipf table is at most 8 MB (1,000,000
float64). It is reserved through the generation budget ledger as a per-stream build cost, and AT-3
counts builds (the GEN-TYPES plan's P10).

Rejection families (PTRS poisson, beta). Each attempt uses a fixed number of words, taken
row-major from `rng.random((m, k))`, so attempt `c` always reads words `[k*c, k*c + k)`. The
stream keeps a carry of accepted values. A batch of size n:

1. returns carried values first;
2. draws blocks of attempts while it needs more;
3. appends the accepted values in attempt order;
4. returns the first n and keeps the rest as carry.

No word is ever discarded, so output j is the j-th accepted attempt whatever the batch sizes.
Block size: `max(64, ceil(1.1 * need / efficiency))`. It changes only how many attempts are
computed ahead, never which value is output j.

Beta. One attempt is a pair: a gamma(alpha) attempt and a gamma(beta) attempt, each
Marsaglia-Tsang (a normal by `ndtri` plus a uniform, plus one more uniform when the shape is below
1, for the boost). The pair is accepted only when both halves are accepted. The halves are
independent, so the accepted X's are still Gamma(alpha), the accepted Y's are Gamma(beta), and the
pairs stay independent. The ratio is computed as `1 / (1 + exp(log Y - log X))`, with
`log X = log(d) + log(v)` (plus `log(u_boost) / alpha` when the shape is below 1). This stays
finite for small shapes, where `X/(X+Y)` would give `0/0`. The words per attempt are fixed per
column (4, 5 or 6, by which shapes are below 1).

PTRS poisson follows numpy's `random_poisson_ptrs` step by step: `U = u1 - 0.5`, `V = u2`, the
`us >= 0.07 and V <= vr` fast accept, the `k < 0 or (us < 0.013 and V > us)` reject, then the log
test with kernel `log` and `loggam`. The constants (`b`, `a`, `invalpha`, `vr`, `loglam`) are
computed once per column with kernel functions.

`min` and `max` clip after the draw as in E1 (`numpy.clip`, exact comparisons). An int family
clips with the bounds converted to int, with the conversion checked at validation.

### 5.4 Arrow types and output_type

`parametric_arrow_type` returns the family's type from its descriptor (int64 for poisson and zipf,
float64 for the rest). E2's `output_type` applies unchanged. `int` on an int family is the
identity.

### 5.5 Fitted histogram draw (E1b-0)

`generators/_distribution.py:166` becomes
`np.add(np.multiply(rng.random(n), bin_hi - bin_lo), bin_lo)`. The width subtraction stays one
ufunc, as numpy's `uniform` does it. numpy's x86-64 `uniform` is
`off + scale * next_double` without FMA (the probe hashes match the separate-ufunc form; AT-0b
checks it), so Linux bytes stay the same and arm64 bytes now equal them. The word consumption is
the same (one `random()` per row, in order). The draw-site catalogue entry
`gen.distribution_snapshot` (`execution/native/_draw_sites_gen.py:209-213`) changes its
`api_operation` and `call_shape` text to match.

### 5.6 Arithmetic self-check (one per process)

The first time a portable family or the histogram draw builds in a process, the kernel runs a
known-answer test. It evaluates `log`, `exp`, `ndtri` and `loggam` on a fixed 64-element vector,
including one input whose exact result is subnormal (`exp(-740)`), and compares the SHA-256 of the
output bytes with a pinned constant. On a mismatch it raises a value-free runtime error with the
new code `generate_platform_arithmetic` ("This machine's floating-point arithmetic does not match
the portable sampler; generation is refused so output stays machine-independent"). The error
names no value and is raised after any `except` block closes (defect class 3). Cost: microseconds,
once per process. This is the fail-closed form of the owner's rule (refuse, do not caveat) for a
process whose FTZ/DAZ or rounding mode was changed by another library.

### 5.7 Registry and catalog

- `CONDITIONAL_REQUIRED["distribution"]["family"]` (config) gains the six families' key lists.
  The per-key checks go in `config/_generate_type_checks.py`, beside `high_bound` and
  `clip_bound`.
- The sampler registry `_FAMILIES` (generation) gains one `FamilySpec` per family: builder, Arrow
  type, words per attempt, `variable_draws`.
- The existing registry test (`tests/unit/generation/kinds/test_gen_types_e1_registry.py`) keeps
  asserting the two key sets agree, so neither table can drift.
- The draw site `gen.distribution`:
  - `consumes_variable_draws` becomes `True` (PTRS and beta);
  - `call_shape` and `api_operation` name the kernel;
  - `provider_version` gains "portable kernel v1".
- `docs/native/draw-site-inventory.md` is regenerated if its sentry requires it.
- The capability-matrix and generate-option docs (#478) list the families.

## 6. Design notes

- Structure added: one kernel module and one family module under `generation/kinds/`. The family
  module answers one question (how a family turns words into values), and the kernel answers
  another (portable elementary functions).
- Open/closed: families keep arriving (the GEN-TYPES table, and the E1 plan expects more). A family
  is one `FamilySpec` entry plus its key list in `CONDITIONAL_REQUIRED`. The registry test is the
  guard that keeps the two tables in step.
- Information hiding: `ParametricStream` keeps its two-method interface (`next_batch`, and the
  constructor from column and context). Carry and table state stay inside the stream.
- One place per fact:
  - family keys live in `CONDITIONAL_REQUIRED`;
  - the sampler, type and draw shape live in `FamilySpec`;
  - the kernel constants live in the kernel module;
  - the KAT digest lives beside the kernel;
  - the 2M-draw digests live in the draw test.
- Patterns followed: fdlibm (via the StrictMath precedent), AS 241, Marsaglia-Tsang, PTRS,
  table-lookup inversion. Each is cited in the docstring of the module that implements it.
- House style: typed `FamilySpec` dataclass, no flag arguments (each family has its own builder),
  comments explain why (FMA, exactness of the `p` grid).
- The kernel stays private to `generation/kinds/` while it has one consumer. F-1 (formula `gauss`)
  would be a second consumer; that slice moves it to a foundation leaf (`src/decoy_engine/_<name>.py`).

## 7. Choke point and guard

Rule: a generation draw path that promises machine-independent bytes reaches floating-point math
only through the portable kernel and the exact basic ufuncs.

- **Choke point.** `_FAMILIES` is the only way a `distribution` column gets a sampler
  (`ParametricStream.__init__` indexes it). The histogram draw has one site.
- **Guard** `tests/sentry/defects/test_portable_draw_ops.py`. It derives the modules to scan by a
  walk: the `__module__` of every `FamilySpec` builder, the kernel module, `_parametric.py`, and
  the function object behind the `gen.distribution_snapshot` draw site (its module and qualname,
  taken from `call_site`). It parses each with `ast` and fails on:
  - any call that resolves to a numpy or `math` transcendental (`log`, `log1p`, `log2`, `log10`,
    `exp`, `expm1`, `exp2`, `power`, `float_power`, `sin`, `cos`, `tan`, `arctan`, `tanh`,
    `lgamma`, `gamma`, `erf`, `erfc`, `pow`);
  - any `Generator` method other than `random` (for example `rng.normal`, `rng.uniform`,
    `rng.gamma`, `rng.integers` on a float path);
  - `np.sum`, `np.mean`, `np.dot`, `np.matmul`, `np.prod`, `np.polyval`, and the method forms
    `.sum()`, `.mean()`, `.dot()`, `.prod()` on any receiver;
  - the `**` operator and `pow()`;
  - `sort`, `argsort` or `sorted` without `kind="stable"` (or `stable=True`).

  The kernel module is exempt only from the transcendental rule, and only for its own definitions
  of those names.
- **Normalized spellings covered** (defect class 5):
  - `import numpy as np`, `import numpy`, `from numpy import log`, `from numpy import log as ln`;
  - `import math`, `from math import exp`;
  - attribute chains (`np.random.Generator.normal`, `self._rng.normal`);
  - method forms on any receiver (`x.sum()`).

  Deliberate evasion (`getattr(np, "lo" + "g")`, `eval`) is out of scope (V2-LOG).
- **Seeded proof.** A scratch copy of a family module gains `np.log(x)`, then `rng.normal()`, then
  `x.sum()`, then `x ** 2`, then `np.argsort(x)`; each must turn the guard red. Removing the
  kernel's exemption must turn it red on the kernel.
- **The one mutation named for review:** replace one kernel `log` call in the exponential builder
  with `np.log`. The guard goes red, and AT-4's arm64-vs-x86 digest equality goes red when run on
  both machines.

## 7a. Enumerations (from the code at engine `6e2e57bd`)

### Routes

- resident and legacy whole-table: `generation/synthesize.py:344` calls `parametric_values`;
- streamed: `generation/_column_streams.py:298` builds `ParametricStream`;
- pinned: `execute_pinned` reaches the same two entries through the one generation core;
- Parquet sink: the streamed route with a bounded sink (E1's AT covers it);
- fitted histogram draw: `ColumnGenerator._generate_distribution_numeric`
  (`generators/_distribution.py:89`, reached from `:71`), the only caller of the `uniform` line.

Config entry points that must refuse the same configs: validation (`option_problems`),
`compile_plan` and `run_config_only_checks`. All three go through `generate_family_problems` and
the per-key `OptionSpec` checks.

Census of numpy distribution-method calls on generation draw paths (`generation/`, `generators/`,
`providers_v2/`): one, `generators/_distribution.py:166` (`rng.uniform`), fixed in E1b-0. libm
calls on generation draw paths: one, formula `gauss` (F-1).

### Exit paths

- A normal batch returns n values and keeps the carry.
- A config refusal raises at validation, compile or config-only checks (`generate_option_*`)
  before any draw.
- A KAT mismatch raises `generate_platform_arithmetic` at stream construction, before any draw.
- A budget refusal raises the existing ledger error before the zipf table is allocated.

None of these leaves state behind: there are no files and no shared caches.

### FK shapes

N/A: `distribution` columns are source-free and cannot be keys or references, and the histogram
draw is a non-key numeric column, so no FK shape reaches either changed site.

### Fields

- New option keys: `mean`, `std`, `mu`, `sigma`, `s`, `n`, `alpha`, `beta`.
- New `family` values: `normal`, `lognormal`, `exponential`, `poisson`, `beta`, `zipf`.
- Unchanged keys: `min`, `max`, `null_probability`, `output_type`.
- Snapshot fields: none change.

### Output surfaces

- Generated column values (CSV, Parquet, in-memory Arrow), int64 or float64 per family.
- Refusal messages and codes (value-free).
- The draw-site catalogue entries `gen.distribution` and `gen.distribution_snapshot`
  (`execution/native/_draw_sites_gen.py`), pinned by
  `tests/native/test_draw_sites_catalogue_pinned.py`.
- The capability matrix and generate-option docs.

## 7b. Class table

| # | Class | Where it applies | Structure that closes it | Proof | Mutation that must turn it red |
|---|---|---|---|---|---|
| 1 | Value-bearing output | range and arithmetic refusals | codes plus key names only; KAT message names no value | AT-5, AT-7 | put the offending parameter in the `generate_option_range` message (AT-5 red) |
| 2 | Cleanup missing on an exit path | none | N/A: pure array functions; no file, lock or scratch opened | n/a | n/a |
| 3 | State machine and concurrency | the rejection carry and the zipf table, both per stream | state lives in one `ParametricStream`, built per column per run, never shared across threads or runs | AT-3 | share the carry across two columns (AT-3 red) |
| 4 | Route parity and silent fallback | resident, streamed, legacy, pinned, Parquet sink | one `ParametricStream` behind every route; no fallback to numpy samplers | AT-9 | streamed route calls `rng.normal` directly (AT-9 and AT-6 red) |
| 5 | Referential integrity | none | N/A: source-free columns, no keys or references | n/a | n/a |
| 6 | Untrusted producer | YAML config parameters | strict number checks (bool refused, finite, int where required), range rules at three entry points | AT-5 | accept a float `n` for zipf (AT-5 red) |
| 7 | Check then use (TOCTOU) | none | N/A: no file or external state read between check and use; parameters come from the compiled plan | n/a | n/a |
| 8 | Weak tests | small digests that miss rare one-ulp rows; moment tests a wrong sampler can pass | 2M-draw digests on both arches; exact-CDF KS and chi-square per family | AT-1, AT-4 | cut `_BIT_ROWS` to 10,000 and keep the normal-via-`np.log` mutant (it then passes; the review checks the count stays 2M) |
| 9 | Refactor drift | E1 `uniform`; histogram Linux bytes | uniform untouched; histogram formula equal to numpy's x86 `uniform` | E1 digest, AT-0b | change uniform to the kernel path (E1 digest red) |
| 10 | Frozen contract drift | determinism promise, reason catalog and f29 manifest, draw-site catalogue, config option catalog | additive families and codes; catalogue re-pinned in the same commit | AT-4, AT-5, `test_draw_sites_catalogue_pinned.py`, reason-manifest tests | add a code without the manifest bump (manifest test red) |

## 8. Placement

| New file | Directory | CODEMAP "Where Things Go" row | Beside |
|---|---|---|---|
| `_portable_math.py` | `src/decoy_engine/generation/kinds/` | Generation: "source-free types in `src/decoy_engine/generation/kinds/`" | `_parametric.py` |
| `_families.py` | `src/decoy_engine/generation/kinds/` | same row | `_parametric.py` |
| `test_gen_types_e1b_kernel.py` | `tests/unit/generation/kinds/` | Generation tests `tests/unit/generation/` | `test_gen_types_e1_draws.py` |
| `test_gen_types_e1b_families.py` | `tests/unit/generation/kinds/` | same row | `test_gen_types_e1_draws.py` |
| `test_fitted_histogram_portable.py` | `tests/unit/generation/statistical/` | same row | existing statistical tests |
| `test_portable_draw_ops.py` | `tests/sentry/defects/` | Sentry test: "recurring-defect class rules in `tests/sentry/defects/`" | `test_generate_option_reads.py` |
| `gen-types-e1b-0.md`, `gen-types-e1b-1.md`, `gen-types-e1b-2.md` | `changelog.d/` | Changelog fragments | `gen-types-e2.md` |

`generation/kinds/` holds 4 modules today, so 6 is well under the fan-out limit of 25. No CODEMAP
row changes. The CODEMAP generation table row for `generation/kinds/` (line 60) gains the two
modules. The new reason code goes in the runtime reason namespace part file
(`reasons/runtime/_part1.py`, 382 lines, room confirmed by the GEN-TYPES plan) with the f29 reason
manifest bumped as for E1.

Edited files: `_parametric.py`, `config/_generate_options.py`, `config/_generate_type_checks.py`,
`generators/_distribution.py`, `execution/native/_draw_sites_gen.py`, `generation/_bounds.py` (the
zipf table cost), `docs/determinism.md`, the capability matrix script and doc, the generate-options
doc, and `test_gen_types_e1_draws.py` (new families join its parametrized digest, batch and route
tests).

## 8a. Reuse inventory

| Need | Reused | Not built |
|---|---|---|
| Uniform words | the column's `default_rng(base_int("dist"))` and `random()` from E1 | a new RNG family or seeding |
| Exact table search | `numpy.searchsorted` | a hand-written binary search |
| Cumulative table | `numpy.cumsum` (sequential, measured portable) | a custom accumulator |
| `sqrt` | `numpy.sqrt` (IEEE correctly rounded) | a kernel `sqrt` |
| Clip | `numpy.clip`, as in E1 | a resampling truncation |
| Option checks and codes | `CONDITIONAL_REQUIRED`, `OptionSpec`, `generate_option_range`/`_unknown`/`_required` | new config codes |
| Budget | `GenerationBudgetLedger` and `_bounds.py` stage formulas | a separate memory guard |
| Cross-arch digest test | E1's `_BIT_DIGESTS` and its completeness test | a new test harness |
| `log`, `exp`, `ndtri`, `loggam` | published algorithms (fdlibm, AS 241, numpy `random_loggam`), ported | numpy or `math` versions (not portable, section 3) |

## 8b. Frozen contracts touched

| Contract | Change | Evidence |
|---|---|---|
| `docs/determinism.md` cross-machine promise | now holds for six more families and for the histogram draw on arm64 | AT-0a, AT-4 |
| Generate option catalog (`GenerateTypeOptions`, public since #460) | additive: six family values and their keys; `required_when` entries | AT-5, the catalog tests |
| Reason catalog and f29 manifest | additive code `generate_platform_arithmetic` | reason-manifest tests with a new manifest version |
| Draw-site catalogue | `gen.distribution` text and `consumes_variable_draws`; `gen.distribution_snapshot` text | `test_draw_sites_catalogue_pinned.py` re-pinned in the same commit |
| Generated bytes | uniform unchanged; histogram unchanged on Linux and moved on macOS arm64 (Q1) | E1 digest, AT-0b |

## 9. Failure modes

| # | Failure | Effect | Prevented by | Test |
|---|---|---|---|---|
| F1 | A builder calls numpy or `math` libm, or a `Generator` distribution method | bytes differ by machine, rarely (about 1 row in 267k), so small pins miss it | guard; kernel only | AT-6, AT-4 |
| F2 | `a + b*c` fused on arm64 | last-bit differences | separate ufuncs; no compiled code in the path | AT-4, AT-0b |
| F3 | `np.sum`/`mean` order changes with the numpy version | table totals move under a numpy upgrade | `cumsum`, no reductions | AT-6, AT-4 under 3 numpy |
| F4 | `ndtri` sees `p = 1.0` | `+inf` in a normal column | exact midpoint grid | AT-2 extremes |
| F5 | `u * cdf[-1]` rounds to `cdf[-1]` | index out of range | clamp | AT-2 extremes |
| F6 | Rejection carry drops or reorders a value at a batch edge | output depends on batch size | carry by accepted value, never by word | AT-3 (Hypothesis partitions) |
| F7 | Parameters overflow (`lognormal` `mu` large, huge `std`) | inf or NaN in data | range rules at validation, compile and config-only checks | AT-5 |
| F8 | Tiny beta shapes give `0/0` | NaN | log-space ratio; floor 0.001 | AT-2, AT-5 |
| F9 | FTZ/DAZ or a rounding mode set by another library | subnormal or rounding differences | KAT refuses, value-free | AT-7 |
| F10 | Zipf table rebuilt per batch, or not budgeted | slow run; memory over the ledger | built once in the stream; ledger reservation | AT-3 build count, AT-8 |
| F11 | Histogram fix moves Linux bytes | existing x86 snapshots regenerate differently | same formula as numpy's x86 `uniform` | AT-0b |
| F12 | Family key tables drift (config vs sampler) | a family validates but has no sampler, or the reverse | registry test | AT-5 |
| F13 | Refusal text carries a parameter value | value leak in logs or errors | codes only, value-free messages | AT-5, AT-7 assert message and `__context__` |
| F14 | Wrong distribution (port error) | silently wrong data | statistical tests against the exact CDF or pmf | AT-1 |
| F15 | Untested platform (Linux arm64, Windows) differs | promise broken there | IEEE basic ops only; KAT refuses at runtime | residual; KAT is the net |

## 10. Acceptance tests

Each is red on base (the family is refused or the site differs) and has a named mutation.

| Id | Test | Mutation that must turn it red |
|---|---|---|
| AT-0a | Histogram draw: the 2M-row digest of `_generate_distribution_numeric` output equals one pinned value on arm64 and on x86 | restore `rng.uniform(bin_lo, bin_hi)` (red on arm64) |
| AT-0b | The new histogram formula equals numpy 2.2.6's `rng.uniform(lo, hi)` byte for byte on x86-64 (the pre-change Linux digest is pinned) | form the draw as `bin_lo * (1 - u) + bin_hi * u` (same distribution, different bits on x86) |
| AT-1 | Distribution correctness per family at 200k draws: continuous families pass a one-sample KS test against the exact CDF (normal via `math.erfc`, exponential and lognormal closed form, beta via a 4096-point numeric CDF computed in the test); discrete families pass a chi-square goodness-of-fit against the exact pmf (`math.lgamma`); first two moments within 4 standard errors (E1's test) | swap PTRS's `vr` constant for 0.86; drop the `+ 2**-54` grid shift; use `log(u)` for exponential |
| AT-2 | Edge inputs: `ndtri` at the first and last grid points is finite and symmetric; exponential at `u = 0` returns 0; table inversion at `x == cdf[-1]` returns the last value; beta at `alpha = beta = 0.001` is finite and in [0, 1] | remove the clamp; form `p` as `u + 2**-54` for all `u` |
| AT-3 | Prefix stability: for every family, Hypothesis-generated partitions of 5,000 rows (seeded database) concatenate to the one-shot draw; batches 1, 7, 1000 too; zipf builds its table once per stream | reset the carry per batch; rebuild the table in `next_batch` |
| AT-4 | Cross-arch bytes: every family's 2,000,000-draw SHA-256 equals its pinned digest; `test_every_shipped_family_has_a_pinned_bit_digest` still requires a digest per family. Run by the builder on the Mac (arm64) and a Linux VM (x86-64), each under Python 3.10, 3.11 and 3.12 (numpy 2.2.6, 2.4.6, 2.5.0), with the logs in the report | replace one kernel `log` with `np.log` (red on one arch) |
| AT-5 | Config: each family accepts its keys and refuses a missing key (`generate_option_required`), a key from another family (`generate_option_unknown`), and each range rule (`generate_option_range`) at validation, `compile_plan` and `run_config_only_checks`; messages carry the key name, never the value; the config and sampler key tables agree | drop the lognormal overflow rule; add a family to `_FAMILIES` only |
| AT-6 | Guard: `tests/sentry/defects/test_portable_draw_ops.py` passes on the tree, and each seeded spelling in section 7 is red in a scratch copy | remove the `.sum()` method-form rule (the seeded `x.sum()` case then passes, so the meta-test goes red) |
| AT-7 | KAT: a pinned digest passes on the tree; patching one kernel coefficient makes stream construction raise `generate_platform_arithmetic`, value-free, with `__cause__` and `__context__` both None; the KAT runs once per process | skip the KAT on the histogram site; compare against a recomputed digest |
| AT-8 | Budget: a zipf column with `n = 1,000,000` reserves its table bytes in the ledger, and a ledger smaller than that refuses with the existing budget error | drop the reservation |
| AT-9 | Routes: resident, streamed, legacy and pinned agree for every family (E1's parametrized test, now over seven families); the Parquet sink route agrees | route one family's streamed path around `ParametricStream` |
| AT-10 | Performance (marker `perf`, not T1): 1,000,000 draws per family on the Mac, warm, 5 runs, median and max reported; budget: table and inversion families at most 4x `uniform`, rejection families at most 12x | none (a budget, not a mutation) |

## 11. Slices

Proportion rule: three slices, because they carry different risk. E1b-0 moves existing macOS bytes
for fitted columns. E1b-1 adds the kernel, the guard and the reason code. E1b-2 adds the stateful
carry. Docs fold into each slice's own changes and finish in E1b-2.

| Slice | Content | Tests (T1) | Full suite (T3 at merge) | Size |
|---|---|---|---|---|
| E1b-0 | Histogram draw fix; `gen.distribution_snapshot` catalogue text; changelog fragment | AT-0a, AT-0b; `tests/unit/generation/statistical/`, `tests/native/test_draw_site_inventory_coverage.py`, the determinism goldens, `tests/sentry` | yes: frozen contract (macOS output bytes of fitted columns move) | about 40 lines |
| E1b-1 | Kernel, KAT and `generate_platform_arithmetic`; `normal`, `lognormal`, `exponential`; guard; registry entries and range rules; `gen.distribution` catalogue; docs for the three families | AT-1 to AT-7 and AT-9 for the three; E1 draw and registry tests; config option tests; reason-manifest tests; `tests/sentry` | yes: reason catalog and manifest fixture (frozen contract) | about 650 lines |
| E1b-2 | Table inversion (zipf, poisson below 10), rejection carry (PTRS, beta), `consumes_variable_draws`; budget reservation; `docs/determinism.md`, capability matrix, generate-options doc | AT-1 to AT-10 for the remaining families | yes: draw-site catalogue (frozen contract) | about 550 lines |

Order: E1b-0 (independent, may land first), then E1b-1, then E1b-2. Each slice's builder reports
AT-4 (or AT-0a) logs from both machines. Mutations run on the final head (defect class 9).

Release: after E1b-2 merges, a release request goes to claudeorchestrator (daily engine release
rule, T0439). E1b-0 can ride the next daily release alone.

## 12. Questions for the owner (recommended answer first)

- Q1. E1b-0 moves macOS bytes of fitted numeric columns to match Linux. Pre-release, no locking
  (T0407), so no seed-protocol bump. Recommended: ship it as a fix with a changelog line. The
  alternative is a `SEED_PROTOCOL_VERSION` bump, which would move every column's bytes on every
  machine, which is worse.
- Q2. Parameter bounds not set by the GEN-TYPES plan: beta shapes in `[0.001, 1e9]`, and the
  finite-output rules for normal, lognormal and exponential. Recommended: as in 5.3.
- Q3. Runtime KAT that refuses (5.6), as opposed to tests only. Recommended: runtime. It costs
  microseconds, and it is the only net for untested platforms and FTZ-setting libraries.

## 12a. Pre-mortem

- The fdlibm port has a transcription slip that is still deterministic. Digests then agree across
  machines while the values are a little wrong. AT-1's exact-CDF tests and the kernel accuracy
  test (kernel against `math.log`/`math.exp` over 1,000,000 points, at most 1 ulp apart) catch it.
- numpy changes `random()` or `cumsum`. NEP 19 makes BitGenerator streams the stable part, and
  AT-4 runs under all three locked numpy versions, so a new lock band that moves a digest is red
  before it merges.
- A builder "simplifies" `np.add(np.multiply(...))` into `a + b * c` on numpy arrays. That is
  still two ufuncs, so it is safe; the guard allows it. The risk is compiled code (Cython, numba,
  the native companion) taking over a family. The guard's module walk includes any module a
  `FamilySpec` builder lives in, and the native draw-site row says the draw stays in Python.
- Rejection-family speed: beta with both shapes small accepts about 0.9 squared of attempts and
  computes several logs each. AT-10 sets the budget; if it is missed, the fix is a larger block
  size, not a different algorithm.
- The KAT refuses on a real user machine. That is the designed outcome (refuse, not caveat), and
  the message says what to do: run on a machine without the arithmetic change, or remove the
  library that sets FTZ.

## 13. Follow-ups (not E1b)

- F-1. Formula `gauss` (`generators/_formula.py:325`) uses Python `random.gauss`, which calls libm
  `log` and `cos`. Fix: a kernel-based `gauss` from the formula stream. This moves the kernel to a
  foundation leaf. Needs a roadmap row from claudeorchestrator.
- F-2. Counter-based per-row streams (Philox) would make `distribution` partitionable for the
  native route. Revisit if the native route needs it.
- F-3. A Linux arm64 and a Windows runner for the AT-4 digests. The KAT covers them at runtime
  until then.
