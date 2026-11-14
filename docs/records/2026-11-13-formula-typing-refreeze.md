Status: record

# Generate formula typing refreeze

Date: 2026-11-13. Slice E1 of GEN-FORMULA-SEQ (plan `docs/plans/2026-11-13-gen-formula-seq.md`,
head `792e9e15`), built from engine `origin/main` `3a30eb89`. Owner decision OQ-1 (a): a formula
reads a number as a number, and the text spellings are refused at compile.

This is the one authorized change to frozen digests for the slice. Every other parity and golden
fixture is byte-identical, and no seed draw moves (`SEED_PROTOCOL_VERSION` is unchanged).

## Re-pinned digests

| Where | Digest | Before | After | Why |
|---|---|---|---|---|
| `tests/unit/generation/unique/test_gen_unique_old_input_bytes.py`, `all_basic_types` | `cells` | `c7ea7449a95cb1740c336f96421aa53acb924b3760ec007f1bea1e81f5434fe6` | `a6cbb34f9386440812a583166e698781007d426281cca2dc61c0281e55cbcc0f` | `score` (`pyint`) is read by `f: score * 2`. `f` was repeated text and is now the product. `score` stays `int64` in the output where the old reader rule made it text. |
| same, `all_basic_types` | `config_json` | `c82643e094caf6665bf2a292cda6561eb89766a1292e244a818c86ce2b45f074` | unchanged | the plan config does not change |
| same, `all_basic_types` | `fingerprints` | `d25f1cc532c7beae91e731d26bcb92d754e62641192ee872bd4097a74a458fdd` | unchanged | no seed draw moves |
| same, `all_basic_types_unlisted_formula` | `cells` | `1f5c4a2f626e7917e8ba2b4978ccac968c7f9234076364d40a9cfe74ef975cca` | removed | the inline `id + score` read is refused at compile; a run that bypasses the check now raises `generate_formula_evaluation_failed`, asserted in place of a digest |
| `tests/parity/_f0_expected.py`, fixture `formula`, table `formula` | `schema` | `427da4841cb278415e1bc4e010bc1773e64568481996a167d357d94bab58628d` | `1cec7e708bfc56d468059910dbd20c8a59dc92de68f676b5f88450a3c6348ba0` | the `bad` column left the fixture |
| same | `table` | `1e934ccf90c1bc2211fdfe9f6171fdde380146d65a8f0d10aefe274e312dfdcb` | `fa26d49b6d942d8bbc85f94536335b2cb044a5e0cc07b3a065463907603d18e5` | same |
| same | per-column digests | seven columns | the same seven, unchanged; `bad` removed | `bad: undefined_name + 1` raised on every row and wrote nulls. GEN-STRICT refuses it at compile, and an unevaluable formula now fails the run. |
| `tests/fixtures/phase_f_fr0/manifest.json`, `formula` | `envelope_digest` | `32c2dab9c092732d2917a9e962e1ba21417a4eb4cd5ce964bcd0b54c28727e70` | `83e060a2ab9d0a93e2f6605a95fa5b48a06415141ff67db7203776aaa656785a` | the fixture config lost one column |

The manifest was regenerated with `python -m tests.parity._fr0_refreeze`. Its only differences from
main are the `formula` entry, `re_frozen_on` and the `refrozen.formula` record. No other fixture
entry moved.

`tests/parity/_f_audit_cells.py` `cfg_formula_referenced`: `seq` is now `int64` in the output (it was
`string`) and the `rfm` text is byte-identical. That file is a mutation audit with no frozen digest,
so nothing was re-pinned.

## Output type changes seen by the suites

| Config | Before | After |
|---|---|---|
| plain sequence read by `'ORD-' + str(id)` | `id` is `string` | `id` is `int64` |
| `pyint` read by `score * 2` | `score` is `string`, `f` is repeated text | `score` is `int64`, `f` is the product |
| nullable numeric categorical read by `str(c)` | `'1.0'`, `''` on a null row | `'1'`, null on a null row |

## Existing tests that changed (plan 7b)

Each old expectation pinned a defect: a null for a failure, or text for a number.

| Test | Change |
|---|---|
| `tests/unit/execution/test_expression_log_redaction.py`, four formula tests | assert `GenerationError` with the code and a fixed message, no value in the error or the log |
| `tests/unit/test_v2_generation.py::TestFormulaParityV1::test_inline_with_random` | formula is `randint(1, 100)`; V1 == V2 is kept |
| `tests/parity/test_numeric_typing_readers.py` | text spellings leave the success cases; reader table row for `formula` is `True`; the nullable test asserts null on a null row |
| `tests/unit/transforms/test_derived.py` | `failure_text` replaces `evaluation_failure_text`; expected text is byte-identical |
| `tests/parity/test_phase_f_f0_invariants.py` | the reversed post-pass wrapper forwards the new keyword |

One change outside the plan's 7b list, reported in the PR: `test_removal_propagates_along_a_chain_of_relationships`
used a formula as its text reader. It now uses `group_key.group_by`, which is still an unsafe reader,
so the assertion (`numeric_plan(raw) == {}`) is unchanged.
