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

The one formal mechanism is `decoy fit --epsilon` (`quality/dp.py`): the
distribution snapshot's COUNTS are released with per-count Laplace noise
(sensitivity 1, add/remove-one-row adjacency), exact quantiles and means are
removed, and min/max collapse to histogram-edge resolution. Read the scope
narrowly:

- The budget is PER COLUMN HISTOGRAM. A snapshot of k columns composes
  sequentially to roughly (k + 1) * epsilon total; the artifact's `dp` block
  records this scope.
- Bin edges and category labels remain DATA-DEPENDENT supports: the histogram
  range comes from the real min/max and categorical `top_values` carry real
  category strings (gated behind `allow_real_categories`). A fully
  data-independent release (fixed bin ranges, thresholded category sets) is a
  recorded follow-up.
- Joint contingency tables are rejected under `--epsilon` (no composition
  accounting in v1).
- Nothing downstream of the snapshot inherits the guarantee: generation
  samples from the noisy artifact deterministically, and masking is entirely
  outside it.

If your use case requires a formal privacy guarantee over the masked or
generated DATA, Decoy alone does not supply it.

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

## What it does do

To be clear about the other side: Decoy does give you deterministic,
reproducible masking; foreign-key and join preservation across tables; a catalog
of standard de-identification transforms; PII detection and risk profiling; and
synthetic-data generation. Those features are real and exercised by the test suite. This page exists so the strength of those features is not mistaken for guarantees Decoy does not make.
