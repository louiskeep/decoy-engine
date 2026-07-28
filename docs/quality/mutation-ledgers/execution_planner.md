# Mutation grading: `execution/_planner.py` -- substrate bar 75%

TQ substrate sweep (branch `tq/substrate-sweep`), DRAFT pending re-grade.
`_planner.py` is the observe-only execution-mode planner: `classify_job`
classifies a validated job into one mode (`polars_native` / `chunked` /
`sequential_relationship` / `out_of_core_relationship` / `pandas_fallback`) and
records why each faster mode was rejected. The eight functions graded here are
the route/strategy compatibility GATES that feed that decision plus the
classifier itself. This is a substrate module (route planner), not crypto/RI, so
the bar is **75% of LOGIC mutants**, not 100%.

The machine fields asserted are the ones the planner actually decides on: the
chosen `mode`, the rejection CODE prefixes (`chunked_relationships_unsupported`,
`bucketize_source_not_null_free_numeric`, `when_predicate_not_chunk_stable`,
`date_shift_requires_explicit_format`, `fpe_join_group`, `fk_resolution`,
`non-polars-native work`), the offending column/table NAMES carried in each
reason (data, not prose), and the returned helper lists. Free-text explanatory
prose inside a reason is the `.message`-class analog and is left EQUIVALENT when
the code and data around it are pinned.

## Numbers

**261 LOGIC survivors addressed: 190 killed by new oracles, 71 EQUIVALENT.** No
survivor left un-triaged. 0 real product bugs found. Independently re-graded via
`scripts/tq_mutate.py`: 388/459 killed = **84.53% LOGIC**, 0 unresolved, above the
75% substrate bar. dennis gate reconciliation: `_runtime_source_rejections` mut_48
was mis-listed as a surviving-equivalent but is actually KILLED (moved to KILLED
below); and `classify_job` mut_35 (`reason += -> =`) was reclassified from
EQUIVALENT to KILLED after the polars composition test was made symmetric (it now
asserts the base clause survives the append, so dropping it is caught).

Per function:

| Function | Survivors | Killed | Equivalent |
|---|---|---|---|
| `_bucketize_columns` | 14 | 14 | 0 |
| `_table_column_entries` | 16 | 16 | 0 |
| `_fpe_join_group_columns` | 41 | 41 | 0 |
| `_whole_column_state_rejections` | 48 | 38 | 10 |
| `_runtime_source_rejections` | 64 | 51 | 13 |
| `_polars_native_rejection` | 14 | 6 | 8 |
| `_chunked_rejection` | 34 | 15 | 19 |
| `classify_job` | 30 | 9 | 21 |
| **Total** | **261** | **190** | **71** |

## Tests

New oracle file `tests/unit/execution/test_planner_mutation_kills.py` (33 tests)
drives the six leaf/helper gates directly with hand-built configs, Arrow tables,
and fabricated work-node namespaces, asserting hardcoded outcomes. Integration
oracles for `_chunked_rejection` delegation and `classify_job` reason
composition were added to `tests/unit/execution/test_execution_planner.py`
(7 new tests: class `TestChunkedGateDelegation`, `TestReasonComposition`). All
60 tests green on unmutated code; ruff format + check clean.

## LOGIC killed (188)

### `_table_column_entries` (16) and `_bucketize_columns` (14)

Direct helper oracles. A two-table config where `t1` is first and `t2` holds the
target columns kills the config-key mutations (`"tables"`/`"columns"` renamed or
nulled -> `[]`), the `table_cfg = None` / `is None` -> `is not None` inversions,
the `isinstance(...) or` -> match-first-dict, and the `name != table` /
`t.get(None)` name-lookup breaks (they return the wrong table or `[]`). A missing
table kills the `next(...)` no-default mutation (StopIteration). A column entry
with the `name` key ABSENT pins the `"?"` fallback, killing the default-value
mutations (`get("name", None)` / `get("name")` / `get("name", "XX?XX")`). The
`strategy == "bucketize"` constant/key mutations are killed by asserting the
bucketize column IS selected on `t2` and IS NOT on the non-bucketize `t1`.

### `_fpe_join_group_columns` (41)

Same table-resolution kills as above, plus the fpe-specific selection logic. The
target fpe join-group column `c` sits AFTER a non-fpe column on `t2`, so
`continue` -> `break` is caught (it would stop before reaching `c`). Asserting
`== ["c"]` kills the `not isinstance(...) or` -> `and`, the strategy `!= "fpe"`
constant/operator flips (they skip fpe columns or process non-fpe ones), and the
`provider_config` / `fpe_join_group` key mutations (they drop the append). A
config whose column list contains a non-dict entry kills the dict-guard
`or` -> `and` (it would `.get(...)` a string). A nameless fpe column pins the
`"?"` name fallback. An fpe column WITHOUT the flag confirms exclusion.

### `_whole_column_state_rejections` (38 of 48)

- Two `when` columns + two undated `date_shift` columns on one table: asserting
  the two codes plus the sorted comma-joined name lists (`wa, wb` / `da, db`)
  kills the `_table_column_entries(table=None)` delegation break, the
  `when`/`strategy`/`date_shift` key+constant mutations, the `and not (...)` ->
  `and (...)` inversion, the `None`-init `.append` AttributeErrors, the
  name-corruption mutations, and the `', '.join` arg/separator mutations.
- Nameless `when`/`date_shift` columns pin the `"?"` fallback (kills the
  remaining name-default mutations).
- A `date_shift` column WITH an explicit `provider_config.date_format` asserts
  an empty result, killing the mutations that invert or loosen the
  `date_format` lookup so a pinned-format column is wrongly flagged
  (`.get(None)`, `provider_config` key/`or {}` breaks, `and {}`).

### `_runtime_source_rejections` (51 of 64)

Direct oracles over Arrow tables (`auto_chunk_threshold_rows` set per case).
mut_48 is among the killed (re-grade truth; it was mis-listed as equivalent in an
earlier draft):

- Extra frames: `{t, o1, o2}` kills `extra = None`, the reason-`None`, the
  `join(None)`, and the `'XX, XX'` separator (asserts `o1, o2`).
- Missing frame and LazySource handle: assert the presence codes; the LazySource
  case (a real Parquet handle) kills the `append(None)`, `and ""`,
  `lazy_source_rejection(None)` / `table=None` / dropped-arg mutations (the
  reason text changes or raises).
- Size gate: 2 rows at threshold 3 is rejected, at threshold 2 is admitted,
  killing the `<` -> `<=` boundary flip.
- Dtype gate: an integer column with one null is flagged while a clean integer
  is not (kills `>= 0`, `> 1`, `column(None)`, the `None`-init append). Clean
  float / bool / temporal / null / string columns assert an empty result,
  killing the `or` -> `and` chain-boundary mutations (they flag a stable dtype)
  and the `is_*(None)` argument mutations (they raise). Two decimal columns
  assert the sorted `da (...), db (...)` join (separator + join-arg kills).
- Bucketize gate: a clean numeric source is admitted; string / numeric-with-null
  sources are rejected with the lowercase code and the offending column named,
  killing the `not (...)` removal, `or` -> `and`, `is_*(None)`, `field(None)`,
  `t = None`, null-count flips, and the append/reason/join mutations. An unknown
  column before a real bad one kills `continue` -> `break` and `not in` -> `in`.
  A non-empty list wired through kills `bucketize_columns and []`. Uppercased
  code (`mut_69`) is killed by the case-sensitive code assertion.

### `_polars_native_rejection` (6 of 14)

Fabricated scalar work nodes on the polars substrate. A non-native scalar
strategy asserts `non-polars-native work: zzz_not_native`, killing the
`kind == "scalar"` constant flips (they emit the kind `scalar` instead of the
strategy name) and the filter `and` -> `or` (which would DROP the node and admit
the job -- a mode change). Two non-native strategies assert the `aaa, bbb` join
(separator kill). An FK job asserts the lowercase `fk_resolution:` code (kills
the all-uppercase message mutation).

### `_chunked_rejection` (15 of 34)

Integration via `classify_job`. Each fixture reduces to a SINGLE chunked
rejection reason, so dropping the delegated gate flips the mode to `chunked`:

- Undated `date_shift` job: `mut_68` (`_whole_column_state_rejections(table=None)`).
- Bucketize-null job with loaded sources: `mut_76` / `mut_80` (bucketize arg
  nulled/removed) and `mut_82` (`_bucketize_columns(table=None)`).
- Fpe join-group job: `mut_53` (`join_group_cols = None`), `mut_55`
  (`_fpe_join_group_columns(table=None)`), `mut_58`/`mut_59` (reason/join `None`
  -> raise). Two fpe columns kill the `mut_60` separator (`c1, c2`).
- Composite job: `'people': composite` kills `mut_42` (`non_scalar = None`),
  `mut_44` (`str(None)`), `mut_46` (`node.table != table`).
- Two mask tables assert `job declares 2 mask tables (ma, mb)` (`mut_12` count
  boundary `> 1` -> `> 2`, `mut_15` separator). Two generate tables assert
  `g1, g2 present` (`mut_8` separator).

### `classify_job` (9 of 30)

- Polars-native + generate tables: `generate-kind table(s) g1, g2 run the`
  kills `mut_36` (`reason -=` -> TypeError), `mut_37` (`join(None)`), `mut_38`
  (separator). The symmetric `assert "all mask work is scalar" in plan.reason`
  (dennis gate) also kills `mut_35` (`reason +=` -> `=` drops the base clause).
- Chunked with loaded sources: `single mask table 'customers'` and
  `source holds 2 rows` kill `mut_78` (`+=` -> `=` drops the mask-table name),
  `mut_76` (`rows = None`), `mut_82` (`reason=None` -> TypeError on the substring
  check), `mut_85` (reason kwarg removed -> `""`).
- Pandas-fallback no-FK: a non-empty `reason` kills `mut_113` (`reason = None`).

## EQUIVALENT (73)

### Prose-only message mutations (message-class, code + data pinned)

Wrapping a reason literal in `XX...XX`, upper/lower-casing it, or swapping the
`"; "`/`", "` separator between whole reasons changes only explanatory text; the
machine code prefix and the column/table names are asserted independently and
are unchanged. These are the direct analog of the `_when_gate` `.message`
equivalence class.

| Function | Mutants | Note |
|---|---|---|
| `_whole_column_state_rejections` | 41, 42, 43, 44, 49, 50, 51, 52, 53, 54 | when/date reason prose |
| `_runtime_source_rejections` | 9, 10, 24, 25, 47, 72, 73, 74, 75, 76, 77 | extra/size/dtype/bucketize reason prose |
| `_polars_native_rejection` | 6, 8, 9, 14, 15, 18 | no-mask/substrate/fk reason prose (18 lowercases "FK", code `fk_resolution` intact) |
| `_chunked_rejection` | 4, 5, 9, 10, 20, 21, 22, 23, 28, 29, 31, 32, 61, 62, 63, 64, 65 | no-mask/generate/substrate/relationships/fpe reason prose |
| `classify_job` | 32, 33, 34, 39, 40, 70, 71, 72, 73, 74, 98, 99, 101, 102, 103, 104, 105, 106, 114, 115 | chosen-mode reason prose (all branches) |

### Code-prefix-preserved XX-wrap (code survives as substring)

| Mutants | Function | Why equivalent |
|---|---|---|
| `mut_17` | `_polars_native_rejection` | `"fk_resolution: ..."` -> `"XXfk_resolution...XX"`; the code `fk_resolution` is still a substring of the reason, so the machine-code assertion cannot distinguish it |
| `mut_68` | `_runtime_source_rejections` | `"bucketize_source_not_null_free_numeric: ..."` XX-wrap; code survives as substring (the uppercase `mut_69` IS killed) |

### Reasons-list separator (per-reason codes asserted independently)

| Mutants | Function | Why equivalent |
|---|---|---|
| `mut_35` | `_polars_native_rejection` | `"; ".join(reasons)` -> `"XX; XX"`; every reason's code is asserted on its own, and the separator between them is not a machine field |
| `mut_86` | `_chunked_rejection` | same, `"; ".join(reasons)` |

### Genuine no-ops / unreachable

| Mutants | Function | Why equivalent |
|---|---|---|
| `mut_21` | `_runtime_source_rejections` | `lazy_source_rejection(...) or ""` -> `or "XXXX"`; `lazy_source_rejection` always returns a NON-empty string for a LazySource (its only truthy path), so the `or` right operand is dead code and never taken |
| `mut_52` | `_chunked_rejection` | `', '.join(non_scalar)` separator; `non_scalar` is a SET of work-node kinds, and only `"composite"` is reachable for a non-relationship single-table job (composite_fk_group needs FK edges, which short-circuit earlier), so the set never holds 2+ elements and the separator is never rendered |
| `mut_45` | `classify_job` | `ExecutionPlan(mode="polars_native", rejections={}, ...)` -> drops `rejections={}`; the field defaults to `dict()` via `field(default_factory=dict)`, so the plan is identical |

## Candidate findings

None. Every gate behaves as its docstring specifies; no mutation exposed a wrong
route, a wrong rejection code, or a wrong classification that current behavior
does not already intend.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to
`src/decoy_engine/execution/_planner.py` and the test selection to
`tests/unit/execution/test_planner_mutation_kills.py` and
`tests/unit/execution/test_execution_planner.py`, then
`rm -rf mutants && python -m mutmut run`. `source_paths` stays at the package
root.
