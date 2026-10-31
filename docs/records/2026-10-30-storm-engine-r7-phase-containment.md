# STORM engine r7: one containment boundary for every public envelope entry point

Base `9b556536` (round 6 gate: GO-WITH-FIXES, 2 MEDIUM). Engine only.

## Correction to the r6 record
The r6 record said the new tests were red first. That is only partly true. Running the r6 tests
against `c97e69f0`: `test_freeze_set_of_mixed_primitives_is_deterministic` passed outright, and
42 parametrized cases of other r6 tests passed too (30 of `test_forged_fields_never_escape_as_other_exception`,
10 of `test_hostile_inputs_only_ever_raise_contract_error`, 2 of
`test_malformed_identity_is_contract_error_with_fixed_text`). The 30 failing items were real, but
"every new test fails before" was not.

## Red first (tests written before any `phase.py` change, run on `9b556536`)
6 failed, 155 passed. Seven new items:

| Test | On `9b556536` |
|------|---------------|
| `test_every_envelope_field_is_walked_and_only_raises_fixed_contract_error` | FAIL: 42 distinct escapes |
| `test_catch_all_detects_containment_removed_from_a_constructor[FrozenMap-__new__]` | FAIL: constructor had no containment to remove |
| `test_catch_all_detects_containment_removed_from_a_constructor[PhaseCoverage-__post_init__]` | FAIL: same |
| `test_frozenmap_rejects_subclassed_pairs_and_primitives_with_fixed_text` | FAIL: `RuntimeError` from a tuple subclass's `__len__` |
| `test_unknown_header_and_reason_errors_never_echo_the_input` | FAIL: `unknown status '<input>'` |
| `tests/sentry/test_phase_containment.py::test_every_public_phase_entry_point_is_contained` | FAIL |
| `tests/sentry/test_phase_containment.py::test_sentry_flags_an_uncontained_function_and_constructor` | **already green** (it tests the sentry, not `phase.py`) |

The 42 escapes include the two MEDIUM 1 cases (a tuple subclass's `__len__`, a raising
`__lt__` on an accepted subclass) and two leak classes the r6 catch-all never reached:
- the unknown `schema_version`, `phase`, `status` and reason errors echoed the input with `!r`
  (`PhaseResult.schema_version`, `PhaseEvent.phase/status/schema_version`, reasons);
- foreign code raising `PhaseContractError(<input>)` itself went through `_contained` unchanged,
  because the wrapper re-raised every `PhaseContractError`.

## Fix (structural)
- One boundary. `_contained` is the only place a foreign escape becomes an error. It passes
  through only `_RejectedError`, the private subclass this module raises with fixed text. Any other
  `Exception`, including a foreign `PhaseContractError`, becomes `_RejectedError("malformed phase evidence")`.
- It wraps every public entry point: `FrozenMap.__new__` (the whole body),
  `PhaseCoverage/PhaseResult/PhaseEvent.__post_init__`, `freeze`, `to_wire`, `canonical_bytes`,
  `satisfies_required_gate` and `error_result`. The private helpers are no longer wrapped one by one,
  so removing the boundary from a constructor is visible (see the mutation test).
- Exact types. One helper, `_is_scalar`, does exact-type scalar checks for `FrozenMap`, `freeze`,
  `_thaw`, set ordering and `_require_frozen`, so a subclass of `int`/`str`/`float`/`bool` is refused
  with fixed text. `FrozenMap` and identity entries must be exact `tuple`s, and mapping keys exact `str`.
  **Corrected (V1 close-out):** at `7f60f21` this was not true for containers. `freeze` matched
  `isinstance(value, (list, tuple))`, so a `list`, `tuple` or `FrozenMap` subclass was accepted and its
  `__iter__` ran (a `FrozenMap` subclass was silently re-frozen as a list). The close-out makes `list`,
  `tuple` and `FrozenMap` exact in `freeze`. `freeze` still accepts any `Mapping` and any
  `set`/`frozenset` subclass, and `identity`, `reasons` and the coverage scope fields still accept
  `list`/`tuple` subclasses before checking each element by exact type.
- Fixed text. The header and reason errors no longer include the value.
- Sentry. `tests/sentry/test_phase_containment.py` walks every public class and function in `phase.py`.
  Each must use `_contained`, matched by the wrapper's code object. A class must define a contained
  `__new__` or `__post_init__`. The sentry also runs against a fake module to show it catches a gap.

## Proof
- The catch-all walks `dataclasses.fields` of every public dataclass in `phase.py`, so a new field
  or envelope type is covered automatically. It asserts that the set of exercised fields equals the
  declared set: 22 fields, including `PhaseCoverage.limits/omissions`,
  `PhaseResult.schema_version` and `PhaseEvent.phase/status/schema_version`.
- Each field is passed through the constructor and also forged with `object.__setattr__`. Each one
  is then run through `to_wire`, `canonical_bytes` and `satisfies_required_gate`. `freeze` and `FrozenMap`
  are exercised directly.
- Hostile set:
  - raising `__repr__`, `__len__`, `__iter__`, `__eq__`, `__hash__`, `__lt__` and `__getattr__`, on
    `object`, `int`, `str`, `float`, `tuple`, `list` and `dict` subclasses;
  - a raising `__getattribute__`, and a foreign `PhaseContractError` raiser;
  - huge ints, None, `Path`, exception values, marker strings and marker `ReasonRef`s;
  - depth-5000 nesting, cycles, NaN, bytes and `object()`.

  Each value is placed bare and inside a tuple, list, dict and pair, and inside a set only when it
  is hashable (**corrected, V1 close-out:** values whose `__hash__` or `__eq__` raises never got
  the set placement, because `_r7_variants` skips a set it cannot build).
- Pass rule: an operation either succeeds or raises `PhaseContractError`, and the error text never
  contains the marker.
- Non-vacuity: the mutation test strips the containment out of two different constructors
  (`FrozenMap.__new__` and `PhaseCoverage.__post_init__`) through `__wrapped__`. The catch-all then
  reports escapes: 43 and 24.

## Behavior changes
- `freeze` and `FrozenMap` refuse subclasses of the scalar primitives, `IntEnum` and `str` enums
  included. No in-repo caller passes them. **Corrected (V1 close-out):** at `7f60f21` `freeze` still
  accepted `list`, `tuple` and `FrozenMap` subclasses; that is fixed in the close-out (see above).
- Error text for unknown schema, phase, status and reason no longer names the value.

## Gates (green on this commit)
- `ruff check .` pass. `ruff format --check` on touched files pass. Both use the pinned 0.15.22.
- `mypy src/decoy_engine/phase.py` (pinned 2.3.0): no issues.
- `tests/unit/phase`: 159 passed.
- `tests/sentry`: 2895 passed, 1 skipped. This covers module size (`phase.py` 479 lines, under 600,
  no census entry), the public-import boundary and the new containment sentry.
- Proof manifest (`tests/sentry/test_proof_manifest.py`): 8 passed.

`error_result` recording `type(exc).__name__` stays deferred to F31. It is now inside the boundary.

## Superseded by the V1 close-out
The owner narrowed the threat model on 2026-10-30 (decoy-platform `handoffs/queue`,
`docs/handoffs/OWNER-DECISIONS-2026-10-30.md`, "Threat model for V1"). `_RejectedError` and its
pass-through are gone; see `2026-10-30-storm-engine-v1-closeout.md`.

## Deferred to V2
Hostile in-process hardening is out of V1 scope. These cases are recorded, not gated, in
`tests/unit/phase/test_v2_hostile_deferred.py` (`xfail(strict=False)`):
- raising `__repr__`, `__len__`, `__iter__`, `__eq__`, `__hash__`, `__lt__`, `__getattr__` on
  `object`/`int`/`str`/`float`/`tuple`/`list`/`dict` subclasses, a raising `__getattribute__`, and a
  foreign `PhaseContractError` raised from a dunder, through the full r7 field walk;
- a `__class__` property spoof;
- a `FrozenMap` forged with `tuple.__new__`;
- a `BaseException` subclass raised from a dunder (V1 lets `BaseException` through on purpose);
- a raising `__del__` reaching `sys.unraisablehook`;
- the r7 mutation case for `PhaseCoverage.__post_init__`, which only the hostile corpus detects.
