# STORM engine r6: phase envelope, four MEDIUM fixes

Base `c97e69f0` (round 5 gate: GO-WITH-FIXES, no BLOCKER/HIGH). Engine only.

## Red first
Tests were added before any `phase.py` change and run against `c97e69f0`: **30 failed, 124 passed**
(81 pre-existing tests still green). Failing groups: gate/canonical encoding (huge int), forged
oversized ints, deep nesting (`RecursionError`), raising-repr set elements, shared default
coverage, malformed identity (`TypeError`), `FrozenMap(None)`, and the hostile-input catch-all.

## Fixes
| # | Fix |
|---|-----|
| 1 | `to_wire` now proves the wire form encodes (`_encode`); the gate calls `canonical_bytes`. Encoding failures become `PhaseContractError`. Explicit contract bounds added: integers must fit signed 64-bit, nesting depth <= 64 (freeze, thaw, FrozenMap, coverage `rows_examined`). |
| 2 | Set elements are validated by exact type (None/bool/int/float/str) and sorted by `(type rank, value)`; nothing foreign is compared or repr'd. Other elements (including tuples in sets) raise fixed-text `PhaseContractError`. |
| 3 | `coverage` uses `field(default_factory=PhaseCoverage)`. |
| 4 | `identity` accepts only Mapping/tuple/list; `FrozenMap` iteration failures are contained; all messages are fixed text. A `_contained` wrapper converts any other `Exception` from `freeze`, identity/str/reason normalizers, `to_wire` and `canonical_bytes` into fixed-text `PhaseContractError`. |

Behavior changes to note: sets of tuples no longer freeze (use a list); ints beyond 64-bit are refused.

## Gates (green on this commit)
- `ruff check .`: pass. `ruff format --check` on touched files: pass (16 unrelated files elsewhere were
  already unformatted before this change).
- `mypy src/decoy_engine/phase.py`: no errors in `phase.py`; the sandbox lacks numpy, so mypy reports
  unrelated import-not-found errors in other modules.
- `tests/unit/phase`: 154 passed (81 prior + new r6 tests incl. the hostile-input catch-all).
- `tests/sentry` (includes module-size and public-import-boundary): 3046 passed in the combined run.
- Proof manifest (`tests/sentry/test_proof_manifest.py`): 8 passed on this commit. The failure reported
  earlier did not reproduce for the reviewer (passed on HEAD and on `86cd474a`) and did not reproduce here.

`error_result` recording `type(exc).__name__` stays deferred to F31.
