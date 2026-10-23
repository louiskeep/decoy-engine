# DI1b (F-DI0-2): propagate nested-GOF unavailability into the quality report

Status: BUILT 2026-10-22 (branch `claude/di1b-gof-omission-propagation`) —
`_collect_warnings` emits `gof_unavailable`; 3 new unit tests + 1 new golden
fixture; all 652 quality/snapshot tests pass, 5 existing goldens unchanged,
ruff + mypy clean. The pre-existing `test_warnings_sorted_deterministically`
assertion was corrected to the real (source, code) sort contract (its data now
also raises a `gof_unavailable` warning). Dev loop, no cross-model review
(owner instruction). Platform honest-capability-doc update + F-DI0-1 semantics +
generation-output GOF are follow-ups; roadmap/shipped-log reconcile at merge.
Date: 2026-10-22
Risk: R2 (changes the quality-report contract — adds a warning code — touching
reported data semantics; additive, pre-GA)

Rules consulted: 00-universal.md, development-loop.md, feature-dev.md,
testing.md, verification.md, observability-and-resilience.md,
api-and-compatibility.md (report shape is a consumed contract; pre-GA additive),
reuse-first.md.

## FRAME

### Problem (DI0 finding F-DI0-2)

The quality report's goodness-of-fit metrics live nested at
`marginal.columns[*].extra_metrics.{ks_complement,chi_cramers_v}`, each carrying
its own `comparable` flag and, when incomparable, a `reason`. But the report's
assembly only propagates the column's PRIMARY `comparable` flag:
`report.py:_collect_omissions` records a column as omitted only when its primary
similarity is incomparable, and `_collect_warnings` never inspects
`extra_metrics` at all. So a column whose primary score IS comparable but whose
nested GOF is NOT (the disjoint/constant chi-square cases F-DI0-1 flags, a
missing numeric histogram, etc.) produces no report-level signal — the report
silently overclaims GOF coverage. (Honest-capability statement, decoy-platform
`docs/backlog/distribution-integrity/honest-capability-statement.md`: "Unavailability
does not propagate… Only the primary comparable flag feeds omissions.")

### Definition of done

1. When a column's nested GOF sub-metric is `comparable: False`, the report
   carries a `gof_unavailable` warning naming the column, the metric, and the
   GOF reason — so a reader can see exactly which GOF coverage is missing and
   why, instead of assuming full coverage.
2. The warning is independent of the primary similarity: a column can be both
   score-comparable (not an omission) and GOF-unavailable (a warning).
3. Additive and deterministic: no existing golden digest changes (existing
   fixtures keep comparable GOF or carry no `extra_metrics`), warning ordering
   stays byte-stable.

### Guarantee / non-goals

- Surfaced as a WARNING, not an OMISSION: `omissions` has a precise
  "excluded from the score" meaning and GOF sub-metrics are never in the score,
  so a warning is the correct channel (closes the "no warning" half of the
  finding directly). `omissions` is left untouched.
- NOT changing the GOF primitive's behavior. F-DI0-1's deeper question (should a
  fully-disjoint categorical score V=1 instead of returning incomparable) is a
  statistical-semantics decision left as a follow-up; the GOF module already
  returns incomparable conservatively for cases the top-K/other_count snapshot
  cannot soundly compare, and THIS slice makes that unavailability honest in the
  report. Generation-output GOF wiring is a separate DI1b piece (Phase F tie-in).
- The platform honest-capability doc update (removing the "does not propagate"
  limitation) is a decoy-platform follow-up; this engine slice updates the
  engine CHANGELOG. Roadmap/shipped-log reconciliation lands at merge.

## Established solutions

Reuse the existing warning channel and shape (`{source, code, detail}`) rather
than inventing a new report section; reuse the GOF result's own `comparable`
and `reason` (single source of truth — `_distribution_gof`), computed by
`fidelity.py`, so assembly only reads flags it already produces.

## PLAN (acceptance tests + failure modes)

Change `report.py:_collect_warnings` to iterate `marginal.columns[*]
.extra_metrics` and emit one `{"source":"fidelity","code":"gof_unavailable",
"detail":{"column","metric","reason"}}` per incomparable sub-metric, and make
the warning sort deterministic via `.get`-based string tiebreakers (existing
warnings have unique `(source, code)`, so their order — and every existing
golden digest — is unchanged).

Acceptance (tests/unit/quality/test_report.py + a new golden fixture):

- **A1** a column with a comparable primary score but an incomparable nested GOF
  (constant categorical → chi `insufficient_identifiable_support`) yields a
  `gof_unavailable` warning carrying the column, metric, and reason; the column
  is NOT in `omissions`; the grade/score still reflect the primary.
- **A2** identity report (comparable GOF everywhere) carries NO `gof_unavailable`
  warning — guards against false positives (and keeps the existing
  `warnings == []` identity assertion valid).
- **A3** multiple `gof_unavailable` warnings are ordered deterministically
  (byte-stable across runs).
- **A4** golden: a new `gof_unavailable` fixture pins the report digest; the
  five existing fixture digests are unchanged (regenerate and diff to confirm).

Failure modes: non-deterministic warning order (handled by the explicit sort
key); accidental change to an existing golden (verified: no existing fixture
triggers the new path).

Note: built under the dev loop with NO cross-model review per owner instruction
(2026-10-22); REVIEW is a self-review against this plan.
