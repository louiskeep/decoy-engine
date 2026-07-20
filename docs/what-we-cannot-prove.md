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

The one formal mechanism is `decoy fit --epsilon` (`quality/dp.py`) feeding a
`mode: generate` statistical column. As of DPS-1/2/3, generated **marginals**
ARE (epsilon, delta)-differentially private by post-processing of the DP
snapshot — PROVIDED all three preconditions below hold; each is enforced
mechanically, not just documented:

- (a) **Data-independent support.** The snapshot was fit with `dp_mode=True`
  and a caller-supplied `numeric_domains` entry for every numeric column
  (`quality/snapshot.py`, DPS-1) — fixed bin ranges, not the real min/max —
  and its categorical label set is THRESHOLD-RELEASED: a label survives only
  if its noised count clears tau = 1 + ceil((1/epsilon) * ln(1/(2*delta)))
  (the stable-histogram / propose-test-release pattern), with the rest
  folded into `other_count`. Without `dp_mode` at fit time, the OLD caveat
  still applies in full: bin edges and category labels are data-dependent
  and this section's guarantee does not hold — `dp_mode` is opt-in, so
  every pre-existing `decoy fit --epsilon` invocation keeps its prior,
  narrower scope unless the caller adopts it.
- (b) **The composed budget is `dp.epsilon_total`/`dp.delta_total`.** Every
  noised release in the snapshot — row_count, each column's null/non-null/
  distinct counts, and each column's histogram (numeric bins, the
  threshold-released categorical set, datetime year bins, freetext length
  bins) — is charged to a `PrivacyBudget` and composed SEQUENTIALLY (Dwork &
  Roth, *Algorithmic Foundations of DP*, Thm 3.16: sum of epsilons, sum of
  deltas). The single per-histogram `epsilon`/`delta` figures in the `dp`
  block are not the whole story; `epsilon_total`/`delta_total` are.
- (c) **Generation reads only the artifact.** Post-processing immunity
  requires the sampler never re-touch the raw source frame; this is a
  regression-locked contract, not an inference —
  `test_generation_consumes_only_the_snapshot`
  (`tests/unit/generation/test_generate_dp_contract.py`) fails the build if
  a future refactor makes the sampler reach for raw data.
- `high_cardinality: true` (HC-5) and `allow_real_categories: true` are
  BOTH anti-DP (they retain/release real vocabulary) and are hard-rejected
  at compile time when `global_settings.dp` is set
  (`plan._checks_dp.check_dp_generate_contract`) — a config cannot silently
  combine them with a DP declaration. Outside a `dp`-declared pipeline,
  `high_cardinality: true` (e.g. an ICD-10-CM/NDC/HCPCS code field)
  intentionally retains its FULL observed vocabulary with no top-K
  collapse, so its snapshot artifact exposes every distinct code AND
  rare-code presence/absence at fit time — treat that artifact with the
  same care as a raw extract of that column, on top of the
  `allow_real_categories` gate it already requires.
- Cross-column JOINT structure is NOT covered by any of the above: joint
  contingency tables are rejected under `--epsilon` entirely (no composition
  accounting in v1), and a preserved marginal says nothing about preserved
  correlation. Joint-distribution DP (PrivBayes-style) is a separate,
  larger, not-yet-built effort.
- MASKED output still carries no epsilon (masking is a deterministic
  transform, entirely outside this mechanism).

If your use case requires a formal privacy guarantee over MASKED data, or
over generated JOINT structure, Decoy alone does not supply it.

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
