# Mutation grading: `execution/_chunked_text_mask.py` -- 89.13% (0 unresolved)

Phase 4 slice 3 (`text_mask` on the chunked route,
`docs/plans/2026-09-01-p4-slice3-text-mask-chunked.md`). `_chunked_text_mask.py` is a new,
single-function module: `reject_text_mask_when`, the `text_mask` + `when:` fail-closed gate
(Trap E). The slice's other production change -- adding `"text_mask"` to `CHUNK_SAFE_STRATEGIES`
in `_chunked_fk.py` -- is graded by the admission and FK-parity tests directly (see "the one-line
admission" below), not by this mutmut run.

Graded via `scripts/native-testing/python_mutation_pilot.py` (standalone-pytest-per-mutant,
avoids mutmut's in-process false-timeout pathology on this pandas/pyarrow-heavy suite), selection
`tests/unit/execution/test_text_mask_chunked.py::TestWhenRejection`, killed-bucket readjudicated.

## Numbers

**41/46 = 89.13% LOGIC, 0 unresolved. 5 survivors, all ACCEPTED NON-CONTRACT (message prose),
0 killable-and-undocumented residual.**

| Function | Total | Killed | Accepted non-contract |
|---|---|---|---|
| `reject_text_mask_when` | 46 | 41 | 5 |

The machine fields asserted (and therefore killed): the coded error
(`chunked_text_mask_when_not_supported`), the `PlanCompileError.path`, the offending COLUMN NAME
(via a name that shares no word with the message's own prose -- see "the self-collision fix"
below), the `', '.join(...)` separator over two offending columns, the `"?"` placeholder when a
column entry has no `name` key, and each of the five trailing "why" sentence fragments' exact
casing (killing a mutant that UPPERCASES a whole fragment). Free-text explanatory PROSE that only
gets an "XX" marker padded onto an otherwise-untouched fragment (the inner text survives
unchanged) is left ACCEPTED NON-CONTRACT -- the same policy `execution_chunked_dgrn.md` established
for `reject_windowed_date_when`: killable only via brittle full-message equality, and the sweep
does not pursue that for pure prose.

## The self-collision fix

The first mutation pass (before the fixes below) reported these mutants as false survivors:
mutants that gutted the column-name extraction entirely (`str(None)` in place of
`str(col_entry.get("name", "?"))`, a `None`/`"?"`/`"XXnameXX"` default-arg tamper, a missing-key
lookup) all SURVIVED against a `test_check_chunked_compatibility_raises_directly` that asserted
`"cell" in exc.value.message` -- because the test's own column was named `"cell"`, and the
rejection message's own explanatory prose contains the word "cell" ("the handler
str()-converts every non-null cell"). The substring assertion passed on every mutant regardless
of what the extracted name actually was, silently voiding the whole test's grading value. Fixed
by renaming the probe column to `target_col` (a name that shares no word with the message's
fixed prose) and adding the DGRN-style precision: `test_two_offending_columns_both_named_and_
comma_joined` (pins the join separator and that BOTH names are read) and
`test_column_missing_name_key_falls_back_to_placeholder` (pins the `"?"` sentinel). This dropped
the survivor count from 20 to 5.

## The 5 accepted non-contract survivors

Mutants 37, 39, 41, 43, 45 -- each XX-wraps ONE of the five trailing prose fragments of the "why"
sentence (`"with a 'when:' predicate, which is not supported on the chunked "`, `"when-gated
column leaves non-matching rows at their original "`, `"(possibly numeric) dtype while matching
rows become masked "`, `"strings -- a chunk-boundary-dependent output dtype."`, and the `"route:
the handler..."` fragment's XX-wrap sibling) without changing the inner text, so a substring
check for that inner text still passes. The matching UPPERCASE variants of the same five
fragments (mutants 38, 40, 42, 44, 46) are killed by exactly those substring assertions. The
`code`, `path`, and column NAMES (including the two-column join separator and the `"?"`
placeholder) are all independently pinned in
`tests/unit/execution/test_text_mask_chunked.py::TestWhenRejection`, so none of the 5 represents
an unverified behavior -- only unverified wording.

## The one-line admission (`CHUNK_SAFE_STRATEGIES` += `"text_mask"`)

Not run through mutmut: per the plan, the FK-admission and byte-parity tests are the grading
surface for a one-line set addition (a mutant deleting the entry would redden them, not survive
silently). Verified directly: temporarily removing `"text_mask"` from `CHUNK_SAFE_STRATEGIES` and
re-running `tests/unit/execution/test_text_mask_chunked.py` reddened 23 of 24 tests in
`TestFkSelfMaskRI`, `TestFkNegativeAndNamespaceIndependence`, and `TestAdmissionSurfaces` (every
FK edge that used to self-mask now raises `chunked_fk_parent_strategy_not_safe`, and the
route-admission tests raise `strategy_not_chunk_safe` instead of running chunked). The change was
reverted immediately after.

## Candidate findings

None. No mutation exposed a wrong admission verdict, wrong coded error, or a chunked/full-frame
divergence that current behavior does not already intend.

## Regenerate

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/_chunked_text_mask.py \
  --tests "tests/unit/execution/test_text_mask_chunked.py::TestWhenRejection" \
  --timeout 30 --readjudicate-killed
```

`source_paths` stays at the package root (mutmut 3.x quirk, see `_chunked_dgrn.py`'s ledger).
