Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, architecture, performance, testing, verification, use-established-methodology

# Arrow-native FK-key resolve for the F2 mixed route (100M fix)

Per Cam 2026-10-18 (fix-first for 100M). Rev-2 folds the Codex plan-gate NO-GO (2 P1 +
2 P2). The dominant memory transient F4 measured is the PER-ROW Python tuples/dicts
(parent_map + child_keys + masked list) in resolve_generate_parent_fk_child, projected
~20GB at 100M. Rev-2 removes THAT by resolving values with pyarrow compute, while
REUSING the existing value-dependent write-back typing (not reimplementing it).

## 1. Problem

Hotspot: execution/_mixed_fk_bridge.py resolve_generate_parent_fk_child (84-96) +
_fk_resolve.py resolve_fk_keys (80-157): parent.to_pylist + dict of 2 tuples/parent-row;
child.to_pylist + a (key,) tuple/child-row (the dominant O(child-rows) transient);
a masked list; then _write_back_fk_column's pandas round-trip. The carrier
(_chunked_preresolved) + mixed route are already Arrow. Shadow (physical/_shadow_fk.py)
shares the function - a rewrite must keep BOTH byte-identical.

## 2. Goal

Compute the resolved FK values with pyarrow compute (no O(child-rows) Python objects),
producing the IDENTICAL resolved column (values + exact dtype + warnings/errors) as the
full-frame oracle AND the shadow leg. Byte-identity pinned by the F2 parity + sweep +
de10 + shadow tests.

## 3. Design (Arrow-native VALUES; REUSE existing typing)

PAIRWISE key-type gate (fold P1-1): take the Arrow path ONLY when the CHILD and PARENT
key columns are the SAME family AND a family where Arrow equality == fk_key_value:
- both plain integer (handle width/signedness losslessly; NO float coercion; cover
  uint64 incl. 2**63+1), or both utf8/large_utf8.
Any CROSS-family pair (e.g. int child / string parent - which index_in would wrongly
match or raise) OR any float/decimal/bool-mixed/other type DELEGATES to the existing
Python resolve_fk_keys bridge unchanged (the correctness-preserving fallback; F2 already
declines dirty/float shapes to full-frame, so no admitted coverage is lost).

RESOLVE with pyarrow compute (for the gated fast path):
- Parent value_set from the parent output's key column. index_in returns the FIRST
  match; the current dict is last-write-wins. For the generate-parent IDENTITY map,
  duplicate equal keys have EQUAL preserved values, so first-vs-last is NOT an
  observable value difference - DOCUMENT this; no dedup needed for the identity map.
  (If a future non-identity map is added, revisit.)
- NULL handling (fold P2-3): pc.index_in matches nulls by DEFAULT. A null CHILD key must
  resolve to null and must NOT count as an orphan (matching today). Explicitly exclude
  null child rows from matching + from the orphan set: compute a child-validity mask and
  apply it (e.g. mask index_in output where child is null -> null; orphan =
  index_null AND child_valid). Verify parent nulls do not create spurious matches.
- indices = pc.index_in(child_keys, value_set=parent_keys); taken = pc.take(parent_values,
  indices); orphan_mask = and(is_null(indices), is_valid(child_keys));
  orphan_count = pc.sum(orphan_mask).as_py() (PLAIN int). resolved_values =
  pc.if_else(orphan_mask, child_keys, taken) for PRESERVE/WARN; FAIL raises the exact
  orphan_fk_violation message; REMAP raises orphan_remap_parent_missing (exact). Select
  compatible branch types for if_else BEFORE calling it so uint64/2**63+1 cannot overflow.

WRITE-BACK dtype (fold P1-2): DO NOT reimplement the oracle's value-dependent dtype
algorithm. The oracle output dtype is value-dependent (nonempty int -> int64; all-null
int32 -> int32; string all-null -> null; large_string -> string; uint64 -> int64). FEED
the Arrow-resolved values into the EXISTING write-back typing (lossless_fk_int_values /
fk_nullable_int_array / fk_all_null_array in _fk_keys.py) that the oracle path already
uses, so the exact dtype is reproduced by the proven code - not re-derived. Keep the
existing float+int>2**53 rejection (out_of_core_fk_key_dtype_unsupported). This still
removes the dominant transient (the per-row tuples/dicts) even if the write-back keeps a
single O(N) column materialization; an Arrow-only write-back is a SEPARATE optional
follow-up only if the 100M run shows the write-back itself is the new ceiling.

WARN: reuse out_of_core/_join.py orphan_fk_warning (byte-identical detail + plain-int
orphan_rows); one per edge, only when WARN + orphans>0.

Shared production+shadow via the one function (do not fork).

## 4. Acceptance

- tests/parity/test_phase_f_f2_mixed_parity.py + _sweep.py ALL green, byte-identical
  (ipc_bytes incl. schema) + warning_signature-identical, unchanged: PRESERVE/WARN
  (one edge warning, orphan_rows==oracle count), FAIL, REMAP matched + orphan,
  int>2**53 lossless typing, string + int keys, NULLABLE keys (incl. null child != orphan),
  multi-chunk, streamed==resident. Dirty/float/when-reads-key/count-warning cases STILL
  decline to full-frame (gate unchanged).
- NEW differential tests: cross-family pairs (int child/string parent AND string
  child/int parent) DELEGATE to the Python bridge (not the Arrow path) and match the
  oracle; uint64 boundary incl. 2**63+1; duplicate parent keys (identity-map dedup
  non-observable); null child + null parent.
- Shadow parity (tests/physical shadow_fk/shadow_mixed) + de10 lossless typing green.
- MEMORY (fold P2-4): re-run the F4 10M mixed tier; set a NUMERICAL peak-RSS target
  (meaningfully below the ~2009MB F4 10M baseline) and assert total external peak RSS +
  show the per-row-object transient is gone. Describe as READINESS for the 100M GCP run,
  NOT an 8GiB proof (the exact 100M is the separate GCP step).
- ci-mirror clean; pytest-one affected suites, companion present + absent.

## 5. Risk

The value-dependent dtype is the main trap - mitigated by REUSING the existing typing
code (not re-deriving). The pairwise gate must be conservative (same-family only, else
Python fallback). index_in null semantics must be handled explicitly (tested). if_else
branch types chosen before the call to avoid uint64 overflow. Memory: the write-back's
single O(N) materialization may still dominate at 100M - measured at 10M; if so, the
Arrow-only write-back follow-up is pre-identified.

## 6. Gate chain

Codex plan-gate (this doc) -> build (Sonnet) -> ci-mirror + F2 parity/sweep + the new
differential/boundary/null tests + shadow + de10 + the 10M memory target, both venvs ->
dennis -> Codex final -> merge (Codex + dennis green + local CI). Unblocks the GCP 100M
mixed run. DOCUMENT: CHANGELOG + records note.
