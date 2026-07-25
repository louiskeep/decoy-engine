# Equivalent-mutant ledger: `relationships/_graph.py`

TQ-0 pilot, 2026-07-25. Mutation run: 247 mutants, 220 killed, **27 survived**,
all equivalent (raw score 89.1%, logic-mutant score 100%). This ledger is the
auditable record behind the "all survivors equivalent" claim; every TQ module
that leaves survivors alive must ship one like it (playbook DoD).

Each survivor is behavior-preserving: no test can kill it because no input
produces an observably different result. Two classes, both verified against the
source.

## WORDING (23): error-message prose only

mutmut lowercases the first character of a string literal, upper-cases it, or
wraps it `XX...XX`. These literals are consumed ONLY inside a raised
`PlanCompileError`'s human `message`. They never become the error `code`, never
reach a `path=`, and never carry interpolated data (that arrives via f-string
`{...}` variables, which string-literal mutation cannot touch). Tests assert the
`code` and the load-bearing message substrings, so wording-only variants pass.

| Mutant | Function |
|---|---|
| `build_relationship_graph__mutmut_17,18,19,20,34,36,37` | orphan-conflict + wiring-guard message prose |
| `check_orphan_fk_policy_completeness__mutmut_44,45,47,48,49,50,51,69,109,110,123,124,125,126,127,128` | missing/invalid/duplicate/absent message prose |

## DEFAULT (4): no-op container default

`config.get("relationships", [])` and `entry.get("children", [])` mutated to a
`None` default (or no default). Each result immediately feeds an
`isinstance(x, list)` guard (`_graph.py:357`, `:400`): `[]` iterates zero times
and `None`/absent is skipped, collapsing to an identical empty `config_lookup`.
A missing key with a non-empty profile then raises the same
`orphan_fk_policy_missing` (`:450-464`). No input distinguishes `[]` from
`None`, so no test can kill these.

| Mutant | Mutation | Line |
|---|---|---|
| `check_orphan_fk_policy_completeness__mutmut_4` | `config.get("relationships", None)` | 356 |
| `check_orphan_fk_policy_completeness__mutmut_6` | `config.get("relationships")` (no default) | 356 |
| `check_orphan_fk_policy_completeness__mutmut_73` | `entry.get("children", None)` | 399 |
| `check_orphan_fk_policy_completeness__mutmut_75` | `entry.get("children")` (no default) | 399 |

## Regenerate

```
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut run
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut results
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut show <mutant-id>
```
