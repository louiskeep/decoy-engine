# Module Test-Quality Playbook

Established by TQ-0 (Test-Quality Program, 2026-07-25) on the pilot module
`relationships/_graph.py`. This is the fixed procedure every later phase copies:
one agent, one module, mutation-graded. Read the program plan in
`docs/plans/2026-07-21-test-quality-program.md` for why; this doc is the how.

## The problem, restated

Coverage says a line RAN under test. It does not say a test would CATCH a bug on
that line. The pilot proved the gap directly: `_graph.py` had 91% line/branch
coverage from example-based tests, yet 65 of 247 planted mutations SURVIVED
(a 73.7% mutation score). Coverage was green over code the tests could not
actually defend.

The fix is oracles independent of the implementation. A characterization test
runs the code and asserts on what it saw, so it passes by construction and locks
in whatever the code already does. An oracle test states what MUST hold and lets
a tool hunt a violation. Mutation testing is the objective grade: inject a bug,
check that a test fails.

## The per-module loop

1. **Pick the module and its covering tests.** Name the invariant it owns and
   its blast radius (why a bug here matters).
2. **Baseline.** Measure current coverage and current mutation score with the
   EXISTING tests only. Record both. The mutation score is the number that
   matters; coverage is context.
3. **Write oracle tests** (property, metamorphic, differential; see below) for
   the module's invariants, in `tests/property/` for reusable invariants or the
   module's `tests/unit/...` dir for targeted cases.
4. **Re-measure mutation.** The delta is the lift. Inspect every surviving
   mutant: each is either a real test gap (add an oracle that kills it) or an
   equivalent mutant (a change with no observable effect; record it, do not
   chase it).
5. **Clear the bar.** Iterate steps 3-4 until the mutation score clears the
   module's bar. Surface any real bug the oracles found along the way.
6. **Report** baseline, lift, final score, bugs found, and any equivalent
   mutants left alive with the reason.

## Oracle patterns (with the pilot's worked example)

- **Property / invariant** (Hypothesis). State what holds for all inputs, let
  Hypothesis search for a counterexample. Pilot: "the topological ordering
  places every parent node before its child node" (`test_ordering_respects_
  every_edge`) is the RI-critical invariant; a violation would let a child mask
  before the parent whose keys it must match.
- **Metamorphic.** For outputs with no single ground-truth, assert a relation
  between runs. Pilot: shuffling the input relationships must leave `edges` and
  `ordering` byte-identical (`test_input_order_does_not_change_output`); an exact
  duplicate relationship must not change the graph (`test_duplicate_relationship_
  is_idempotent`). These are what keep cross-process golden fingerprints stable.
- **Composition.** Two functions that are each other's contract must compose.
  Pilot: `check_orphan_fk_policy_completeness` returns a lookup that
  `build_relationship_graph` accepts without the wiring-bug error
  (`test_completeness_roundtrip_composes_with_build`).
- **Differential / oracle corpus.** Against a slow-but-obviously-correct
  reference or a frozen corpus. Engine already does this (the 346k-row
  byte-identical `code_set` oracle); reach for it where a reference exists.

Cite each invariant's source (the module docstring or spec section) in the test
module docstring, matching the engine "cite the source pattern" rule.

### The Hypothesis strategy pattern

Generate the domain, not one example. The pilot's `dag()` strategy builds a
random acyclic FK graph by indexing tables `t0..t{n-1}` and only emitting edges
from a lower to a higher index (acyclic by construction), with a per-edge unique
child column so two edges into one child never collide. Build a matching
adversarial strategy for the negative invariant (a hand-built 2-cycle for
`test_cycle_is_rejected`). Reuse the existing `audit` Hypothesis profile
(`max_examples=300`, `deadline=None`, `print_blob=True`) so a counterexample is
replayable.

## Tooling

### Mutation grading (mutmut 3.6)

Config lives in `pyproject.toml` `[tool.mutmut]`. The copy-per-module pattern:

```toml
[tool.mutmut]
source_paths = ["src/decoy_engine"]              # keep at the PACKAGE root
only_mutate = ["src/decoy_engine/relationships/_graph.py"]   # narrow per module
pytest_add_cli_args_test_selection = [
    "tests/unit/relationships/",
    "tests/property/test_ri_graph_invariants.py",
]
```

`source_paths` MUST stay at the package root: mutmut 3.x copies exactly that
subtree into its `mutants/` working dir, and the covering tests' `conftest.py`
transitively imports sibling package modules (via `tests/_dp_cert.py`). Scoping
`source_paths` to a single file starves the copy and the run dies with a generic
`BadTestExecutionCommandsException` (a masked `ModuleNotFoundError`, pytest exit
4). Narrow the actual mutation with `only_mutate`, never `source_paths`.

Run and inspect:

```
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut run
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut results
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut show <mutant-id>
```

`mutants/` is mutmut's working tree; keep it untracked (it is git-ignored via
the usual build-artifact patterns; confirm before committing).

### Coverage (and the duckdb gotcha)

`pytest --cov` breaks in this repo: coverage's `source=` filtering reloads
modules, and duckdb's compiled `_duckdb._sqltypes` submodule does not survive the
reload (`'_duckdb' is not a package`). The fix is to import duckdb BEFORE
coverage starts. For a one-off measurement, run coverage in-process with duckdb
pre-imported (see `scripts/` or the pilot's runner). TQ-3 owns the CI-side fix
(a `.pth` / `sitecustomize` pre-import, or a coverage plugin) before the
diff-coverage ratchet lands.

## Definition of done and bars

A module is done when its mutation score clears its bar WITH the oracle layer
present, not when coverage hits a number. Bars are measure-first: baseline the
module, then set the bar at `max(baseline + 15 points, 75%)`. Two families are
exempt from measure-first and MANDATORY at 100%: crypto and RI/FK. Record any
surviving equivalent mutant with the argument for why it is equivalent; never
silently drop it.

### Scope the score to LOGIC, not error-message wording

mutmut mutates every string literal (it lowercases the first character and wraps
the string in `XX...XX`). For a module heavy in error messages (a config
validator like `check_orphan_fk_policy_completeness`), this floods the survivor
set with mutants that change only the TEXT of an error, not behavior. mutmut 3.x
cannot disable string mutation selectively (`should_mutate` is file-level only),
so the policy is:

- The bar (including the 100% crypto/RI mandate) applies to LOGIC/behavior
  mutants: operators, comparisons, boundaries, control flow, constants that
  affect an outcome, and the `code`/type of a raised error.
- A mutation that changes ONLY error-message prose (not the raised `code`, not a
  `path`/field a caller asserts on, not control flow) is classified EQUIVALENT
  for scoring. It is not a correctness defect; asserting exact message wording is
  brittle and low-value. Tests still SHOULD assert the load-bearing parts of a
  message (the offending value, the enumerated valid options, the `path`), which
  kills the meaningful string mutants and leaves only pure-wording ones.
- Report both numbers: the raw mutmut score AND the logic-mutant score (raw minus
  equivalent message-wording survivors), with the equivalent set listed. The
  logic-mutant score is the one the bar is applied to.

## Pilot result (`relationships/_graph.py`)

| Metric | Existing tests | + property/metamorphic layer |
|---|---|---|
| Line/branch coverage | 91% | 99% (property suite alone) |
| Mutation score (raw) | 73.7% (65/247 survived) | 89.1% (27/247 survived) |
| Mutation score (logic) | -- | **100%** (0 logic mutants survive) |

Bugs found in `_graph.py`: none. The module is correct; every logic mutant is
killed, so the RI 100% bar is met on logic mutants.

Equivalent mutants left alive (27, all behavior-preserving, none a test gap):
- 23 error-MESSAGE mutations (mutmut lowercases the first char or wraps a
  message string in `XX...XX`): they change only the prose of a raised error,
  not its `code`, `path`, or control flow. Tests assert the `code` and the
  load-bearing message parts, so only pure-wording variants survive.
- 4 default-value mutations: `config.get("relationships", [])` and
  `entry.get("children", [])` mutated to a `None` default (or no default). The
  next line is `if isinstance(x, list):`, which treats `[]` (iterate nothing)
  and `None` (skip) identically for the outcome, and a missing key with a
  non-empty profile raises the same `orphan_fk_policy_missing` either way. No
  input can distinguish them, so no test can kill them.

The pilot's tests are `tests/property/test_ri_graph_invariants.py` (28 tests);
copy its structure, the `dag()` strategy shape, and the `[tool.mutmut]` block for
the next module.
