# What Decoy does not prove

Decoy is a practical de-identification and synthetic-data tool. It applies
recognized transformation primitives (masking, hashing, format-preserving
encryption, generalization, suppression, synthesis) and preserves structural
properties like foreign keys and determinism. This page is the honest boundary:
the things Decoy does NOT prove, so you do not rely on a guarantee it does not
make.

## It provides a formal privacy guarantee in exactly one place, and nowhere else

MASKED OUTPUTS carry no differential privacy, no epsilon, and no
mathematical bound on re-identification risk. The `storm` profiler reports
heuristic re-identification-risk signals to help you assess a dataset; those
are diagnostics, not a proof.

The one formal mechanism is `quality.dp.fit_dp_snapshot`, which produces a
`dps-marginal/v2` release consumed by a `type: statistical` generate column
under a `global_settings.dp` declaration. Each column's privacy loss is
certified by OpenDP's own privacy map (`Measurement.map`, `opendp==0.15.1`),
and the fit-wide loss is composed by Google's `dp_accounting` (0.6.0) PLD
composition over those per-column certificates. The composition uses a
dominating-pair representation of each certified `(epsilon, delta)`, so the
reported total is a valid upper bound rather than the tightest achievable
one. This is a CONDITIONAL claim, not a blanket one: do not read "Decoy
supports DP" as "every statistical column in every run is DP" -- most are
not, unless the column's snapshot is a verified `dps-marginal/v2` release
consumed by a compiled, DP-verified Plan.

> For a DP fit whose declared fit-wide privacy loss is `(epsilon, delta)`,
> Decoy's released single-column numeric and categorical marginals, and
> synthetic columns generated solely as post-processing of a DP-verified
> pinned Plan, are covered by that fit's approximate `(epsilon, delta)`
> differential privacy guarantee under add-or-remove-one-row adjacency.
>
> This guarantee is marginal only. It does not cover joint distributions,
> cross-column correlations, conditional sampling, masked outputs, non-DP
> snapshots, or forged artifacts.
>
> When several independent release IDs are consumed, their privacy losses
> compose; repeated references to the same release ID are charged once, and
> conflicting artifacts carrying one release ID are rejected.

**What is covered.** A statistical generate column's marginal carries the
declared `(epsilon, delta)` guarantee only when its snapshot was produced by
`fit_dp_snapshot` for that specific column, and the compiled Plan's
`dp_verification` receipt (reproduced from the pinned snapshot bytes, never
trusted from the artifact's own claim alone) certifies it:

- **Numeric columns**, released as a fixed-bin-count histogram over a
  caller-declared domain (`numeric_domains`), via `make_find_bin >>
  then_count_by_categories >> then_laplace`.
- **Categorical columns**, released as a thresholded top-label set plus a
  non-null total, via `make_count_by >> then_laplace_threshold` (grouped)
  and `make_count >> then_laplace` (total).
- **Row count**, released once per fit via `make_count >> then_laplace`,
  shared across every column in that fit.

**What is NOT covered, ever:**

- **Cross-column correlation and conditional synthesis.** The protected
  release scope is single-column marginals only. Joint contingency tables,
  `condition_on` sampling, and any cross-column structure carry no privacy
  accounting; a preserved marginal says nothing about preserved
  correlation. Joint-distribution DP (PrivBayes/MST/AIM-style mechanisms) is
  a separate, larger, not-yet-built effort, out of scope for this
  single-column-marginals build.
- **Masked output.** Still carries no epsilon (masking is a deterministic
  transform, entirely outside this mechanism).
- **Datetime and freetext columns.** Not eligible for a DP release; only
  numeric and categorical kinds are ever accepted by the compile-time
  provenance check.
- **`high_cardinality: true` and `allow_real_categories: true` columns.**
  Both retain or release real, non-DP vocabulary and are anti-DP by
  construction; a DP-declared pipeline hard-rejects either at compile time.
- **A Plan embedding an exact, non-DP snapshot inherits that snapshot's
  full sensitivity.** Pinning a snapshot's bytes into the Plan (so
  generation never rereads a path) is required for the generation
  capability regardless of whether that snapshot is DP-fit; an exact
  snapshot referenced by a non-DP-declared pipeline carries no privacy
  reduction just because it is now embedded in a Plan file.
- **Numeric domains and column kinds are public metadata**, declared by the
  caller at fit time and recorded in the artifact -- they cost no privacy
  budget, but they are not protected either; do not treat them as secret.
- **Categorical label discovery consumes privacy budget.** Which labels
  survive the release's threshold is itself a private-data-dependent
  outcome, budgeted as part of the fit's query schedule, not a free
  side-channel.

**What is enforced mechanically versus what is an operator precondition.**

- **`OpenDpReleaseSession` is the sole OpenDP call site.** It enforces a
  fixed query schedule (refuses an unscheduled or duplicate release,
  refuses a loss report before the schedule completes) and asserts every
  recorded certificate equals `measurement.map(1)` on the object actually
  invoked, never the calibration target. The fixed schedule is enforced by
  Decoy, not by the DP library: an OpenDP-native compositor that would
  itself refuse an unscheduled query is unavailable in this build (it
  requires a Polars version Decoy does not run -- see the dependency
  decision below). A defect in Decoy's release session could therefore
  under-count a release, and no library would catch it; the mitigations are
  the single call site, refusal of unscheduled/repeated queries, and the
  certificate-count assertion, not a library-enforced backstop.
- **DP generation requires a DP-verified pinned Plan.** `generate_tables`
  accepts only a compiled `Plan`; a categorical column's `allow_real_
  categories` consent gate is bypassed only when the PLAN COMPILER (never
  the artifact's own `dp` key) has verified the pinned snapshot as a
  `dps-marginal/v2` release for that specific column. Successful
  compilation alone does not protect a caller who bypasses the compiled
  Plan and calls internal generation helpers directly with unvalidated
  data.
- **Independent release IDs compose; the same ID is charged once.**
  Release IDs are privacy-ledger identities, never derived from content,
  timestamps, paths, or row counts. Distinct release IDs referenced by a
  pipeline compose via basic sequential composition; the same release ID
  referenced by many columns is charged once, keyed by ID, not by content
  digest. A release ID reused with a DIFFERENT digest is rejected as a
  conflicting artifact (`dp_release_id_conflict`), never silently
  re-charged or silently accepted as a duplicate. A content hash never
  identifies a privacy release by itself.
- **Serialized Plan files are integrity-checked internally, not
  authenticated against a hostile replacement.** `plan_from_yaml`
  recomputes each embedded snapshot's digest from its pinned bytes and
  rejects a mismatch, and recomputes the `dp_verification` receipt from
  those same revalidated bytes rather than trusting the serialized receipt
  verbatim. This catches accidental corruption and a forged receipt on an
  otherwise-genuine pinned artifact; it does not authenticate that the
  Plan file as a whole came from a trusted compiler, since nothing signs
  the file itself.
- **The consumed snapshot is trusted to be genuine.** The provenance check
  reads the snapshot JSON at face value: it defends against honest
  misconfiguration (pointing a DP-declared pipeline at a non-DP-fit
  artifact, or an artifact whose internal schedule doesn't match its own
  declared columns), not against a forged or hand-edited artifact. A DP
  artifact's recorded `epsilon_total` and `delta_total` are self-declared;
  Decoy verifies internal consistency and release-ID uniqueness, but a
  hand-edited artifact can understate what it actually spent. The
  compile-time ceiling check is a policy control over artifacts Decoy
  itself produced, not a defense against a caller who edits their own
  artifacts.
- **DP artifacts reveal exactly the labels and counts deliberately released
  by OpenDP.** The artifact never carries exact row counts, exact distinct
  counts, suppressed label names, suppressed noisy counts, per-query
  certificate breakdowns, or an RNG seed.

**Snapshot bytes are read once and pinned, never reopened.** `compile_plan`
reads every referenced snapshot path exactly once, embeds the exact bytes
and a SHA-256 digest into the Plan, and neither `generate_tables` nor the
fidelity gate reopens a `snapshot_file` path afterward -- both consume the
pinned, parsed artifact from the compiled Plan. This closes a
read-time-of-check/read-time-of-use window where a file swapped between two
separate reads within one compilation, or between compile and generate,
could serve different bytes to different consumers; generation reads only
the pinned artifact.

**DP-fit output is not reproducible run to run, by design.** `fit_dp_snapshot`
draws its Laplace/threshold noise from OpenDP's own entropy, never from the
job seed: seeding the noise from material the config holder controls would
let them subtract it back out and recover the true counts, voiding the
guarantee. So each DP release draws fresh noise -- applying the mechanism
twice to identical data and the same fit parameters yields a different noisy
release every time. This is separate from Decoy's normal determinism
contract, not a weakening of it: once a specific release artifact exists and
is pinned into a compiled Plan, generating synthetic rows from it is fully
seed-reproducible as usual (the samplers are seeded; the release is just
weights). Do not expect byte-identical DP artifacts across repeated fits,
and do not treat the job seed as a privacy-relevant secret for the DP
mechanism, since it plays no part in the noise draw.

If your use case requires a formal privacy guarantee over MASKED data, over
generated JOINT or conditional structure, or over datetime or freetext
columns, Decoy alone does not supply it.

## It does not certify legal or regulatory compliance

Decoy ships configuration bundles named after regulations (for example a HIPAA
Safe Harbor bundle that targets the 18 identifier categories). These are
engineering aids that encode a common interpretation of an identifier set. They
are not a compliance certification, not legal advice, and not a determination
that any given output meets a regulation as applied to your data. Whether a
masked dataset satisfies HIPAA, GDPR, CCPA, or any other regime is a
determination for you and your counsel, considering your data, your context, and
the residual-risk analysis the regulation requires. Running a bundle named
`hipaa` does not by itself make a dataset HIPAA-compliant.

## It does not guarantee semantic correctness of free-text

Free-text redaction (`text_redact`) finds and replaces PII spans using
pattern-and-hint detectors. Detection is best-effort: it can miss an identifier
the detectors do not recognize (a false negative) or replace a span that was not
actually sensitive (a false positive). Decoy does not understand the meaning of
free text and does not guarantee that every identifier in a notes column has
been found, nor that the surviving text is semantically coherent. Treat
free-text output as reduced-risk, not as proven-clean, and review it where the
stakes warrant.

When NER-backed redaction is enabled (`ner: true`), the spaCy model version is
stamped into the compiled plan. If the installed model is updated between compile
time and run time, the engine raises `ner_model_version_mismatch` rather than
silently producing different redactions for the same config and seed. This guard
catches version drift; it does not widen the coverage guarantee above.

## It does not validate that your configuration matches your intent

Decoy validates a config against its schema and runs what the config says. It
does not know which columns in your data are actually sensitive. If a config
leaves a sensitive column on `passthrough`, or masks the wrong column, the run
will succeed and the output will leak. Use `storm` to find candidate PII and the
post-mask checks to look for residual identifiers, but the mapping from your
data's sensitivities to a correct config is yours to get right.

## The ML column classifier does not de-identify anything

The STORM LightGBM column classifier (lgbm-v1) DETECTS which semantic type
a column is likely to contain (SSN, email, ICD-10 code, etc.). It does not
mask, redact, transform, or remove any values. A column classified as `ssn`
still contains SSNs until a masking step explicitly transforms them.

The classifier is an advisory signal. It can have false negatives (missed PII
columns) and false positives (non-PII flagged as PII). The held-out evaluation
in `docs/v2/ml/lightgbm-report.json` documents the known error rates.

The model artifact carries no differential privacy guarantee. It was trained on
fully synthetic data and its weights do not encode any real PII cell values
(§B.4 of ml-benchmarking-and-privacy.md), but that is a training-data hygiene
property, not a DP guarantee over the model's outputs.

## Correlation through a value-changing mask: harness-side Cramers V (Phase 3c)

The engine quality module's joint metric (Total Variation Distance over the
joint contingency table) compares value-LABELED cells. A value-changing strategy
(`fpe`, `hash`, `code_set`, `joint_mask`) relabels the cells, so the source and
output crosstabs become disjoint and the TVD similarity collapses to 0.0 even
when the correlation structure is preserved perfectly. Measured directly: a
faithfully FPE-masked pair whose joint structure is identical to the source
scores 0.0 on TVD similarity, which is worse than a genuinely decorrelated pair
(approximately 0.34).

The engine TVD metric is NOT used for correlation checks on value-changing masked
pairs. The test-flight suite (Phase 3c, 2026-06-29) closes this gap for
categorical and low-cardinality column pairs via a harness-side relabel-invariant
statistic: Cramers V computed over the contingency COUNTS (not value labels).

How the Phase 3c metric works: Cramers V = sqrt(chi2 / (n * min(r-1, c-1))),
where chi2 is the Pearson chi-square statistic computed from the observed
contingency table vs expected counts under independence, n is the row count, and
r and c are the numbers of distinct values in each column. A bijective strategy
(fpe) maps every unique value in column A to a new unique value, and similarly
for column B. Because it is a bijection (no collisions, no merges), the COUNT of
every (A_val, B_val) pair is preserved exactly in the masked output. The
contingency table has the same cell counts; only the labels differ. Therefore
Cramers V of the output equals Cramers V of the source.

The harness asserts abs(V_out - V_src) <= tol for declared
`masked_correlations` pairs. An FPE pair that faithfully preserves the joint
structure passes this check (diff approximately 0). A pair where one column was
independently shuffled after masking fails it (V_out drops toward 0). Mutation
controls in `testflight/test_testflight_teeth.py` prove both directions.

Scope of the Phase 3c metric:
- Covered: categorical / low-cardinality column pairs where BOTH columns are
  masked by a bijective strategy (fpe, hash, or any strategy that remaps values
  without merging distinct source values). Cramers V is well-defined as long as
  each column has at least 2 unique non-null values.
- NOT covered: high-cardinality continuous (numeric) column pairs. Cramers V
  requires discrete categories; continuous data would need binning that
  introduces its own distortion. For continuous paired columns, rank correlation
  (Spearman) or a bin-based metric would be more appropriate; that extension is
  out of scope for Phase 3c.
- NOT covered: value-MERGING strategies (bucketize, geo_generalize, code_set
  when the target corpus has fewer values than the source). A merging strategy
  changes the contingency structure itself; the count preservation argument does
  not hold. Those pairs are checked via the distribution fidelity metric with
  strategy-aware policy bands (coarsen class).

Practical consequence: for any `masked_correlations` pair declared in a job
manifest, a green check does prove that the categorical association was preserved
through the value-changing mask. The TVD similarity metric is explicitly NOT
used to judge these pairs (it would falsely report 0.0 on a correct FPE run).

## FPE orphan-remap passes out-of-charset characters through (partial keys, and =False)

When `orphan_policy:remap` re-applies FPE to an orphan FK key, the covering hash
(`_covering_hash_to_charset` in `transforms/fpe.py`, fix #42) fires ONLY for keys
with ZERO in-charset characters: such an all-out-of-charset key is routed to a
deterministic in-charset string and is never emitted verbatim under the default
`preserve_separators=True`.

It does NOT cover a key with a MIX of in- and out-of-charset characters. Under
`=True`, the in-charset characters are permuted in place and every out-of-charset
character is passed through verbatim - the standard product-wide
`preserve_separators` behavior, where out-of-charset characters are treated as
separators by the user's charset choice. So a partial-out-of-charset orphan key
(e.g. `STATUS-1` under a `digits` charset) still leaks its out-of-charset
characters (`STATUS-`) in the clear. And with `preserve_separators: false`
explicitly set, FPE returns the whole value verbatim when ANY character is outside
the charset.

Mitigations for the partial and `=False` cases:
- Widen the charset to cover the orphan key's alphabet (e.g. `ALPHANUM` for
  uppercase identifiers) so every character is encrypted.
- Use `orphan_policy:fail` to reject orphan FK values entirely.
- Use `orphan_policy:preserve` if the source key is already non-sensitive.

## The test-flight value-changing-passthrough check has a low-alphabet blind spot

The acceptance test-flight's per-position FPE leak check (`testflight/_fk_remap.py`)
flags a column when any character position is emitted verbatim across rows at an
*informative* position - one whose source takes at least
`_FPE_MIN_INFORMATIVE_ALPHABET` (4) distinct characters. Positions with fewer
distinct source characters are excluded, because a low-entropy position that is
legitimately preserved (e.g. an NPI's leading digit, always 1 or 2, kept by the
checksum-aware FPE) is indistinguishable by retention fraction alone from one that
leaks. Consequence: a verbatim leak confined to positions with fewer than 4 distinct
source characters is NOT caught by the positional check. It is never a silent green -
an all-low-entropy column is surfaced as an explicit SKIP, and a mixed column
discloses "N low-entropy pos NOT leak-checked" in its status. No column in the
current jobs is exposed (the only all-low-entropy column, `orders.risk_flag`, is
protected by the complete-no-op set check and cannot partial-leak under a named
charset), but a custom-charset or mixed-low-alphabet column would fall in this gap.
Mitigation: cover the column's full alphabet with the charset (e.g. `ALPHANUM`), the
same fix as the orphan-remap case above.

## What it does do

To be clear about the other side: Decoy does give you deterministic,
reproducible masking; foreign-key and join preservation across tables; a catalog
of standard de-identification transforms; PII detection and risk profiling; and
synthetic-data generation. Those features are real and exercised by the test suite. This page exists so the strength of those features is not mistaken for guarantees Decoy does not make.
