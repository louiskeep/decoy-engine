# Mutation grading: `transforms/_codeset_index.py` -- LOGIC-100%

TQ step-4 pass, graded 2026-07-26. `_codeset_index.py` (122 LOC) builds the
selection indexes for `code_set` masking (`build_selection_indexes`,
`SelectionIndexes`, `hole_candidate_count`, `hole_resolve`) -- the data structures
that map a source code to its deterministic replacement within a corpus.

**Grade scope: FOCUSED selection** (`tests/unit/transforms/test_code_set.py`,
`-k "not docs_strategies_md"`).

## Numbers

**54 mutants: 54 killed, 0 survived.** LOGIC-mutant score 100%. 0 timeouts.
No new tests required -- the existing `code_set` suite already exercises every
mutable branch of the index-building and hole-resolution logic. 0 EQUIVALENT.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `transforms/_codeset_index.py`, selection
to `tests/unit/transforms/test_code_set.py` + `-k "not docs_strategies_md"`, then
`rm -rf mutants && python -m mutmut run`.
