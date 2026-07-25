# DE-10: decimal-scale referential-integrity hole in chunked FK self-masking

Status: **RESOLVED, already shipped on `main`.** This document was requested as
a forward-looking decision doc (fork of options A/B/C, pick one). Investigation
found the fix already merged before this doc was written: PR #74
(`20add89`, merged 2026-07-14) implements **Option A** (the breaking fix) and
Cam already approved it, per the commit message on the relevant sub-commit
("`Cam approved the fix`"). This doc records the finding, confirms the fix is
correct and complete, and flags the one loose end (a CHANGELOG gap) for
Cam's awareness. Nothing here requires a new decision; treat the "fork"
section as a record of the tradeoff that was already resolved, not an open
question.

## Summary

The chunked FK self-masking route (`run_mask_pipeline_chunked`, used when
`orphan_policy: remap`) re-derives each row's masked value independently,
with no parent-key lookup. For value-sensitive strategies (hash, fpe,
truncate, ...), two decimal columns of different scale that hold the "same"
logical value (`1.0` vs `1.00`) canonicalize to different bytes, so a parent
key and its child FK can mask to different values and silently stop joining.
Before the fix, the code folded every decimal declaration into one bare
`"decimal"` family regardless of scale, so this mismatch passed the
compile-time gate undetected. The fix makes the family scale-aware and fails
closed pre-GA on any FK column whose decimal declaration omits scale (a
"bare decimal" job on this one route). This has already shipped; it does
not affect the full-frame, sequential, or out-of-core routes, which don't
have this failure mode by construction (see Blast radius).

## Concrete failure example

A healthcare billing schema: `patients.patient_id` (parent key, `decimal(2,1)`
— an odd but real shape, e.g. a legacy check-digit-encoded ID) referenced by
`claims.patient_id` (child FK). Both columns are masked with `strategy:
hash` under `orphan_policy: remap`, routed onto the chunked self-masking path
because the job is too large to hold the full table in memory.

Before the fix (bare `"decimal"` family, no scale):

1. Config declares `dtype: decimal` (no scale) on both `patients.patient_id`
   and `claims.patient_id` — a plausible, common way to write "this is a
   decimal column" without spelling out precision/scale.
2. At compile time, `gate_fk_child_edges` condition (f) compares
   `_dtype_family("decimal")` on both sides. Pre-fix, both resolved to the
   same bare `"decimal"` family string, so the edge was **admitted**
   (`_chunked_fk.py:526`, comparing the return of `_dtype_family` at each
   call site).
3. At runtime, `patients` streams through with its real Arrow dtype
   `decimal128(2, 1)`; a patient row holds the logical value `1.0`, stored
   as the exact bytes for scale 1.
4. `claims` streams through separately (single-table-at-a-time, no parent
   data resident) with its real Arrow dtype `decimal128(3, 2)` — same
   logical value, `patient_id = 1.00`, stored at scale 2.
5. Each side hashes its own raw value independently. The kernel
   canonicalizer encodes a decimal by `(unscaled_int, scale)`
   (`_chunked_fk.py:164-169` docstring), so `1.0` at scale 1 and `1.00` at
   scale 2 canonicalize to **different byte strings**, and therefore hash to
   **different masked outputs** — even though they are the same patient ID.
6. Downstream: `claims.patient_id` (masked) no longer equals
   `patients.patient_id` (masked) for that patient. The join silently
   breaks. No error, no warning — the job completes "successfully" with
   corrupted referential integrity. In a healthcare/finance dataset, this
   is exactly the kind of quiet corruption that fails an audit weeks later,
   not at generation time.

After the fix, step 2 still admits the edge at compile time (bare-vs-bare
still compares equal — see Root cause), but step 3/4's *runtime* guard now
rejects each side independently before any row is masked, with a
`chunked_fk_declared_dtype_mismatch` error naming the column and explaining
why a bare decimal declaration can never be verified on this route. The job
fails fast and loud instead of finishing with silently broken joins.
Verified directly in
`tests/unit/execution/test_de10_chunked_fk_declared_dtype.py::test_reproduced_ri_case_bare_decimal_both_sides_fails_closed_each_role`
(lines 393-427), which reproduces this exact parent-`decimal128(2,1)`
/child-`decimal128(3,2)` shape and asserts both sides now raise.

## Root cause

Code walk, all in `src/decoy_engine/execution/`:

- **`_chunked_fk.py:111-186`** — `_dtype_family(dtype: str) -> str`, the
  shared family classifier used at both the compile-time gate and the
  runtime guard. For decimal/numeric (`_chunked_fk.py:160-171`):
  - If the dtype string carries explicit precision+scale (PyArrow's own
    `str()` form `decimal128(2, 1)`, or hand-written SQL-style
    `decimal(2, 1)`/`numeric(2, 1)`, matched by `_DECIMAL_PRECISION_SCALE_RE`
    at `_chunked_fk.py:66-68`), it resolves to a **scale-keyed** family
    string `"decimal(scale=S)"` (precision is deliberately dropped — verified
    empirically that `decimal128(2,1)` and `decimal128(3,1)` mask an equal
    key to identical bytes, so folding precision in would over-reject a
    healthy scale-matched/precision-mismatched pair).
  - If the regex doesn't match (a bare `"decimal"`/`"numeric"` with no
    scale), it resolves to the sentinel `_DECIMAL_UNPROVABLE_FAMILY =
    "decimal:unprovable"` (`_chunked_fk.py:59`).
- **Compile-time call site**: `_chunked_fk.py:526`, inside
  `gate_fk_child_edges` condition (f)
  (`_chunked_fk.py:477-545`): `if _dtype_family(parent_dtype) !=
  _dtype_family(child_dtype): raise ...chunked_fk_child_key_dtype_mismatch`.
  Two bare declarations both resolve to the same sentinel string, so they
  compare **equal** and the edge is admitted here — the compile gate cannot
  catch a bare-vs-bare pairing because it only ever sees the declared
  strings, and two unprovable declarations are indistinguishable from each
  other at that point.
- **Runtime call site**: `_chunked_fk_dtype.py:133-134`, inside
  `reject_mismatched_chunked_fk_declared_dtype`
  (`_chunked_fk_dtype.py:105-170`): `real_family =
  _arrow_dtype_family(real_type)` (which itself delegates to `_dtype_family`
  via `str(arrow_type)` at `_chunked_fk_dtype.py:50`) vs `declared_family =
  _dtype_family(declared_dtype)`. This is where the actual per-chunk Arrow
  data is available, so it is the only place scale can be checked against
  reality. The explicit sentinel check at `_chunked_fk_dtype.py:142-154`
  fails closed on **any** column whose *declared* family is
  `"decimal:unprovable"`, independent of what the real family turns out to
  be — because the chunked route only ever sees one table at a time, it can
  never confirm the other side's real scale, so a bare declaration can never
  be proven safe here, full stop.
- Why the fix needed to touch admission logic at all: the underlying
  masking kernel canonicalizes decimals by `(unscaled_int, scale)`
  (`_chunked_fk.py:122-138` docstring), which is correct kernel behavior
  (scale genuinely changes the byte representation of a decimal), but that
  means the *admission* layer — the only place that can say "these two
  declarations are provably compatible" — has to know the scale to make that
  promise. A bare declaration carries no scale, so no promise is possible,
  and pre-fix the admission layer silently treated "unknown" as "same" —
  the classic RI hole shape.

## Blast radius

**Affected**: chunked FK self-masking route only
(`run_mask_pipeline_chunked`, reached when `orphan_policy: remap` and the
job routes onto the chunk-streaming path — typically large tables that don't
fit in memory). Within that route, only:
- Value-sensitive strategies (everything except `redact`, per
  `DTYPE_INVARIANT_STRATEGIES` at `_chunked_fk.py:108`) — `redact` emits a
  constant regardless of input so scale is irrelevant.
- FK edges where at least one side's declared dtype is a bare
  `"decimal"`/`"numeric"` (no precision+scale), **or** both sides declare
  scales that don't match. A correctly, identically scaled declaration on
  both sides (e.g. both `decimal128(2, 1)`) is unaffected and continues to
  work exactly as before.
- Both `mask` and `gen`-with-remap jobs that use this route are equally
  exposed — the gate doesn't distinguish job type, only strategy/dtype
  shape.

**Not affected** — full-frame, sequential, and out-of-core routes do not
share this failure mode, by construction, not by a parallel fix:
- **Full-frame / sequential** (`PandasExecutionAdapter`, `_fk_keys.py`):
  these routes build a real parent-key map from the parent's *actual* data
  and look up each child's masked value through that map — they never
  re-derive the child's masked value independently from its own raw bytes.
  The join key itself is decimal-scale-normalized on purpose:
  `_decimal_join_token` (`_fk_keys.py:139-160`) calls `.normalize()` so
  `Decimal('1.20')` and `Decimal('1.2')` collapse to the same join token
  (`_fk_keys.py:142-152` docstring) — i.e., these routes treat differing
  decimal scale as the *same logical key* for join purposes and then reuse
  the parent's one precomputed masked value for the child. There is no
  independent re-derivation step where scale-dependent canonicalization
  could diverge.
- **Out-of-core** (DuckDB route, `execution/out_of_core/`): also joins
  through a real `RelationshipGraph`/parent map (`_join.py`, `_batch_join.py`,
  `_runner.py` all take a `relationship_graph: RelationshipGraph` and
  perform an actual join), not a declared-dtype admission gate. Same
  reasoning as full-frame: the parent's masked value is looked up, not
  re-derived, so scale mismatch in the *declaration* is moot.
- The chunked route is the only one that masks a child's FK key with zero
  visibility into the parent's data, which is exactly why it needs an
  admission-time promise (the dtype declaration) in the first place — and
  exactly why that promise had to become scale-aware.

## The fork (as it stood before the fix — for the record)

**(A) Breaking fix: require scale in FK decimal declarations, fail closed on
bare decimals pre-GA.** This is what shipped. Correct RI: a job with
correctly-scaled matching declarations continues to work; a job that can't
prove scale agreement is rejected loudly at first-chunk execution
(`chunked_fk_declared_dtype_mismatch`) instead of silently corrupting joins.
Breaks any existing bare-decimal FK job on the *chunked* route specifically
— such a job now fails where it previously ran (with silently wrong output).
Cheapest to take pre-GA: engine is explicitly `RELEASE_PHASE = "pre-ga"`
(`src/decoy_engine/release.py:45`), and the forward roadmap
(`~/.claude/plans/decoy-forward-roadmap.md`) states the whole point of
staying unfrozen is to front-load exactly this class of format/admission
change before the one GA-corpus-freeze — "unfreeze until actual release...
we freeze the corpus + flip RELEASE_PHASE->ga ONCE, at the release cut." A
breaking admission change taken now costs zero customers (pre-GA, no
compatibility contract binding yet — `docs/compatibility-contract.md` only
becomes binding at GA per `CLAUDE.md`); taken after GA it would require a
deprecation cycle under the frozen compatibility contract.

**(B) Opt-in scale param, default preserves bare-decimal behavior.**
Non-breaking: existing bare-decimal jobs keep running unchanged. Requires
adding a new opt-in config knob (e.g. `require_decimal_scale: true`) that a
careful operator could set, but the *default* leaves the RI hole open for
every job that doesn't opt in — which is realistically almost all of them,
since the hole is invisible until someone notices broken joins. This
converts a correctness bug into a documented, silently-active footgun for
the default path. Weaker than A for a security/correctness-sensitive
product (Decoy's whole pitch is trustworthy synthetic data).

**(C) Park and document the hole for GA, revisit post-GA.** Cheapest short
term, but wrong at this stage specifically: pre-GA is exactly when
admission-semantics changes are supposed to land (per the roadmap doc), and
parking a known HIGH-severity silent-RI-corruption bug into the GA corpus
freeze means it ships to the compatibility contract and becomes expensive to
fix later (any post-GA admission tightening is now a breaking change under a
binding contract, requiring a deprecation path). Also inconsistent with the
existing DE-10 lossless-typing contract precedent
(`_fk_keys.py:1-46` docstring) of fixing FK correctness holes fail-closed as
they're found rather than deferring them.

## Recommendation

Ratify what already shipped: **Option A**. The reasoning that applied when
Cam approved it still holds and doesn't need to be re-litigated:
- The engine is pre-GA and unfrozen specifically so format/admission
  changes are cheap now and expensive later — this is a textbook instance
  of that window.
- The alternative (B) makes a known silent-corruption bug the default
  behavior, which is a worse trade for a data-integrity product than a
  loud, actionable failure with a clear error message and a documented
  remediation ("declare the exact decimal type, e.g. decimal128(P,S), or use
  run_pipeline/run_sequential").
- The fix is narrowly scoped: it only fails jobs that (a) route onto chunked
  self-masking, (b) use a value-sensitive strategy, and (c) have an
  unprovable or mismatched decimal declaration on an FK key. Every other
  shape, including all bare-decimal columns that are NOT FK keys, and all
  correctly-scaled FK declarations, is unaffected.
- Tests confirm correctness: 79/79 pass across the three DE-10 test files
  (`tests/unit/execution/test_de10_chunked_fk_declared_dtype.py`,
  `test_de10_chunked_fk_passthrough.py`, `test_de10_fk_lossless_typing.py`),
  including the exact bare-decimal-both-sides repro case.

One loose end for Cam's awareness, not a re-decision: the scale-aware
decimal fix (PR #74, `20add89`, merged 2026-07-14) doesn't have its own
`CHANGELOG.md` entry — the `[0.4.0] - 2026-07-15` section documents the DE-02
KeyProvider work and two smaller chunked-masking fixes but is silent on
DE-10's family-model change. Given this is a breaking change for a real
(if narrow) class of jobs, it's worth a CHANGELOG line so anyone hitting
`chunked_fk_declared_dtype_mismatch` on an upgrade has something to grep for.
This is a docs gap, not a code or design gap — flagging for barry/doc-sync
rather than proposing to fix it here.

## Rough implementation cost (historical — this already shipped)

For reference, sizing what each option would have cost, since the ledger is
useful if a similar fork comes up again:

- **(A) Breaking fix — actual cost, already paid.** Effort: **M**. Touched
  `_chunked_fk.py` (scale regex, sentinel, scale-keyed family — ~60 net
  lines including docstring), new sibling `_chunked_fk_dtype.py` (~170
  lines, kept `_chunked_fk.py` under the 600-LOC orchestration cap per
  `CLAUDE.md`), a sentry allowlist bump for `_chunked_fk.py` (650 cap,
  tracked with a decompose-later note), and a new test file
  (`test_de10_chunked_fk_declared_dtype.py`, ~480 lines covering int/float/
  date/timestamp/binary/decimal family edge cases). Went through five
  follow-up commits (date/timestamp+binary split, scale-aware decimal,
  negative-scale BLOCKER fix, decimal32/64 + precision-vs-scale LOW fixes,
  sentry allowlist) — the iteration cost came from cross-model review
  (dennis + Codex) catching edge cases (negative scale, decimal32/64,
  precision-vs-scale folding) rather than the core design being wrong.
- **(B) Opt-in param — estimated, not built.** Effort: **S-M**. Would need a
  new config field, threading through the compile gate and runtime guard,
  a default-off code path, and docs explaining when to turn it on. Smaller
  than A in raw lines, but carries ongoing cost as a support/documentation
  burden (explaining why RI can silently break by default) that A avoids
  entirely.
- **(C) Park and document — estimated, not built.** Effort: **XS** (a
  paragraph in known-limitations docs). Lowest short-term cost, highest
  deferred cost: post-GA, closing the same hole becomes a breaking change
  under the frozen compatibility contract, which per the roadmap doc's own
  framing is exactly the class of cost the pre-GA unfreeze window exists to
  avoid paying.
