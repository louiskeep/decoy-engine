# DPS established-library survey

**Date:** 2026-07-22
**Purpose:** survey vetted, established differential-privacy libraries so the
next DPS implementation guide adopts/wraps proven mechanisms instead of
repeating the home-grown categorical math that got `feat/dps-marginal-dp`
and `feat/dps-option-a` Codex-blocked twice. Research only; no engine code
changed by this document.

**Standing rule this answers to:** we do not roll our own DP math (Cam,
2026-07-21, memory `decoy-tq-authorization` / `survey-established-sources-
before-complex-work`). Both blocks trace to exactly that: a formally broken
categorical release built without checking what OpenDP/SmartNoise already
solved.

## 1. What we built, and precisely what is unsound

Branch `feat/dps-option-a` @ `fafbb75` (worktree
`decoy-engine/.claude/worktrees/agent-a2fad920ba2fde45c`), on top of
`feat/dps-marginal-dp` @ `0afb290`. Four modules:

- **`quality/dp_budget.py`** -- `PrivacyBudget`: accumulates `(label,
  epsilon, delta, mechanism)` charges and sums them (Dwork & Roth Thm 3.16,
  basic sequential composition). Correct arithmetic, hand-rolled bookkeeping.
- **`quality/dp.py::apply_dp_noise`** -- takes an exact `distribution-
  snapshot/v1` (the `decoy fit` artifact) and returns a noised copy:
  - Numeric: Laplace noise on histogram bin counts, `scale = 1/epsilon`,
    exact quantiles/mean/std stripped, min/max collapsed to bin edges.
    Sound in isolation *if* bin edges come from a caller-declared domain
    (`dp_mode` + `numeric_domains`, DPS-1).
  - Categorical: **stable-histogram / propose-test-release** pattern
    (Korolova et al. 2009; Dwork & Roth Sec. 3). Each `top_values` entry
    gets Laplace noise; a label survives only if its noised count clears
    `tau = 1 + ceil((1/epsilon) * ln(1 / (2*delta)))`; below-threshold mass
    folds into `other_count`.
  - Row/column scalars (`row_count`, `null_count`, `distinct_count`, etc.)
    noised individually and charged individually.
- **`plan/_checks_dp.py`** -- compile-time consume-side gate:
  `check_dp_categorical_unsupported` (hard-rejects categorical under
  `global_settings.dp`), `check_dp_generate_contract` (rejects
  `allow_real_categories`/`high_cardinality` under `dp`),
  `check_dp_snapshot_provenance` (fail-closed allow-list: only
  `numeric` + `support_origin: caller` passes; enforces declared
  `(epsilon, delta)` ceiling against artifacts' actual spend, deduped by
  content hash).
- **`generation/synthesize.py::generate_tables`** -- the public,
  `__all__`-exported entrypoint that actually produces rows. It normalizes
  the seed and topo-sorts tables; it does **not** import or call anything
  from `plan/_checks_dp.py`. Confirmed by reading the function: the only
  validation it runs is `_normalize_job_seed_int`, and its own docstring
  says it accepts "validated... **or unvalidated** dicts (V1-parity
  callers)". The DP gate lives entirely in `compile_plan` /
  `run_config_only_checks`, one layer up, and nothing forces a caller
  through that layer.

### The 7 Codex findings, sorted by whether a DP library can fix them

| # | Finding | Nature |
|---|---|---|
| F1 | All-null categorical column bypasses the fit-time DP-eligibility fence (`kind:"empty"` skips `_stats_for`) | fit-time eligibility gate: **our** plumbing; shape-invariance is library-assisted |
| F2 | Numeric/categorical artifact *shape* (kind, budget metadata, generate-success) differs between an all-null and a finite neighbor | **math-adjacent**, library-fixable by construction |
| F3 | `generate_tables` bypasses every DP gate (raw config in, no provenance/budget check) | **architecture, ours** -- gate in wrong layer |
| F4 | No compile-to-runtime pinning; artifact re-read by path, TOCTOU | **architecture, ours** -- same refactor as F3 |
| F5 | Content-hash dedup lets two independent fits collide (rounded output) and undercharge the budget | **math-adjacent**, library-fixable by construction |
| F6 | `dp: {}` (falsy-but-present) treated as "DP absent," fails open | **config-schema bug, ours** |
| F7 | CHANGELOG/docs overclaim (categorical "would be" DP, "provably" same object, etc.) | **docs, ours** -- follows from F3/F4/F5 |

Only F2 and F5 are genuine DP-mechanism problems a vetted library removes by
construction. F1 is hybrid (the eligibility gate is ours; the shape
invariance a domain-first library API gives us helps close it). F3/F4/F6/F7
are Decoy's own layering and config-validation bugs -- no DP library, however
good, decides which of our Python functions gets called, pins an artifact
digest into our Plan object, or fixes our JSON-truthiness bug. Call this out
plainly to whoever builds next: **adopting a library is necessary but not
sufficient. F3/F4/F6 need a Decoy-side refactor regardless of library
choice.**

## 2. Library survey

### OpenDP (core)

- **What it provides:** modular Domain/Metric/Measurement/Transformation
  primitives in a Rust core with Python (and R) bindings. Measurements
  include Laplace, Gaussian, discrete/geometric noise, and -- directly
  relevant to our categorical break -- `make_count_by` (histogram over an
  **unknown** category set) chained with a thresholded-Laplace measurement
  (the "Thresholded Laplace Mechanism," OpenDP's name for propose-test-
  release / stable histogram; older docs call the same idea `make_base_ptr`
  -- confirm the current exported name against the pinned version before
  wiring, API naming has moved across 0.9 to current 0.15.x). `make_count_by_
  categories` covers the KNOWN-category-list case. Composition combinators
  (`opendp.combinators`: sequential, adaptive, fully-adaptive/odometer) track
  a `Measurement`'s accumulated privacy loss as a typed property of the
  composed object, not a hand-summed list.
- **Maturity/maintenance:** actively maintained. Latest release 0.15.1
  (2026-05-29), 951 commits on main, two named maintainers with a
  committed response SLA, quarterly health-check cadence. Originated at
  Harvard's IQSS; used as the reference implementation the U.S. Census
  Bureau and others cite.
- **License:** MIT. Compatible with Decoy's Apache-2.0.
- **Python usability:** `pip install opendp`; ships prebuilt wheels
  (manylinux/macOS/Windows) so no local Rust toolchain needed at install
  time. Confirmed platform/version support as of 0.9.2-era docs was Python
  3.8-3.11; verify 3.12 wheel coverage against Decoy's `requires-python
  >=3.10` / classifier matrix (3.10-3.12) before pinning, since that specific
  claim came from an older doc snapshot in this research pass.
- **Solves:** F2 and F5 by construction (domain-declared-first API design;
  typed composition object as release identity). Also gives a formally
  reviewed threshold mechanism to replace our round-then-compare tau check
  (fixes the "rounding drops effective threshold below delta" defect
  directly, since the library's threshold test operates in the mechanism's
  own proven arithmetic, not a Python `round()` we bolted on afterward).

### SmartNoise (SmartNoise-SDK / SmartNoise-Synth)

- **What it provides:** Microsoft + OpenDP joint project. Two halves:
  SmartNoise-SQL (DP query rewriting over SQL, not our use case) and
  SmartNoise-Synth (`pip install smartnoise-synth`), which wraps several
  published synthesizers: **MST** (McKenna et al., "Winning the NIST
  Contest," 2021), **AIM** (McKenna, Miklau, Sheldon, "AIM: An Adaptive and
  Iterative Mechanism for Differentially Private Synthetic Data," 2022),
  MWEM, PAC-Synth, plus GAN-based ones (DP-CTGAN, PATE-GAN, PATE-CTGAN,
  QUAIL) that need PyTorch.
- **Maturity/maintenance:** actively maintained; latest `smartnoise-synth`
  1.0.8 released 2026-04-15, Python 3.10-3.14 support, MIT license, 730+
  commits, not archived.
- **License:** MIT for the SmartNoise packages themselves. Dependency chain
  for the marginal synthesizers (MST/AIM/PrivBayes-class, the ones we'd
  actually want): `mbi` (Apache-2.0, Ryan McKenna's Private-PGM marginal-
  inference library -- the shared estimator MST and AIM both build on),
  `pac-synth` (Rust crate/wheel from the SmartNoise project; license not
  independently confirmed in this pass -- verify before depending), `opacus`
  (Apache-2.0, Meta's DP-SGD library) and `disjoint-set` (MIT). `torch` is
  declared **optional** in `smartnoise-synth`'s own `pyproject.toml`, but
  `opacus` itself is a PyTorch library -- whether it can actually import
  without torch present needs a real dependency-resolution check, not an
  assumption, before anyone scopes MST/AIM as "torch-free."
- **Solves:** the joint/multi-column-correlation need (DPS-4 in our own
  numbering) -- MST and AIM are the established, benchmarked answer for
  "release correlated marginals across several columns," which is exactly
  what a hand-rolled PrivBayes would have tried to reinvent. Does not solve
  any of the 7 findings directly (those are all in the single-column
  marginal path); relevant to the *next* milestone, not the current block.

### Google differential-privacy / dp-accounting / PipelineDP

- **What it provides:** `google/differential-privacy` is a monorepo: C++/Go/
  Java DP building blocks, Privacy-on-Beam, and a standalone **pure-Python**
  `dp_accounting` package (`DpEvent` classes describing Laplace/Gaussian/
  subsampled/composed mechanisms, `PrivacyAccountant` classes that ingest
  events and report composed `(epsilon, delta)`; supports both Privacy Loss
  Distribution (PLD, tight) and RDP accounting, not just basic sequential
  composition). `PipelineDP` (with OpenMined) targets Beam/Spark pipeline-
  scale aggregation -- a different problem than Decoy's in-process,
  row-level generation.
- **Maturity/maintenance:** mature, Google-maintained, "used in research,
  experimental, and production" per its own README.
- **License:** Apache-2.0 across the repo -- same license Decoy ships
  under, no compatibility question at all.
- **Python usability:** `dp_accounting` alone is pure Python, no native
  build step, much lighter than pulling the whole monorepo or PipelineDP's
  Beam dependency.
- **Solves:** F5 as an alternative to OpenDP's own composition combinators
  -- `dp_accounting` gives a typed, tested accountant object as the release
  ledger, and (bonus, not required to close the finding) can compose more
  tightly than the basic sequential bound our `PrivacyBudget` currently
  uses, which matters if per-column epsilon costs start adding up across
  wide tables.

### IBM diffprivlib

- **What it provides:** general-purpose DP toolkit oriented around
  scikit-learn-style models (DP logistic regression, DP random forest, DP
  k-means) and NumPy-style DP statistics (mean, histogram, quantiles) built
  on its own Laplace/Gaussian/exponential mechanism classes.
- **Maturity/maintenance:** mature (since 2019), actively maintained
  (recent releases track scikit-learn/NumPy compatibility), MIT license.
- **Fit for Decoy:** weakest fit of the group. It is aimed at "train a
  model differentially privately," not "release a distribution snapshot
  that a downstream sampler consumes" -- our actual shape. Its
  histogram/quantile primitives are usable as a secondary reference but
  don't map onto our unknown-category-set problem as directly as OpenDP's
  `make_count_by` + threshold pairing. Not recommended as a dependency for
  this gap; keep as a canonical-methodology citation only if useful.

### Tumult Analytics

- **What it provides:** a session/query API (`Session.from_dataframe(...).
  evaluate(query)`) for aggregate DP queries at scale, built on Tumult Core,
  in production at the Census Bureau, IRS, and Wikimedia.
- **Maturity/maintenance:** active. Latest release 0.21.0 (2026-07-01),
  1,110+ commits. The Tumult Labs team joined LinkedIn and the project now
  lives under the `opendp` GitHub org -- worth knowing the org home moved,
  not a sign of abandonment (release cadence says otherwise).
- **License:** Apache-2.0 (code), CC-BY-SA-4.0 (docs). Compatible.
- **Fit for Decoy:** requires Spark as a runtime dependency and is designed
  for distributed, large-scale aggregate-query workloads. Decoy's DPS path
  is in-process, row-level, single-machine. Pulling Spark in for a
  per-column histogram is disproportionate. Valuable as a **reference** for
  how a mature team structures a query/session API and enforces composition
  at the API boundary (their `Session` object *is* the accountant, you
  cannot evaluate a query without going through it) -- that pattern maps
  onto how we should fix F3/F4 (generation should have to go through an
  object that already proves DP-ness, not a raw config dict), even though
  we would not adopt Tumult itself.

### Canonical references already cited correctly in our code

Our own docstrings already cite the right sources -- this is good sign of
the survey-first instinct existing at the mechanism level, just not carried
through to the engineering layer:

- Dwork & Roth, *The Algorithmic Foundations of Differential Privacy*
  (2014) -- Thm 3.16 (sequential composition), Sec. 3 (thresholding /
  propose-test-release), Prop. 2.1 (post-processing invariance).
- Dwork, McSherry, Nissim, Smith, "Calibrating Noise to Sensitivity in
  Private Data Analysis" (TCC 2006) -- the Laplace mechanism.
- Korolova, Kenthapadi, Mishra, Ntoulas, "Releasing Search Queries and
  Clicks Privately" (WWW 2009) -- stable-histogram / propose-test-release
  for an unknown category set.
- Zhang et al., "PrivBayes: Private Data Release via Bayesian Networks"
  (2014/2017) -- conditional-marginal joint synthesis; superseded in
  practice by MST/AIM below for tabular synthesis benchmarks.
- McKenna, Sheldon, Miklau, "Winning the NIST Contest: A scalable and
  general approach to differentially private synthetic data" (2021) -- MST.
- McKenna, Miklau, Sheldon, "AIM: An Adaptive and Iterative Mechanism for
  Differentially Private Synthetic Data" (2022) -- AIM.

These are the right papers; the gap was never citation, it was
implementation from scratch against them instead of using a library that
already implements and has been externally audited against the same papers.

## 3. Mapping table: problem -> mechanism -> library -> how to wrap -> license

| Problem / need | Established mechanism | Library | How Decoy would wrap it | License |
|---|---|---|---|---|
| Categorical marginal release (rank leakage, rounding-before-threshold) | Thresholded Laplace / propose-test-release stable histogram | OpenDP (`make_count_by` + threshold measurement) | `quality/dp.py`'s categorical branch becomes a thin adapter: build the OpenDP measurement from the snapshot's category list, call it, read back `{value, count}` pairs already in the mechanism's own output order (never re-sort by true count) | MIT |
| Rare-category suppression / `other_count` | Post-processing of two independently-charged DP releases (Dwork-Roth Prop. 2.1), not "sum the below-threshold noisy counts back in" | OpenDP (threshold measurement drops unreleased keys; a separate `make_count`-style scalar release gives total row count) | Compute `other_count = noised_total - sum(kept noised counts)` as post-processing, charged for the total-count release only; stop folding sub-threshold noisy values into a hybrid bucket | MIT |
| Budget composition + accounting | Sequential / RDP / PLD composition with a typed release-identity object | OpenDP combinators, or Google `dp_accounting` (`DpEvent` + `PrivacyAccountant`) | Replace `PrivacyBudget`'s manual `_charges` list with a real accountant object minted once per `decoy fit` invocation; that object (not a content hash of output bytes) is the release identity, closing F5 at the root | Apache-2.0 (Google) / MIT (OpenDP) |
| Declared-epsilon enforcement | N/A -- comparing a config-declared ceiling to an accountant's total is policy, not a DP mechanism | N/A (build on top of whichever accountant is adopted) | Keep `check_dp_snapshot_provenance`'s fail-closed compare, but read the ceiling check against the accountant object's `.epsilon`/`.delta` properties instead of a hand-parsed JSON `dp` block once the artifact schema carries one | n/a |
| Numeric support (fixed-domain histogram, shape invariance) | Domain-first (declare bounds before touching data) histogram + Laplace | OpenDP (`Domain`/`Bounds` declared before the `Measurement` is invoked; same chain runs regardless of whether the column is empty) | Fit-time: construct the numeric domain/measurement from the caller's `numeric_domains` entry unconditionally; always invoke it even on all-null/all-inf data so shape can't diverge (closes F2) | MIT |
| Joint / multi-column correlations | MST (spanning-tree + Private-PGM) / AIM (adaptive measurement selection) | SmartNoise-Synth (`mbi`-backed MST/AIM, skip the GAN/torch synthesizers) | Future DPS-4 milestone: fit MST or AIM over the declared numeric/categorical domains, ship the synthesizer's own noised marginals rather than hand-building PrivBayes' conditional structure | MIT (SmartNoise) / Apache-2.0 (`mbi`) |

## 4. Recommendation

**Adopt OpenDP (core) as the mechanism/accounting engine for marginal DP,
wrapped, not vendored.** Concretely:

1. `quality/dp.py` becomes an adapter over OpenDP `Measurement` objects:
   construct the categorical threshold measurement and the numeric
   Laplace-over-fixed-domain measurement from our snapshot schema, invoke
   them, translate the typed output back into our `distribution-snapshot/v1`
   shape. Delete `_stable_histogram_threshold` and the manual
   round-then-compare logic -- that exact code is what got us blocked twice.
2. `quality/dp_budget.py`'s `PrivacyBudget` is replaced (or backed) by
   OpenDP's composition combinator, or Google's `dp_accounting` if we want
   a pure-Python accountant decoupled from the Rust core. Either gives a
   typed release-identity object minted at fit time, closing F5 by
   construction (no more content-hash-as-identity).
3. Reserve SmartNoise-Synth's MST/AIM for the joint/multi-column milestone
   (DPS-4) only -- do not pull it in for the current numeric+categorical
   scope, and when we do, install it without the torch-dependent GAN
   synthesizers.

**Wrap, don't vendor.** OpenDP's core has published, reviewed privacy-proof
machinery; copying its mechanism logic into decoy-engine (even faithfully)
would reproduce the "confident-but-unaudited DP math" failure mode Codex
caught, just with better citations. The adapter layer owns translation
to/from our snapshot schema; OpenDP owns the actual noise, threshold test,
and composition proof.

**What library adoption does NOT fix (engineering, not math):**

- **F3 (gate in wrong layer)** -- no DP library decides which of Decoy's own
  Python functions a caller invokes. This needs the architecture change
  already identified in the parked decision: `generate_tables` must consume
  a compiled, DP-verified, pinned Plan object rather than a raw config
  dict, OR re-run the preflight inside `generate_tables` itself (weaker,
  per the original analysis).
- **F4 (TOCTOU / no pinning)** -- same refactor as F3; a library can prove
  a measurement's privacy loss, but nothing prevents an on-disk artifact
  from changing between compile-time verification and runtime read unless
  Decoy pins the verified digest/parsed object into the Plan itself.
- **F6 (`dp: {}` fails open)** -- a config-schema bug in
  `_dp_settings`/`_checks_dp.py`; distinguishing "absent" from
  "present-but-invalid" is ours to fix regardless of mechanism library.
- **F7 (docs overclaim)** -- follows mechanically from fixing F3/F4/F5;
  a barry pass once the code changes, not a research question.

So: adopting OpenDP (+ optionally `dp_accounting`) makes F2 and F5
unreachable by construction, and gives F1's eligibility gate a
shape-invariant foundation to sit on. F3, F4, F6, F7 still need the
Decoy-side architecture and doc work the parked decision already scoped --
no library changes that math.

## 5. Licensing, packaging, and dependency-weight notes

- Decoy engine ships Apache-2.0 (`pyproject.toml`). Every library above is
  MIT or Apache-2.0 -- no copyleft conflicts, no relicensing question.
- Current runtime dependency set is already non-trivial: pandas, polars,
  pyarrow, duckdb, boto3, google-cloud-storage. Adding OpenDP (a compiled
  Rust core with prebuilt wheels) is a comparatively small addition next to
  what's already shipped -- it is not pure-Python, but it is far lighter
  than a PyTorch-class dependency.
- **Do not** pull in `smartnoise-synth`'s GAN synthesizers (DP-CTGAN,
  PATE-GAN, PATE-CTGAN, QUAIL) for the current scope -- they need PyTorch
  via `opacus`, a heavy native dependency wholly unjustified for a
  single-column marginal release. If/when MST or AIM is adopted for the
  joint-correlation milestone, verify at that time whether `opacus` can
  resolve without pulling `torch` transitively, or depend on `mbi` +
  MST/AIM logic directly rather than the full `smartnoise-synth` package.
- `pac-synth`'s exact license was not independently confirmed in this
  research pass (time-boxed web search did not surface its LICENSE file
  directly) -- verify before depending on any smartnoise-synth code path
  that pulls it in.
- Decoy is pre-GA; the Apache-2.0 declaration in `pyproject.toml` is
  already in place, so there is no separate "wait for the license flip"
  blocker for this specific dependency decision (that gate applies to
  making the *repo* public, not to what licenses new dependencies may
  carry).
- Verify OpenDP's published wheel matrix covers Decoy's full supported
  Python range (3.10, 3.11, 3.12 per `pyproject.toml` classifiers) at
  whatever version gets pinned -- the platform-support figures found in
  this pass came from an older doc snapshot (0.9.2-era: Python 3.8-3.11)
  and should be re-checked against the current 0.15.x release notes before
  the implementation guide locks a version.

## Sources

- [OpenDP GitHub](https://github.com/opendp/opendp)
- [OpenDP docs](https://docs.opendp.org/)
- [Announcing OpenDP Library 0.9](https://opendp.org/2024/04/16/announcing-the-opendp-library-0-9/)
- [OpenDP installation docs (0.9.2)](https://docs.opendp.org/en/v0.9.2/user/installation.html)
- [OpenDP transformations user guide](https://docs.opendp.org/en/stable/api/user-guide/transformations/index.html)
- [SmartNoise SDK GitHub](https://github.com/opendp/smartnoise-sdk)
- [smartnoise-synth on PyPI](https://pypi.org/project/smartnoise-synth/)
- [SmartNoise MST synthesizer docs](https://docs.smartnoise.org/synth/synthesizers/mst.html)
- [SmartNoise AIM synthesizer docs](https://docs.smartnoise.org/synth/synthesizers/aim.html)
- [private-pgm / mbi GitHub](https://github.com/ryan112358/private-pgm)
- [google/differential-privacy GitHub](https://github.com/google/differential-privacy)
- [dp_accounting source](https://github.com/google/differential-privacy/tree/main/python/dp_accounting)
- [pipeline-dp on Libraries.io](https://libraries.io/pypi/pipeline-dp)
- [IBM diffprivlib GitHub](https://github.com/IBM/differential-privacy-library)
- [Diffprivlib docs](https://diffprivlib.readthedocs.io/)
- [Tumult Analytics GitHub (opendp org)](https://github.com/opendp/tumult-analytics)
- [Tumult Labs open-source announcement](https://www.tmlt.io/resources/tumult-analytics-open-source)
