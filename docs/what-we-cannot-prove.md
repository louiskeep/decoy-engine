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

## It does not validate that your configuration matches your intent

Decoy validates a config against its schema and runs what the config says. It
does not know which columns in your data are actually sensitive. If a config
leaves a sensitive column on `passthrough`, or masks the wrong column, the run
will succeed and the output will leak. Use `storm` to find candidate PII and the
post-mask checks to look for residual identifiers, but the mapping from your
data's sensitivities to a correct config is yours to get right.

## What it does do

To be clear about the other side: Decoy does give you deterministic,
reproducible masking; foreign-key and join preservation across tables; a catalog
of standard de-identification transforms; PII detection and risk profiling; and
synthetic-data generation. Those features are real and exercised by the test suite. This page exists so the strength of those features is not mistaken for guarantees Decoy does not make.
