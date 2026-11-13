# POSTSCAN-STREAM PS-2b0 build record: comparable key type families

Status: record

Plan: `docs/plans/2026-11-12-postscan-stream.md` on `plan/postscan-stream` (section 4.2.1, AT-35, AT-36).
Branch `feat/postscan-ps2b0`, cut from `origin/main` c38f17cd.

## What was built

- `_comparable` returns `integer` for every integer width, `string` for string, large_string and
  a dictionary of either, and the exact type text (large_binary folded to binary) otherwise.
- `keys_comparable(parent, children)` is the one comparison; `key_facts` uses it. It reads schemas
  only.
- `key_facts` picks one DuckDB type per label from every schema in the call: the declared type
  when all agree, `VARCHAR` for the string family, `BIGINT` for integers, `HUGEINT` when a
  `uint64` is present. No Arrow cast touches a key value.

## Decisions the plan left open

- Oracle for AT-35: the same data with both ends as `decimal128(38, 0)` (integers) or plain
  `string`, which takes the declared-type path that never meets two types.
- Comparable but not identical outside the two families (binary against large_binary): the
  parent's declared type is kept, as before.
- AT-35 shows "exactly one orphan" for `uint64` `2**63 + 5` against `int64` `-(2**63) + 5` and
  `1`; the cell gives the parent `2**63 + 5` and `1`, so the `1` matches and the negative value is
  the single orphan.

## AT-38

Pending. `check_keys` is not on main, and the plan gives no hand-over mechanic. The union-shaped
cell of AT-35 covers the same defect at the `key_facts` level.

## Red and mutations

The test commit ran against the old source: 62 failed (56 integer pairs, typed-empty, uint64,
dictionary, union, `keys_comparable`, AT-36). Mutations on the source head, each in a detached
copy:

| Mutation | Red tests |
|---|---|
| tables created from the declared types (first-schema load) | union-shaped parent |
| `uint64` cast to `int64` in Arrow before insert | `uint64` against `int64`; integer pairs |
| exact type text in `_comparable` | 62 tests, including AT-36 |

## Fix round

- One owner for the family rule: `_family(t)` in `profiling/_scan_integrity.py` returns
  `"integer"`, `"string"` or `None`. `_comparable` and `_duck_types` both call it; `_duck_types`
  picks the DuckDB type from the token. Behavior is unchanged (15 added, 17 removed source lines).
- Guard test `test_comparable_pair_never_falls_back_to_the_declared_type`: every comparable pair of
  different types among the integer widths and string encodings gets a non-`None` type from
  `_duck_types`. It passes on the old code too (a characterization guard); its red is the mutation.
- The typed-empty cell now reads `duckdb_columns()` through `_before_fetch` (a second cursor, since
  the first still holds the orphan result) and asserts `parent.k0` and `child.k0` are `HUGEINT`.
- Run on the Mac of `tests/unit/profiling`, `tests/unit/detection_verification/postscan` and
  `tests/sentry`: the first run had 1 failure of mine (the probe reused the busy connection, fixed
  in the last commit) plus the 10 expected `test_import_layers.py` failures. After the fix the
  family test file runs 140 passed, 76 skipped.
- Mutation: `_family` stops recognizing `large_string` while `_comparable` still accepts it on its
  own. The guard fails for `large_string` against `string` and against the dictionary string;
  `test_keys_comparable_reads_schemas_and_opens_no_connection` fails as well. Not committed.
