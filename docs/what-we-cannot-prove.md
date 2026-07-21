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
statistical generate column. This is a CONDITIONAL claim, not a blanket one:
generated marginals are (epsilon, delta)-differentially private by
post-processing of the DP snapshot only for the specific columns and only
when the snapshot was actually produced by a DP fit. Do not read "Decoy
supports DP" as "every statistical column in every run is DP" -- most are
not, unless every item below holds for that specific column.

**Categorical DP is not yet supported (as of 2026-07-21).** An earlier draft
of this feature attempted a categorical release (real vocabulary through a
stable-histogram threshold). A privacy review (Codex) found the mechanism
did not satisfy its stated (epsilon, delta) bound: the released label ORDER
leaked unnoised rank information (`quality/snapshot.py`'s true-count sort,
`quality/dp.py`'s iteration order), suppressed noisy counts were summed into
an observable `other_count` (a measurable privacy gap, not a rounding
error), counts were rounded before the threshold test (silently lowering the
effective threshold by up to 0.5), and fit SUCCESS itself was a
data-dependent function of the private data (an object column typed
categorical below 30 distinct values, freetext -- rejected under `dp_mode`
-- above it, so two neighboring datasets could produce "artifact exists" vs.
"typed error" at zero privacy cost). Rather than ship a guarantee that does
not hold, `dp_mode` now rejects EVERY object/string column outright at fit
time (`dp_mode_categorical_unsupported`, `quality/snapshot.py::_stats_for`,
regardless of its distinct-value count), and `global_settings.dp` rejects
any categorical `type: statistical` generate column with one clear
compile-time error (`dp_categorical_not_yet_supported`,
`plan/_checks_dp.py`). Numeric marginal DP is unaffected; it never depended
on the categorical mechanism. A correct categorical mechanism is a tracked
follow-up, not a documented-but-broken feature.

**What is covered.** A statistical generate column's marginal carries the
(epsilon, 0) guarantee -- pure DP, no delta spent -- only when its snapshot
column is:

- **Numeric, with a caller-supplied domain.** Fit with `dp_mode=True` and a
  `numeric_domains` entry for that column (`quality/snapshot.py`, DPS-1):
  fixed bin ranges, not the real min/max, so releasing the support spends NO
  privacy budget (it never touched the data). Without a caller-supplied
  domain the range comes from the real data and the guarantee does not hold
  for that column, dp_mode or not.

**What is NOT covered, ever, as of this writing:**

- **Categorical marginals.** Not yet supported at all (see above) -- fit
  rejects the column, and compile rejects the config, before either can
  reach `generate`.
- **Datetime and freetext marginals.** These are out of scope on BOTH
  sides. At fit time, `dp_mode` REJECTS datetime columns outright
  (`quality/snapshot.py::_stats_for`, raises `ValueError`) rather than
  silently degrading: a datetime column's year bins come from the real
  observed year set -- data-dependent support with no caller-supplied
  override, so an outlier admission year or DOB would survive as a bin
  whose PRESENCE singles out an individual regardless of the noised count.
  Freetext is out of scope for the same reason and is now covered by the
  same object/string rejection as categorical (above). At consume time, the
  provenance check (below) additionally rejects any referenced
  datetime/freetext column, because `apply_dp_noise` will happily noise a
  column that was fit WITHOUT `dp_mode` -- so fit-time rejection alone does
  not stop a non-`dp_mode` snapshot from being noised and then consumed.
  Mask or exclude these columns, or fit them outside `dp_mode` (their
  release then carries no DP guarantee at all, same as any pre-DPS-1 fit).
- **Cross-column joint structure.** Joint contingency tables are rejected
  under `--epsilon` entirely (no composition accounting in v1), and a
  preserved marginal says nothing about preserved correlation.
  Joint-distribution DP (PrivBayes-style) is a separate, larger,
  not-yet-built effort.
- **Masked output.** Still carries no epsilon (masking is a deterministic
  transform, entirely outside this mechanism).
- **`high_cardinality: true` and `allow_real_categories: true` columns.**
  Both retain or release real vocabulary and are anti-DP by construction
  (and, being categorical, are covered by the blanket rejection above
  regardless).

**What is enforced mechanically versus what is an operator precondition.**
Four things are compile-time or test-suite enforced, not just documented:

- **Categorical is hard-rejected.** When `global_settings.dp` is set, a
  `type: statistical` generate column whose referenced snapshot column has
  `kind: "categorical"` is rejected at compile time
  (`plan._checks_dp.check_dp_categorical_unsupported`,
  `dp_categorical_not_yet_supported`) regardless of `allow_real_categories`.
- **Anti-DP knobs are hard-rejected.** When `global_settings.dp` is set,
  `high_cardinality: true` and `allow_real_categories: true` on a
  statistical generate column are hard-rejected at compile time
  (`plan._checks_dp.check_dp_generate_contract`): a config cannot silently
  combine either with a DP declaration.
- **A dp-declared pipeline must consume a DP-fit snapshot, within its
  declared budget.** When `global_settings.dp` is set, compile time also
  checks the snapshot each statistical generate column actually references
  (`plan._checks_dp.check_dp_snapshot_provenance`): the snapshot must carry
  a `dp` block with a recorded `epsilon_total` (proof `apply_dp_noise` ran
  over it), the referenced column must not be categorical (above), and a
  NUMERIC column must show `support_origin: "caller"` (fit with `dp_mode` +
  a `numeric_domains` entry, not the real data-dependent range). A plain,
  never-DP-fit snapshot, or a numeric column fit without `dp_mode`, is
  rejected with `PlanCompileError` even though every other check passes.
  The per-kind verdict is a fail-closed ALLOW-LIST: a referenced column is
  accepted ONLY as numeric-with-`caller`; every other kind -- categorical,
  datetime, freetext, empty, and any unknown/future kind -- is rejected by
  default (`dp_snapshot_kind_not_dp_eligible` for the non-categorical
  cases). This matters because `apply_dp_noise` noises ANY snapshot
  (datetime year bins and freetext length bins included), so a column fit
  WITHOUT `dp_mode` and then noised carries a `dp` block over data-dependent
  support; the consume-side allow-list is what stops that non-DP release
  from shipping under a DP declaration, not the fit-time rejection alone.
  Beyond provenance, the check also composes every DISTINCT consumed
  artifact's `(epsilon_total, delta_total)` (deduped by content hash -- the
  same artifact referenced by many columns is one release, charged once) by
  basic sequential composition (Dwork & Roth Thm 3.16) and rejects the
  config (`dp_budget_exceeded`) if the composed spend exceeds
  `global_settings.dp`'s DECLARED `epsilon`/`delta`; a malformed artifact
  budget (missing/non-finite/negative) fails closed
  (`dp_snapshot_budget_malformed`). Before this, the declared ceiling was
  checked for presence only, never compared against what the artifacts
  actually spent.
- **Generation reads only the artifact.** Post-processing immunity requires
  the sampler never re-touch the raw source frame; this is a
  regression-locked contract, not an inference --
  `test_generation_consumes_only_the_snapshot`
  (`tests/unit/generation/test_generate_dp_contract.py`) fails the build if
  a future refactor makes the sampler reach for raw data.

**The snapshot loader is content-addressed.** `generation/statistical/
_spec.py`'s snapshot cache is keyed by `sha256` of the file bytes, not the
file path, and `plan._checks_dp.check_dp_snapshot_provenance` reads through
the SAME loader. A cache hit is therefore byte-equivalent to a fresh read at
call time by construction: a long-lived process (e.g. the platform API)
cannot serve a stale cached artifact to a DP-declared run after the file at
a given path is overwritten with a different (or non-DP) one, and the
compile-time gate and the generation-time sampler provably see the same
parsed object rather than merely the same path.

**The consumed snapshot is trusted to be genuine.** The provenance check
reads the snapshot JSON at face value: it defends against honest
misconfiguration (pointing a DP-declared pipeline at a non-DP-fit artifact,
or under-declaring the spent budget), NOT against a forged or hand-edited
snapshot. The (epsilon, delta) guarantee assumes the consumed artifact is
the real output of `decoy fit`; there is no signature on the snapshot today,
so a deliberately falsified `dp` block or `support_origin` marker would
pass.

Everything else -- that the operator actually ran `decoy fit --epsilon`
with `dp_mode` and a correct `numeric_domains` entry per numeric column in
the first place, and that the composed budget (`dp.epsilon_total`/
`dp.delta_total`, sequential composition per Dwork & Roth, *Algorithmic
Foundations of DP*, Thm 3.16) reported to stakeholders is the DECLARED
`global_settings.dp` ceiling now enforces against, not a smaller
single-histogram `epsilon` quietly substituted in its place -- is an
OPERATOR PRECONDITION: something the fit invocation must get right, not
something the engine can independently verify from the config alone at
every point.

**The fit-time flow is CLI-side and its wiring is unverified here.** The
engine change described above provides the MECHANISM
(`quality/snapshot.py`'s `dp_mode`/`numeric_domains`, `quality/dp.py`'s
`apply_dp_noise`) and the CONSUME-SIDE enforcement
(`check_dp_generate_contract` + `check_dp_snapshot_provenance` above). It
does not, by itself, prove that `decoy fit --epsilon` on the CLI actually
threads `dp_mode`/`numeric_domains` through to
`compute_distribution_snapshot` end to end -- that wiring lives in a
separate repo and is pending verification. Until it is verified, treat this
as "the engine can produce and correctly gate a DP snapshot when asked
directly," not as "the `decoy fit --epsilon` CLI command is a turnkey DP
workflow."

**DP-fit output is not reproducible run to run, by design.** The Laplace
noise in `apply_dp_noise` (`quality/dp.py`) is drawn from fresh OS entropy
(`numpy.random.default_rng()`), never from the job seed. This is deliberate:
seeding the noise from material the config holder controls would let them
subtract it back out and recover the true counts, voiding the guarantee, so
the `rng` parameter exists only for tests. So each DP release draws fresh
noise: applying the mechanism twice to identical counts, at the same seed and
config, yields a different noisy snapshot every time. This is separate from
Decoy's normal determinism
contract, not a weakening of it: once a specific noisy snapshot artifact
exists, GENERATING synthetic rows from it is fully seed-reproducible as usual
(the samplers are seeded; the snapshot is just weights). So do not expect
byte-identical DP snapshots across repeated fits, and do not treat the job
seed as a privacy-relevant secret for the DP mechanism, since it plays no
part in the noise draw. Tracking total epsilon across repeated fits is now
enforced against `global_settings.dp`'s declared ceiling (above), not just
the operator's responsibility to track separately.

If your use case requires a formal privacy guarantee over MASKED data, over
generated JOINT structure, or over a CATEGORICAL, datetime, or freetext
column, Decoy alone does not supply it.

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
