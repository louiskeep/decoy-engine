# DPS-CODEC implementation guide

Status: PLAN COMPLETE / BUILD-READY (Codex round-6 verdict: A -- BUILD NOW),
revision 6 (remediated against Codex plan-review rounds 1-5, all
BLOCK but each spec-detail-not-design; round 5 confirmed the carrier architecture is
sound, the `carrier=None` non-DP compatibility CLOSED, 1,273 tests pass, and returned
a HIGH + MEDIUM now closed here at the ROOT rather than by another hand-patch: (HIGH)
hand-enumerating the proof stack's transitive deps is unwinnable whack-a-mole (round 4
missed SciPy; round 5 missed attrs/absl-py/wrapt/Deprecated/polars/dateutil/pytz/six +
the CPython patch), so §3.8 now certifies a mechanical LOCK FINGERPRINT over the whole
resolved environment + full CPython version instead of a curated tuple -- complete by
construction; (MEDIUM) the direct-carrier path is made genuinely pandas/pyarrow-free by
splitting the adapter into a lazily-imported `carrier_adapter.py` with a CI
import-isolation assertion). Author: Opus. Cycle: DPS-CODEC.
BUILD PROGRESS:
- **Phase 1 (carrier core) — DONE & dual-gate cleared.** Commits 7daaa2e (build)
  -> 24752ef (dennis remediation: mypy gate, coverage guard, interrupt test) ->
  75ec133 (Codex HIGH: structural bound validation) -> 6e8a9c5 (Codex LOW: coded
  bound-conversion errors). dennis APPROVE + Codex APPROVE at 75ec133; crown-jewel
  suite mutation-verified non-vacuous. `quality/carriers.py` (pandas-free),
  `dp_schema.py`, `test_carriers.py` (62 passed, 1 phase-2 xfail). L1 (top-level
  `__init__` eager pandas import) remains deferred to when the fit API lands.
- **Phase 2 (pandas adapter) — DONE & dual-gate cleared.** Commits 7495086 (build,
  DataFrame-path crown-jewel arm un-xfailed) -> d76ef46 (dennis LOW-1: coded
  duplicate-column error) -> 9556773 (Codex LOWs: string carrier/kind validation,
  MultiIndex ambiguous-column guard, real complex128 boxing seed). dennis APPROVE +
  Codex APPROVE. `quality/carrier_adapter.py` (lazily imported; guarded positional
  per-cell fetch -> phase-1 codecs -> sanitize; closed kind x carrier validation).
  Codex mutation-confirmed the crown-jewel is non-vacuous and found no stability-1
  break. test_carriers.py 97 passed; direct-path isolation intact.
- **Phase 3 (certified lock-fingerprint manifest + fit/generation gates + CI matrix) —
  DONE & dual-gate cleared.** Commits 677f9ab (build) -> 0e20af7 (dennis LOWs) ->
  9bfe363 (Codex MEDIUM: reproducible dev+lint+vault profile re-pin). dennis APPROVE +
  Codex APPROVE (Codex agreed the subset-direction finding is MEDIUM not HIGH: the
  runtime gate hashes the actual installed set vs the frozen literal, so an incomplete
  env cannot false-pass). `quality/dp_provenance.py` (mechanical SHA-256 fingerprint
  over the complete installed dist set + full CPython + single Linux/x86-64 platform;
  fail-closed check_fit_environment / validate_recorded_provenance; lock==installed
  guard), `_DP_EPSILON_CEILING=700.0` in dp_budget, draft CI matrix. Certified pin =
  6c0b2bbd... over the reproducible 77-dist profile. Fit/generation CALL-SITE wiring
  deferred to phase 5.
- **Phase 4 (dp_schedule/dp_budget carrier plumbing) — DONE & dual-gate cleared.**
  Commits 1005be0 (build) -> fcde874 (dennis LOWs: wording, unscheduled-pair guard).
  dennis APPROVE (its own Codex concurred SOUND) + independent Codex APPROVE (0
  findings; probe-proved legacy-crossing certificates byte-identical, bool measurements
  stability-1 with certs identical to str, endpoint fallback cannot under-noise,
  epsilon-700 ceiling fails closed with 0 backend calls). Carrier threaded through
  CategoricalQuerySpec + frozen cache key; bool-domain make_count_by(bool)/make_count(
  bool) for flag; epsilon ceiling wired into composition; dp_ledger accepts
  epsilon_total >= 0. test_dp.py held at 1180 (no regression to live accounting).
- **Phase 5 (orchestration + dps-marginal/v3 artifact: dp.py fit API, snapshot.py,
  _checks_dp.py) — DONE & dual-gate cleared.** The integration phase: wired phases 1-4
  into the live fit path (`column_schema` fit API through the adapter/carrier codecs),
  bumped the artifact v2->v3 (pre-GA hard break) recording the proof-stack identity +
  per-column carrier + source boundary, and rewired generation verification (recorded-
  provenance validation, v3 schema, epsilon >= 0, carrier-aware query count + per-column
  identity). Build `cafca62`; remediation `bfc9f80`/`4ccb67a` (dennis H1/M1), `07b15f5`->
  `c6a46aa` (dennis H2 module split + H3 flag e2e + L1 verifier + M2 flag guard),
  `60b4fcc` (Codex TOCTOU: freeze column_schema at entry), `aeda621` (Codex BLOCKER dp.py
  size -> new dp_fit_schema.py; HIGH columns/dp carrier agreement; MEDIUM bounds freeze +
  import-cycle break + kind/bounds cross-check). Gates: dennis logic-approved (round 1,
  traced+probed the DP path correct) -> Codex 5 findings (round 2) -> all remediated ->
  Codex APPROVE with 0 findings (round 3). Codex confirmed clean: number/text byte-
  identity, flag bool-domain stability-1, query-count, provenance ordering, epsilon >= 0.
  New pandas-free module `quality/dp_fit_schema.py`; `OpenDpReleaseSession` extracted to
  `quality/dp_session.py` (phase-4 size debt). Full DP+plan+generation surface green.
- **Phase 6 (flag artifact/generation semantics in `_sample.py`) — DONE & dual-gate
  cleared.** Generation-side flag decoder ("true"/"false" -> genuine Python `bool`),
  `carrier` threaded onto the frozen `StatisticalSpec` (dropped from serialization when
  `None`, honored only for a compiler-verified column), `other_mode="emit"` rejected for
  flag, a verifier-side canonical-token shape guard (`dp_flag_token_invalid`), and the
  phase-5 M2 guard lifted. Build `bb95747`; remediation `bc33c1a` (dennis+Codex HIGH: the
  M2-lift went too far -- an unverified recognizable v3 flag artifact fell to the legacy
  `str()` path and emitted strings against a `bool` dtype; restored fail-closed via
  `statistical_flag_requires_dp_declaration`, gated on the `dp`-block-presence
  discriminator so the guide-3.9 legacy path for ordinary snapshots is preserved). Gates:
  dennis BLOCK 1 HIGH (its own Codex reproduced it) -> remediated -> independent Codex
  APPROVE 0 findings. Known LOW carried forward: `plan/_checks_dp.py` at exactly 600 LOC
  (zero headroom; next editor extracts). Full generation+plan+quality surface green.
- **Phase 7 (retire `dp_normalize.py` once the property/carrier suite subsumes its
  matrix) — NEXT.**
- **Phase 8 (docs: `_spec.py`/`_global_settings.py` wording, claim-copy tests, CHANGELOG,
  `what-we-cannot-prove.md`) — PENDING.** MUST ALSO FIX (phase-7 finding): the DP artifact
  ships `dp_policy._DP_NORMALIZATION_POLICY["categorical_labels"]` prose that still
  describes the RETIRED dp_normalize behavior (bool/real/decimal/zero-imaginary-complex/
  huge-int rendered as text labels). The `text` carrier now DROPS non-`str` cells (never
  stringifies) and bool routes through the separate `flag` carrier, so this shipped claim
  over-describes the release. Correct the artifact prose and restore the assertion in
  `test_normalization_policy_is_identical_whatever_the_frame_holds` (phase 7 left a
  KNOWN GAP comment there rather than assert false-but-passing text).

GATE OUTCOME: Codex round 6 (framed as the explicit build-now-vs-revise decision)
probed the fingerprint + adapter split and returned **A -- BUILD NOW**: revision 6 is
build-ready, no residual carrier/codec/adjacency/artifact/compatibility design hole
remains, and the round-5 transitive-distribution HIGH is closed by construction; the
remaining items are executable build-time checks best retired as code under the build's
own review gates (folded into §7 as the round-6 build-gate acceptance checks + honest
native-artifact scope wording in §3.8). AWAITING Cam greenlight to start BUILD phase 1.

Expands ROADMAP §DPS item 5; grounded in the current code it modifies.

## 1. Goal, stated honestly

Replace the scattered, value-level pandas type inference in the DP fit with **one
canonical typed-carrier layer converted through a single total, versioned codec**,
pinned to an exact, tested dependency set.

**What this achieves (round-1/2 BLOCKER correction).** Typed carriers make
`CarrierTable -> OpenDP vector` stability-1 by construction. They do NOT by
themselves make `DataFrame -> CarrierTable` stability-1. So:

- **Core claim** is over a *canonical* `CarrierTable` (invariants in §3.1) and is
  pandas-free. A caller supplying a canonical `CarrierTable` gets DP with no pandas
  in the theorem.
- **End-to-end DataFrame claim** additionally depends on the pandas adapter being a
  **total, boxing-invariant stability-1 transformation**. The adapter lives in the
  engine and INSIDE the end-to-end theorem (outside the OpenDP mechanism core, not
  outside the proof). The win: the adapter is ONE certified codec, property-tested,
  in the crown-jewel invariant, gated to an exact dependency set, replacing per-type
  inference defended example-by-example across an unbounded version range.

Both claims inherit the **documented residual exclusions** carried forward from
`what-we-cannot-prove.md` (see §3.7): cell dunders raising `KeyboardInterrupt`/
`SystemExit`, live/executable-object cells and `Series`-subclass containers
(fail-loud, out of domain), and the single-threaded warning-suppression
precondition. The claim is total over DATA VALUES, not over arbitrary embedded
executable objects. "Formally specified total" means total-over-data with these
exclusions, not unconditional.

Non-goal: changing any privacy MECHANISM. The releases stay exactly as landed:
numeric = binned histogram count (mean is `None`), categorical = grouped
thresholded count + non-null total. No mean mechanism is added (round-2 HIGH-4).

## 2. Why (the bug class)

OpenDP proves (epsilon, delta) over its vector with stability 1. Equivalence to a
row-level claim needs: add/remove one row => at most one changed vector element,
others equal under OpenDP's atomic equality. pandas derives storage from ALL rows,
so one added row can rebox every value (int+None->float; Arrow list->object of
ndarrays; **bool->complex128 when a `1j` is appended**), breaking distance-1 while
`map(1)` is unchanged.

## 3. Design

### 3.1 CarrierTable: physical shape AND canonical invariants (round-2 BLOCKER-1)

Owned by a new module `quality/carriers.py` (public import path
`decoy_engine.quality.carriers`). Physical shape:

```
CarrierTable:
    row_count: int                      # authoritative N for the row-count release
    columns: dict[name, Column]
Column := NumberColumn(values: float64 ndarray, validity: bool ndarray)
        | FlagColumn(values:   bool  ndarray, validity: bool ndarray)
        | TextColumn(values:  tuple[str,...], validity: bool ndarray)
```

**Canonical invariants** (a `CarrierTable` is "valid" only if ALL hold; enforced by
`sanitize_carrier_table()` which runs on EVERY input, including a directly-supplied
`CarrierTable` that bypasses the adapter — round-2 probe: OpenDP assumes domain
membership rather than enforcing it, so a valid-marked NaN reached `make_find_bin`,
a NUL text truncated, a surrogate raised):

- `row_count` is a non-negative `int` and NOT a `bool` (round-3 HIGH: `bool` is an
  `int` subclass); `columns` keys exactly equal the `column_schema` keys.
- every column is 1-D, exact dtype per its type, `len == row_count`, `validity` is
  1-D bool `len == row_count`.
- for `validity[i] == True`: NumberColumn value is finite-or-clamped, non-NaN,
  binary64-canonical, signed-zero-normalized (`-0.0 -> 0.0`); FlagColumn value is a
  real `numpy.bool_`; TextColumn value has `type(v) is str` exactly (round-3 HIGH:
  NOT merely `isinstance(v, str)` -- a `str` subclass can override the methods the
  sanitizer invokes while still satisfying the written invariant), UTF-8-encodable,
  no embedded NUL, no lone surrogate.
- `validity[i] == False` positions are dropped before the FFI call; only valid
  cells enter the OpenDP vector.
- **Adjacency** is defined over `(value, validity)` rows: add/remove one row changes
  at most one vector element (a valid row adds/removes one element; an invalid row
  changes none), and changes `row_count` by exactly one.

`sanitize_carrier_table()` re-applies the per-carrier FFI-safety checks (§3.2) to
direct carriers so the direct path cannot smuggle a NaN/NUL/surrogate past OpenDP.

### 3.2 Carrier codecs (closed set, executable totality)

Each codec is total over a single pandas cell, output invariant under every
reboxing. "Total" = the whole path **fetch -> container/temporal reject -> null
detect -> decode -> validity -> FFI-safety**, subject to the §3.7 exclusions.

- **`number`**: reject containers and temporal BEFORE unboxing (`datetime64[ns].item()`
  / `timedelta64[ns].item()` return ints; see `dp_normalize.py:109`). Zero-imaginary
  complex -> real; nonzero imaginary -> invalid. NaN -> invalid; +/-inf clamps to the
  bound; finite out-of-domain clamps into `[lower, upper]`; binary64 image;
  signed-zero-normalized. Equality is OpenDP atomic f64 equality, not byte-identity.
- **`flag`**: `bool`, exact int/float `0`/`1` reboxings, AND zero-imaginary complex
  whose real is exactly 0 or 1 (round-1 BLOCKER, probe-confirmed the codec collapses
  the bool->complex128 rebox). Nonzero imaginary / any other -> invalid.
  Container/temporal reject before the equality check.
- **`text`**: Unicode only, verbatim (never `str()`). Reject embedded NUL (OpenDP
  truncates; `dp_normalize.py:574`) and lone surrogates (`UnicodeEncodeError`). A
  `text` column stored as `int64` -> all-invalid, NOT stringified.

Datetime/timedelta: invalid (null) for v1, rejected before unboxing.

### 3.3 Allowed release-kind x carrier pairs (closed table)

| kind        | allowed carrier(s) | grouped mechanism         | total mechanism            |
|-------------|--------------------|---------------------------|----------------------------|
| numeric     | `number`           | binned histogram count    | (n/a; row-count only)      |
| categorical | `text`, `flag`     | `make_count_by(T)`        | `make_count(T)` non-null   |

`categorical + number` REJECTED for v1 (OpenDP has no float `make_count_by`; probe
confirmed). Schema validation fails closed on any unlisted pair. `T` is `str` for
`text`, `bool` for `flag`.

### 3.4 The categorical `flag` path, end-to-end (round-2 BLOCKER-2)

A categorical release is TWO measurements (`dp_budget.release_categorical`): grouped
count and non-null total. The current non-null-total `count_measurement` uses
`atom_domain(T=str)` (`dp_budget.py:204`), which fails on bools ("inferred bool,
expected String"). So for `flag`:

- Add a `carrier` field to `CategoricalQuerySpec` (`dp_schedule.py:42`, built at
  `dp.py:436`); it enters the schedule and the cache signature (§4).
- Both measurements get a **bool-domain** variant: grouped `make_count_by(bool)`
  (probe-confirmed) and a bool-domain non-null `make_count(bool)`. `str` carrier keeps
  the existing str-domain pair. Selected by the column's carrier.
- **Carrier reaches the sampler (round-3 BLOCKER):** `StatisticalSpec`
  (`_spec.py:69`, a frozen dataclass with no carrier field today) gains a
  `carrier: Optional[str]` field defaulting to `None`, threaded through
  serialization, artifact reconstruction, and `load_spec` validation, so the sampler
  knows a column's domain. `None` = the legacy non-DP path (§3.9); a set carrier =
  the codec path. It is mandatory + strictly validated only for verified DP-v3
  columns. Serialization drops the key when `carrier is None` (post-`asdict`, so older
  serialized Plans round-trip byte-identically); a regression asserts that a stray
  `carrier` on an ordinary `distribution-snapshot/v1` column stays on the legacy
  sampling path.
- **Artifact encoding:** flag `top_values[].value` entries serialize deterministically
  as the canonical tokens `"true"`/`"false"` (not Python `str(bool)` `"True"/"False"`,
  not `"0"/"1"`); the column records `carrier: "flag"`, `dtype: "bool"`. The verifier
  accepts, for a flag column, ONLY the two canonical tokens `"true"`/`"false"` (any
  other value fails closed with a coded `dp_flag_token_invalid` shape error).
- **Generation output = one representation (round-3 BLOCKER):** the sampler
  (`_sample._categorical_tables`, `:156`) currently `str()`-forces every value. For a
  `flag` column it decodes `"true"/"false"` to Python `bool` and emits `bool` (artifact
  `dtype: "bool"`) -- one canonical representation, never `"True"/"1"` mixtures.
- **`other_mode` (round-3 BLOCKER, corrected):** the supported modes are
  `"redistribute"` and `"emit"` (`_spec.py:53`); there is no `"drop"`. `"emit"` inserts
  `OTHER_TOKEN` (`"__other__"`), which is not a flag value, so **`other_mode="emit"` is
  rejected in generate-side validation for `flag` columns**; flag columns use
  `"redistribute"` (tail mass spread across the two known categories). Enforced in
  `load_spec`.
- Tests: empty and all-invalid flag vectors; a flag column whose grouped + non-null
  total both release; one-category and both-category end-to-end fit->generate; the
  `other_mode="emit"` rejection.

### 3.5 The fit API

```
fit_dp_snapshot(
    source,                       # DataFrame (uses the adapter) OR a CarrierTable
    column_schema: {name: {kind, carrier, bounds?}},   # per-column only
    epsilon: float,               # requested ceiling, strictly > 0
    delta: float,                 # requested ceiling, strictly in (0, 1)
    numeric_bins: int,            # ONE fit-level value for v1 (round-2 HIGH-4)
    ...
)
```

`epsilon`/`delta`/`numeric_bins` are fit-level; the schema holds per-column
`kind`/`carrier`/`bounds` only. Replaces `categorical_columns`/`numeric_domains`/
`delta`. `numeric_bins` stays a single fit-level value (matches the current
artifact/verifier); no per-column bins in v1.

### 3.6 The pandas adapter (in-engine, in the end-to-end claim)

One shared adapter `DataFrame + column_schema -> CarrierTable` in
`quality/carrier_adapter.py` (a lazily-imported submodule, NOT `carriers.py`, so the
direct-carrier path stays pandas/pyarrow-free -- §3.8 MEDIUM),
inside the end-to-end claim. Owns the guarded per-cell fetch (keep the positional-read
guard from `_cells`, `dp_normalize.py:295`, for Arrow cells that raise on fetch), null
detection, validity construction, then the per-carrier codec. Its output goes through
`sanitize_carrier_table()` like any carrier. It is what the crown-jewel property test
certifies.

### 3.7 Totality exclusions carried forward (round-2 BLOCKER-1)

The claim is total over DATA VALUES with exactly the residual already documented in
`what-we-cannot-prove.md` (do not silently strengthen it). State every disposition
consistently (round-3 HIGH: rev 3 named only `__str__`/`__float__` and was
inaccurate):

- **Ordinary failure -> drop the cell.** Any conversion, null-detection, container,
  temporal, or FFI-safety failure on a cell is caught (`except BaseException` minus
  the two below) and that cell is marked invalid (dropped). This is the total,
  content-independent path.
- **`KeyboardInterrupt`/`SystemExit` from ANY invoked hook -> propagate.** Not only
  `__str__`/`__float__`: null detection can invoke a cell's `__array__` (the landed
  null-check guard re-raises there, `dp_normalize.py:553`, pinned by
  `test_dp.py:1604`), and the adapter's fetch/decode hooks likewise. Any of these two
  interrupts from any hook re-raises so an operator can Ctrl-C and a caller can exit.
  Such a cell can terminate a fit where its one-row neighbour succeeded -- out of the
  adjacency domain.
- **Live container setup -> fail loud.** A `Series` subclass whose `array` or length
  runs row-dependent caller code raises rather than silently releasing a near-null
  column. Out of domain.
- **Executable-object cells and non-canonical subclasses -> rejected / outside the
  adjacency domain** (this is why TextColumn requires `type(v) is str`, §3.1): a cell
  that is code seizing process control is not data.
- **Single-threaded precondition.** Warning suppression uses process-global
  `catch_warnings()`+`simplefilter("ignore")`; a concurrent `simplefilter("error")`
  reopens the fit-success channel. Documented, not defended.

§§1/3.5/10 and the revised `what-we-cannot-prove.md` state these explicitly, matching
the landed behaviour rather than a stronger paraphrase.

### 3.8 Dependency gate: exact certified manifest (round-2 HIGH-3)

A STATIC certified manifest. Every row certifies the **complete locked
Python-distribution version set** of the proof stack (round-6 wording, honest scope):
it captures every Python distribution and its version, not just the adapter libs
(round-4 HIGH: a tuple that omits a runtime dimension the proof depends on merely
relocates version drift). It is deliberately NOT a claim of exact binary identity --
native inputs below the distribution/version layer (OpenDP's bundled `opendp.abi3.so`
Rust core, numpy/scipy's bundled OpenBLAS/gfortran, the exact glibc patch, the exact
CPython build) are captured only at wheel/`RECORD`-hash granularity, which v1 does not
gate on. This matches the non-authentication threat model (§3.8 last bullet); if exact
tested-binary identity is later wanted, add selected wheel/`RECORD` hashes + an exact
libc constraint (deferred, ROADMAP). The row identity has two parts:

1. **Platform (round-4 HIGH-b).** v1 certifies **exactly one platform: Linux,
   x86-64, CPython, glibc (manylinux)** -- the only platform our CI actually
   exercises (`ci.yml` is Ubuntu-only). Fit derives the local platform
   (`sys.platform`, `platform.machine()`, `platform.python_implementation()`, libc)
   and fails closed with a coded `dp_platform_uncertified` error on anything else
   (Windows, macOS, ARM, PyPy, musl). The artifact records the platform string;
   generation validates it against the certified platform. Broadening to a full
   multi-OS / multi-arch wheel-hash manifest is deferred (ROADMAP; see §10 decision) --
   a false guarantee on an untested platform is exactly the bug class this redesign
   exists to kill, so v1 refuses rather than over-certifies.

2. **Proof-stack fingerprint, NOT a hand-enumerated tuple (round-5 HIGH — root-cause
   fix).** Rounds 4 and 5 showed that hand-listing the versioned distributions the
   proof depends on is unwinnable whack-a-mole: round 4 was missing SciPy; round 5 was
   missing `attrs`, `absl-py`, `Deprecated`, `wrapt` (the eager PLD/OpenDP import
   path), `polars` (pulled by `opendp.prelude`'s broad extras import), and
   `python-dateutil`/`pytz`/`six` (the pandas adapter), plus the full CPython patch
   version. The transitive closure is large and drifts, so any curated subset will
   always be incomplete. **The fix is to stop enumerating and fingerprint the whole
   resolved lock.** Each certified row is:

   `(boundary, platform_triple, cpython_full_version, lock_fingerprint)`

   where `lock_fingerprint` = a stable hash (e.g. SHA-256) over the sorted
   `(distribution_name, version)` pairs of the ENTIRE resolved locked environment for
   that `(platform, cpython)` marker set, as pinned in `uv.lock`. This is complete by
   construction — every transitive dep (attrs, absl-py, wrapt, Deprecated, polars,
   dateutil, pytz, six, and anything a future bump adds) is in the lock, hence in the
   fingerprint — and mechanically derivable, so it needs no maintenance as deps change.
   `cpython_full_version` is the full `major.minor.micro` + release level, NOT just the
   minor (round-5: a bare `3.10` row admits a moving patch release, but the PLD overflow
   boundary and other numerics can drift by patch). The human-readable key versions
   (numpy, scipy, opendp 0.15.1, dp_accounting 0.6.0, pandas, pyarrow) are retained as
   INFORMATIONAL annotation in the manifest and artifact for auditability, but the GATE
   is the fingerprint, not the annotation.

- **CI establishes the certified fingerprints.** The dependency-matrix workflow, pinned
  to EXACT CPython patches (not floating minors), computes the fingerprint for each
  certified `(boundary, platform, cpython)` row and runs the crown-jewel adjacency
  property there; the fingerprint CI observes IS the certified value written into the
  static manifest. Certify only patches CI actually runs; admit no patch the matrix
  does not exercise.
- **Fit time:** derive the local platform triple + full CPython version, and compute
  the local fingerprint from `importlib.metadata` over the installed distributions;
  check membership in the static certified set BEFORE reading private data. Fail closed
  with a coded error on any mismatch (`dp_platform_uncertified` for platform/interp,
  `dp_stack_uncertified` for a fingerprint miss).
- **Generation time:** validate the artifact's RECORDED `(platform, cpython,
  lock_fingerprint)` against the static certified set — the same complete identity is
  checked from the record, which generation does not recompute from locally installed
  libraries. This replaces the current compare-to-local-env at `_checks_dp.py:277`
  (which both over-matches generation-irrelevant libs AND, if merely deleted, would
  stop validating the proof stack entirely).
- The artifact records its `boundary` (adapter vs direct) so verification picks the
  right row class; the boundary also drives the import-isolation assertion below.
- Recording the identity is audit evidence, NOT authentication (the MAC is ROADMAP
  item 4 / schema v4).
- **Direct-carrier import isolation (round-5 MEDIUM).** The direct path must actually
  BE pandas/pyarrow-free, not merely omit them from an annotation. Today `dp.py:55`
  imports pandas eagerly and the proposed `carriers.py` bundles the adapter with the
  core carriers. Split it: `quality/carriers.py` (core `CarrierTable`, columns, codecs,
  `sanitize_carrier_table`) imports NEITHER pandas nor pyarrow; the DataFrame adapter
  lives in a separate submodule (`quality/carrier_adapter.py`) imported LAZILY only
  when a DataFrame is passed to `fit_dp_snapshot`. A direct-row CI assertion imports and
  exercises the direct-carrier path in a subprocess and asserts `pandas`/`pyarrow` are
  absent from `sys.modules`. (The fingerprint itself is over the installed lock and so
  is identical for both boundaries in one venv; the boundary distinction is enforced by
  this import-isolation assertion and by the honest per-boundary annotation, not by two
  different hashes.)

### 3.9 Artifact schema (round-1 HIGH: v3/v4)

Codec metadata = `dps-marginal/v3`; artifact-auth MAC = `dps-marginal/v4` (update the
ROADMAP MAC entry to v4 during build). v3 adds `column_schema`, per-column `carrier`,
codec id/version, the recorded proof-stack identity (§3.8: platform triple, full
CPython version, and the lock fingerprint), and the source-boundary flag. Pre-GA hard
break, no shim.

**Carrier is a DP-v3 field, not a break for non-DP snapshots (round-4 MEDIUM).**
`StatisticalSpec.carrier` (§3.4) is `Optional[str]`, defaulting to `None`. It is
mandatory and strictly validated (`number`/`flag`/`text`) ONLY for columns of a
verified `dps-marginal/v3` DP artifact. Ordinary non-DP snapshots
(`distribution-snapshot/v1` -- numeric/categorical/datetime/freetext) keep
`carrier=None`, which is the explicit legacy sampling mode: `spec_to_dict` omits the
field when `None` (older serialized Plans deserialize unchanged), `spec_from_dict` /
`load_spec` default a missing `carrier` to `None`, and the sampler dispatches on
`carrier is None` -> current behavior vs a set carrier -> the codec path. No top-level
schema bump and no migration for non-DP snapshots; the carrier path is reached only
through the DP fit API.

## 4. Fold in the comprehensive-review findings (modules KEPT)

- **Shared-mutable policy dict (Medium).** Fresh deep copy per artifact; module value
  immutable. (Confirmed correctly specified.)
- **Budget calibration (Medium).** Check BOTH `binary_search` endpoints; treat
  lower-endpoint-satisfies as feasible (`dp_budget.py:215`). Supported epsilon range:
  freeze a single CONCRETE conservative ceiling as a module constant
  (`_DP_EPSILON_CEILING`), NOT a build-time-probed value. The PLD exponential
  overflows at ~709.783 on py3.10 (709.782 OK) and the exact boundary drifts by
  Python/`dp_accounting` version, so pin the ceiling well below every certified row's
  observed overflow — set `_DP_EPSILON_CEILING = 700.0` (comfortably under 709.78 on
  all v1 manifest rows; a requested fit-wide epsilon ceiling that large is already far
  outside any real DP regime). Requests above it fail closed with a coded
  `dp_epsilon_unsupported` error BEFORE reading private data — never a raw
  `OverflowError` mid-composition. The CI dependency-matrix workflow asserts, per
  manifest row, that (a) `_DP_EPSILON_CEILING` composes without overflow and (b) the
  documented overflow point for that row stays above the frozen ceiling (a boundary
  probe that fails the build if a version bump moves the overflow at or below 700.0,
  forcing a re-pin rather than a silent regression).
- **Zero-epsilon (Medium).** Accept composed `epsilon_total >= 0` in
  `ReleaseLedger.charge` and snapshot verification; requested ceilings stay strictly
  positive. (Probe-confirmed `eps=1, delta=0.9` -> legitimate `epsilon_total=0`.)
- **Allocation cost (Medium) — frozen cache key (round-2 MEDIUM-6).** Cache ONLY
  scalar calibration/allocation results (never a measurement or certificate; the
  certificate must be read from the object actually invoked). Frozen key =
  `(carrier-bearing schedule signature, edges/bins, epsilon, delta, exact OpenDP
  version, exact dp_accounting version, backend cache namespace)`. Production backend
  uses a stable namespace; fake/stateful test backends bypass the cache or use an
  instance-unique token.
- **Private-seam wording (Low).** Reword `_fit_dp_snapshot_with_backend` as an
  unsupported internal seam.

## 5. Affected surface (round-2 HIGH-5, completed)

`quality/dp_normalize.py` (removed), `dp_budget.py`, `dp_ledger.py`, `dp_policy.py`,
`dp.py`, and:

- `quality/carriers.py` (NEW — `CarrierTable`, columns, codecs,
  `sanitize_carrier_table`; the public import path; imports NEITHER pandas nor pyarrow).
- `quality/carrier_adapter.py` (NEW — the DataFrame->CarrierTable pandas adapter, split
  out of `carriers.py` and imported lazily only when a DataFrame is passed, so the
  direct-carrier path stays pandas/pyarrow-free; round-5 MEDIUM). Remove the eager
  pandas import at `dp.py:55`.
- `quality/dp_schedule.py` (add `carrier` to `CategoricalQuerySpec`; carrier enters
  the schedule + cache signature).
- `quality/snapshot.py` (schema owner, `:78`).
- `plan/_checks_dp.py` (verification + query-count reconstruction `:305`; fix the
  generation-time version compare `:277`).
- `generation/statistical/_sample.py` (flag artifact decoding + `other_mode`).
- `generation/statistical/_spec.py` (DP-exemption wording `:184`; add
  `carrier: Optional[str] = None` and its `None`-omitting serialization).
- `config/_global_settings.py` (generate-side config docs `:34`).
- The dependency-matrix CI workflow (Linux/x86-64, pinned to exact CPython patches;
  computes the certified lock fingerprint per row, runs the crown-jewel adjacency
  property, the frozen epsilon-ceiling boundary probe, and the direct-carrier
  import-isolation assertion) + the static certified manifest file (rows =
  `(boundary, platform, cpython_full, lock_fingerprint)` + informational version
  annotation).
- Plan serialization / compat fixtures; `test_dp_claim_copy.py`; CHANGELOG; and
  `docs/what-we-cannot-prove.md` (guarantee wording — this GATES).

DPS stays explicitly unshipped in the claim until the CLI + platform callers and the
revised claim are complete.

## 6. Replaced vs kept

Replaced: `dp_normalize.py` + the fit-API surface. Kept (adapted): `dp_budget`,
`dp_ledger`, `dp_schedule`, `dp.py`, the OpenDP chains, release-ID/artifact machinery.

## 7. Test strategy (TQ discipline, invariant-first)

- **Crown-jewel invariant, landed FIRST:** for the full `DataFrame -> CarrierTable ->
  OpenDP vector` path AND the direct-`CarrierTable` path, every add/remove-one-row
  neighbour has multiset distance <= 1 **including the row-count projection**, no
  raise, no warning (subject to §3.7 exclusions), across EVERY certified manifest
  tuple. Hard merge gate.
- **Property tests (Hypothesis)** with a defined strategy generating values + their
  reboxings (bool/int/float/complex widening; list/ndarray; nullable Boolean;
  NUL/surrogate text; temporal). Assert decode totality + boxing invariance.
- **Preserve the current matrix's known examples as regression SEEDS** (do not delete
  `dp_normalize`'s matrix until the property suite proves equal strength): complex
  widening, Arrow temporal fetch errors, list->ndarray, nullable Boolean,
  NUL/surrogate text, hostile dunders, and both list-reconstruction and `pd.concat`
  paths.
- **Non-vacuous coverage guard** (current suite `test_dp.py:688`): every carrier/
  reboxing produces a real dtype-differing comparison.
- Flag-path tests (§3.4). Sanitizer tests: direct carrier with NaN/NUL/surrogate is
  rejected before FFI.
- **Round-6 build-gate acceptance checks (Codex-required, verified as executable
  invariants at the code-review gate, not in prose):**
  - **Lock-set == installed-set.** A CI assertion that the marker-selected `uv.lock`
    distribution set equals the installed `importlib.metadata` set BEFORE certifying
    its hash. Freeze the exact extras/groups profile used for certification and the
    project-inclusion policy (the editable `decoy-engine` dist); PEP 503 name
    canonicalization, exact version-string handling, duplicate-name rejection, and a
    canonical serialization before hashing. (The phase-3 certified env is the clean
    reproducible profile `uv sync --frozen --extra dev --extra lint --extra vault` =
    77 dists = 76 registry + 1 editable `decoy-engine`. The certified value is the
    checked-in literal in `dp_provenance._CERTIFIED_STACKS`, not any count in prose.)
  - **Fingerprint fail-closed** on any unrelated package added to the env (safe but
    strict -> CLI/platform hosts need their own certified rows or an isolated fit env;
    this feeds ROADMAP item 2 wiring).
  - **Direct-carrier import isolation (broader than `dp.py:55`).** The subprocess
    acceptance test (real direct-carrier fit -> assert `pandas`/`pyarrow` absent from
    `sys.modules`) must also drive fixing the eager import chain Codex found:
    `quality/__init__.py:23` (eager pandas-bearing exports), `snapshot.py:71` (schema
    constants that import pandas) -> move DP schema constants to a pandas-free module +
    lazy package exports, since importing `quality.carriers` today transitively
    executes `quality/__init__.py`.
  - **Native-artifact scope stated honestly** in `what-we-cannot-prove.md` (§3.8
    wording): the gate is the locked distribution/version set, not exact binary
    identity (opendp `.so`, OpenBLAS, glibc patch, CPython build are below the gate).
  - Crown-jewel adjacency passes on EVERY certified row.

## 8. Build order

1. `quality/carriers.py` (pandas/pyarrow-free): `CarrierTable` + invariants +
   `sanitize_carrier_table`; crown-jewel adjacency invariant (both paths, row-count
   projection) landed FIRST; the three codecs with the property strategy + regression
   seeds. Land the direct-carrier import-isolation subprocess test here (drives the
   `quality/__init__.py` / `snapshot.py` pandas-free refactor).
2. `quality/carrier_adapter.py` (lazily imported) + `column_schema` API + closed
   kind x carrier validation + `other_mode` flag rule.
3. Certified manifest as a LOCK FINGERPRINT (§3.8: lock-set==installed-set CI
   assertion, frozen profile, canonical hash) + fit/generation gates + CI matrix
   pinned to exact CPython patches.
4. `dp_schedule` carrier field; `dp_budget` bool-domain measurements + endpoints +
   eps-range probe + frozen cache key; `dp_ledger` `>= 0`.
5. Orchestration + artifact `dps-marginal/v3` (carrier metadata + recorded tuple +
   source boundary); `dp.py`, `snapshot.py`, `_checks_dp.py`.
6. Flag artifact/generation semantics in `_sample.py`.
7. Remove `dp_normalize.py` once the property suite subsumes its matrix.
8. Docs: `_spec.py`/`_global_settings.py`, claim-copy tests, CHANGELOG,
   `what-we-cannot-prove.md` (carrier + certified-adapter claim + §3.7 exclusions).
9. CLI + platform wiring against `column_schema` (ROADMAP item 2, next cycle).

Gates per phase: dennis (Opus) then Codex; Opus may take the hardest carrier/adapter/
orchestration build given DPS difficulty.

**Carry-forward from the phase-5 review (dennis M1) — undeclared-column omission.**
The DataFrame fit path (`dataframe_to_carrier_table`) fits exactly the columns named
in `column_schema` and silently ignores any extra frame column; only a schema column
absent from the frame fails closed (`dp_adapter_missing_column`). This is
spec-sanctioned (§3.5, "extra frame columns ignored") and is NOT a DP leak (nothing
about an omitted column is released), but it removes the old API's full-coverage safety
net, so a caller could believe a column is DP-protected when it was never in the
schema. The direct-`CarrierTable` path does not have this gap (`sanitize_carrier_table`
requires `set(columns) == set(schema)`). The CLI + platform seam (item 9) MUST require
explicit acknowledgement of undeclared frame columns rather than dropping them silently,
and the customer docs must state the omission behavior.

## 9. Resolved open questions

1. Datetime/timedelta null-only, reject before unboxing. 2. Exact dependency tuples
incl Python minor, every tuple tested (§3.8). 3. `number`/`flag`/`text` sufficient for
v1 after the flag fix + closed pair table + full flag path (§3.4); no fixed-vocabulary
categorical now. 4. Accept `epsilon_total >= 0`; requested ceilings strictly positive.
5. Adapter in-engine, in the end-to-end claim.

## 10. Decisions to surface to Cam (customer-facing, non-blocking)

### 10a. What we advertise (the claim strength)

- **(default, built here)** Keep the end-to-end DataFrame-row DP claim with the
  adapter certified as a stability-1 transformation under the exact-version gate,
  carrying the §3.7 residual exclusions. Most customer-friendly, strictly stronger
  than today, pandas mitigated to one gated tested component.
- **(stricter)** State DP only over a canonical `CarrierTable`; DataFrame fits are
  labeled convenience and cannot advertise row-level DP unless the caller supplies a
  canonical carrier.

Building the default; the carrier-level claim is the clean core, so we can tighten
later without a rebuild. Flag if you want the stricter public claim from v1.

### 10b. Certified-platform scope (round-4 HIGH)

The DP guarantee is only honest on a platform we actually test, and CI is Ubuntu-only.

- **(default, built here)** Certify **Linux / x86-64 / CPython / glibc only** for v1;
  fit fails closed (`dp_platform_uncertified`) on any other OS/arch/interpreter. The
  DP feature is unavailable off that platform rather than silently unverified. Minimal,
  honest, fastest to GA, fully reversible (widen later).
- **(broader)** Build a full multi-OS / multi-arch certification manifest with
  per-wheel hashes and a CI matrix across Linux/macOS/Windows and x86-64/ARM, so DP
  runs verified on more platforms from v1. Heavier machinery; more CI cost.

Building the default (Linux/x86-64 fail-closed). This matches where the engine is
actually tested today and can be broadened when a customer needs macOS/Windows/ARM DP.
Flag if you want the broader platform manifest in v1.
