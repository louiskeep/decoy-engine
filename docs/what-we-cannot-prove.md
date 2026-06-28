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

## The acceptance suite cannot measure correlation preserved THROUGH a value-changing mask

The acceptance test-flight (`testflight/`) asserts pairwise-correlation
preservation using the engine quality module's joint metric, which is a Total
Variation Distance over the joint contingency table of the two columns' actual
values. That metric compares value-labeled cells. A value-changing strategy
(`fpe`, `hash`, `code_set`, `joint_mask`) relabels the cells, so the source and
output crosstabs become disjoint and the similarity collapses to 0.0 even when
the correlation structure is preserved perfectly. Measured directly: an
`fpe`-masked pair whose structure is identical to the source scores 0.0, which
is lower than a genuinely decorrelated pair (about 0.34).

Consequence: the suite's correlation tooth can only verify that a correlation is
preserved for columns that remain VALUE-STABLE (e.g. `passthrough`), and that
such a correlation is not destroyed. It cannot verify that a value-changing
masking strategy preserves a correlation, because the shipped metric cannot see
through the relabeling. Genuinely closing that gap requires a relabel-invariant
statistic (Cramers V, mutual information, or a rank correlation) computed by the
harness on the masked output columns; that is owed work, not a current
capability. Do not read a green correlation check on a value-changing column as
proof that masking preserved its correlation.

## What it does do

To be clear about the other side: Decoy does give you deterministic,
reproducible masking; foreign-key and join preservation across tables; a catalog
of standard de-identification transforms; PII detection and risk profiling; and
synthetic-data generation. Those features are real and exercised by the test suite. This page exists so the strength of those features is not mistaken for guarantees Decoy does not make.
