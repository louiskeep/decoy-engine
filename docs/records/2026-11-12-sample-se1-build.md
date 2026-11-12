Status: record

# SAMPLE SE1 build: the engine sample request (2026-11-12)

`decoy_engine.subset` can now describe a linked sample as a `SubsetRequest`. Nothing runs it yet: the stage (SE2) and the CLI come later.

## What changed

- `subset/_sample.py` (new): `SampleSpec`, `sample_request`, the row bounds and the 1,000,000 kept-row ceiling. With no `subset:` block and no root, every table that is never a parent of a retained edge gets a `head` seed and every retained edge goes `"upward"`; a pure cycle seeds its first table in config order. A root walks `"both"` in its own component. A `subset:` block keeps its seeded components and gets `cap=rows` on each seed.
- `subset/_edges.py`: `mask_relationships` is the generate-edge drop moved out of `subset_request_from_config`, and `sample_refusal_edge` names the first generated-parent to masked-child edge.
- `subset/_types.py`, `_seed.py`, `_api.py`: `SeedMode` gains `head`, `SeedSpec` gains `cap`; `select_seed_rows` no longer sends an unknown mode to `keys`.
- Fix round 1: a self-referencing table is child-free (`_sample.py`); `linked_neighbours` in `_edges.py` is the one reach walk for `scope_for` and the sample; a relationship table outside the mask tables raises `SUBSET_UNKNOWN_TABLE`; `one_failure_report` builds the one-failure report at both raise sites.
- `subset/_scope.py`: `SubsetRequest.sample`, `SubsetScope.head`; with a sample, no table is `whole`, and a head table with an edge raises.
- Exports, a changelog fragment and the CODEMAP lines.

## Tests

`tests/unit/subset/test_sample_request.py` and `test_sample_seed.py`, 36 cases covering AT-1 to AT-8a. Red first: 33 of 36 failed on assertions against stubs; the three that passed were the YAML refusals, which hold before and after.

## Mutations

Each applied alone to a throwaway checkout of source head `74ec077e`, `tests/unit/subset` only. All went red. Rows for `_sample.py` were re-run on that head; the rows that mutate other files were run on the first head and not repeated.

| Test | Mutation | Red |
|---|---|---|
| AT-1 | seed the parent | 7 |
| AT-2 | head by key order | 2 |
| AT-3 | cap before the filter | 1 |
| AT-4 | pick the last table | 1 |
| AT-4a | linked head seed to `head` scope | 7 |
| AT-4b | leave component 2 unseeded | 3 |
| AT-4c | seed the first table | 5 |
| AT-4d | drop the head-edge check | 1 |
| AT-5 | keep the generate edge | 1 |
| AT-5a | inspect post-drop relationships | 1 |
| AT-6 | accept 1001 | 1 |
| AT-6a | `isinstance` for `type(...) is int` (sample) | 2 |
| AT-7 | no unknown-root refusal | 2 |
| SE1-f1 | count a self-edge as a parent | 1 (the departments-first order) |
| SE1-f3 | skip the unknown relationship table check | 1 |
| AT-8 | `head` in `SeedModeLiteral` | 1 |
| AT-8a | fall-through `else` | 1 |

## Chosen where the plan was silent

- The invalid-knob refusal is `ExecutionError` from the `_execution_error` leaf: `SubsetConfigError` is mapped to the `config` namespace by the reason sentry, so it cannot carry `runtime.invalid_execution_knob`.
- An unknown or generate root raises `SubsetPreflightError` with a one-failure report, the pattern `relationships_from_config` uses, because that family maps to the `subset` namespace.
- `sample_refusal_edge` returns a plain tuple `(parent_table, parent_columns, child_table, child_columns)`.
- `sample_request` raises the invalid-knob code for a generated parent of a masked child; the CLI refuses earlier through `sample_refusal_edge`.
- `select_subset` still preflights every seed table, so SE2 passes a request without the head seeds of tables it keeps out.
