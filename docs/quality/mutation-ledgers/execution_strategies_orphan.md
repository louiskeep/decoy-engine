# Mutation grading: `execution/_strategies/_orphan.py` -- LOGIC-100%

TQ step-4 pass, graded 2026-07-26. `_orphan.py` is the orphan-row / foreign-key
handler in the RI family: `resolve_fk_keys` walks each child FK cell and decides,
per `orphan_policy`, whether to remap it (via an injected `remap_fn`), preserve it,
warn, fail closed, or cascade it to a `RowError` when its parent key errored;
`gather_errored_parent_keys` builds the errored-parent cache lookup;
`cascade_row_errors` emits one `RowError` per cascaded row; `make_remap_fn`
resolves the parent node + strategy handler (fail-closed on either missing) and
sets/restores the current parent table around the handler run.

Because RI is worst-blast-radius, the bar is 100%: every LOGIC survivor killed.

**Grade scope: FOCUSED selection only.** mutmut ran against `_orphan.py` with the
test selection restricted to `tests/unit/execution/test_orphan_fk.py`. Integration
and pipeline suites that also exercise orphan handling were NOT in the selection,
so the survivor count is a conservative lower bound: some mutants counted
"survived" here may be killed by tests outside this file. The existing suite only
drove the adapter happy path, so the S2 cascade branch, `gather_errored_parent_keys`,
`cascade_row_errors`, and `make_remap_fn`'s error/restore paths were never
executed; the new tests call those helpers directly.

## Numbers

**159 mutants: 90 killed (57% baseline), 69 survived -> 142 killed after this
pass, 17 EQUIVALENT.** LOGIC-mutant score 100%. 0 timeouts (the strategy suite is
fast enough that surviving runs finish under mutmut's per-mutant limit, unlike the
`_planner` substrate; see tq-findings.md #8).

- **52 LOGIC survivors killed** with new tests in `test_orphan_fk.py`
  (27 tests total in the file after the pass). 0 real bugs found.
- **17 EQUIVALENT survivors** left alive (all message/reason prose, or `zip`
  `strict=` on operands built in lockstep). All verified behavior-preserving.

## LOGIC (52): killed by new tests

All killing tests live in `tests/unit/execution/test_orphan_fk.py`.

### `resolve_fk_keys` (19)

| Mutants | Mutation | Killed by |
|---|---|---|
| `mut_16` | RI cascade guard `key in errored_parent_keys` -> `key not in` | `test_errored_parent_key_cascades_to_none_with_trigger` |
| `mut_17` | cascaded masked value `None` -> `""` | same |
| `mut_18` | `cascade.append(tuple)` -> `None` | same |
| `mut_19` | cascade loop `continue` -> `break` (strands later orphan rows) | `test_cascade_does_not_halt_later_rows` |
| `mut_36`, `39`, `40` | REMAP `zip(..., strict=True)` -> None / removed / False (a short `remap_fn` would silently drop an orphan remap; must fail closed) | `test_short_remap_result_fails_closed` |
| `mut_54` | WARN detail `provider` -> None | `test_warn_emits_single_aggregated_warning` |
| `mut_55`, `59` | WARN detail `column` -> None / removed | same |
| `mut_65`, `66`, `67`, `68`, `70`, `71`, `72`, `73` | WARN `detail` dict key-name mutations | same (exact-dict assert) |
| `mut_64` | WARN column join `","` -> `"XX,XX"` (only observable on a composite child key) | `test_warn_column_joins_composite_child_columns` |

### `gather_errored_parent_keys` (10)

| Mutants | Mutation | Killed by |
|---|---|---|
| `mut_1` | empty return `{}` -> `None` | `test_none_cache_returns_empty_dict` |
| `mut_2` | `is not None` -> `is None` | `test_collects_keys_with_their_triggers` |
| `mut_3`, `4` | `cache_key` -> None lookups | same |
| `mut_8`, `9`, `10`, `11` | `setdefault` key/trigger corruptions | same |
| `mut_5`, `7` | `.get(k, {})` -> `.get(k, None)` / `.get(k)` (crash on a missing cache entry) | `test_absent_cache_key_contributes_nothing` |

### `cascade_row_errors` (7)

| Mutants | Mutation | Killed by |
|---|---|---|
| `mut_1`, `2`, `3` | `RowError` column / row_index / trigger -> None | `test_builds_one_row_error_per_cascaded_row` |
| `mut_5`, `6`, `7`, `8` | those kwargs removed -> `TypeError` (RowError fields all required) | same |

### `make_remap_fn` (16)

| Mutants | Mutation | Killed by |
|---|---|---|
| `mut_8` | RI parent-node guard `or` -> `and` (None node -> `AttributeError` instead of fail-closed `ExecutionError`) | `test_missing_parent_node_fails_closed` |
| `mut_11`, `13`, `15`, `16` | `orphan_remap_parent_missing` code mutations | same |
| `mut_22`, `24`, `26`, `27` | `unsupported_strategy` code mutations | `test_missing_handler_fails_closed` |
| `mut_30`, `33`, `37`, `38` | parent-table not set correctly during handler run (multi-table evidence attribution) | `test_parent_table_is_set_during_run_then_restored` |
| `mut_50`, `54`, `55` | `current_table` not restored to prior in `finally` | same |

(Defense-in-depth: `test_parent_map_hit_takes_precedence_over_errored_key` pins the
documented branch-1 precedence limitation -- a `parent_map` hit wins over an
`errored_parent_keys` entry.)

## EQUIVALENT (17)

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `resolve mut_26`, `28` | FAIL `ExecutionError(message=None)` / removed | `.message` defaults to `""`; `code="orphan_fk_violation"` unchanged and asserted. Prose. |
| `resolve mut_44`, `47`, `48` | PRESERVE/WARN `zip(orphan_positions, orphan_keys, strict=...)` None/removed/False | both operands are appended in lockstep in the same loop, so their lengths are always equal and `strict` can never fire. (Contrast REMAP's zip, whose second operand comes from the injected `remap_fn` -- that one IS killable, above.) |
| `cascade mut_4` | `RowError(reason=None)` | `.reason` is documented human-readable, never machine-consumed (copied verbatim into `RowErrorRecord`). Prose. |
| `cascade mut_9`, `10`, `11`, `12`, `13` | reason string case / `XX`-wrapping | same prose rationale. |
| `make_remap mut_12`, `14`, `17`, `18` | parent-missing `ExecutionError` message None/removed/prose | `.message` prose; the `code` is asserted. |
| `make_remap mut_23`, `25` | unsupported-strategy `message` None/removed | same. |

## Gate

Dennis batch gate: **PASS (APPROVE)**, 0 P0 / 0 P1. All 17 equivalents verified
behavior-preserving against source (the `zip(strict=)` claim confirmed: both
operands are appended in lockstep at `_orphan.py:140-141` with no intervening
branch/continue/raise, so lengths are invariably equal); all 52 kills confirmed
genuine. Two optional P2 defense-in-depth items left open (non-blocking):
- P2-1: `test_builds_one_row_error_per_cascaded_row` could also assert
  `errors[0].reason.startswith(...)` (reason serializes to the quarantine JSONL,
  though it is not machine-consumed -- so `cascade mut_4/9-13` stay EQUIVALENT).
- P2-2: no direct oracle pins the documented cascade-under-FAIL invariant
  (`_orphan.py:128-136`: a cascaded key under `OrphanPolicy.FAIL` is quarantined,
  not raised); correct by construction and no mutant survived on it, but a
  one-line test would guard it against a future refactor.

## Regenerate (any shell)

Repoint `[tool.mutmut]` `only_mutate` to
`src/decoy_engine/execution/_strategies/_orphan.py` and the test selection to
`tests/unit/execution/test_orphan_fk.py`, then `rm -rf mutants && python -m
mutmut run`. `source_paths` MUST stay at the package root (see the playbook's
copy-per-module note).
