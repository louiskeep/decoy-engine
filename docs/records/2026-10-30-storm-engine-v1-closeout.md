# STORM engine: phase envelope containment closed out under the V1 threat model

Base `7f60f21f`. Engine only. Threat model: decoy-platform `handoffs/queue`,
`docs/handoffs/OWNER-DECISIONS-2026-10-30.md`, "Threat model for V1". All runs below use
`uv run --frozen` with `uv sync --frozen --extra dev --extra cloud --extra lint` (Python 3.11.17,
ruff 0.15.22, mypy 2.3.0 from the `lint` extra).

## Changes
1. One boundary (`phase._contained`). It catches `Exception`, keeps only a text from the closed
   `_FIXED_TEXTS` set (any other text becomes "malformed phase evidence"), drops `args`/`kwargs`
   from its frame and raises a fresh `PhaseContractError` outside the handler. `_RejectedError` is
   deleted. `KeyboardInterrupt` and `SystemExit` pass through. `_reason_wire` and `_encode` also
   raise outside their handlers.
2. `error_result` no longer reads `type(exc).__name__`. It records a kind from an exact-type
   table of builtin exceptions, else `"Exception"`.
3. `freeze` matches `list`, `tuple` and `FrozenMap` by exact type; a subclass gets
   "cannot freeze this type into phase evidence". No caller of `freeze` or `error_result` exists
   outside `phase.py` in decoy-engine `src/`, decoy-platform or decoy (grep), so no Platform caller
   passes a subclass.
4. Tests. `tests/unit/phase/test_v1_canaries.py` (new), `tests/sentry/test_phase_containment.py`
   (replaced: enumerates `phase.__all__` and checks the `__phase_contained__` mark), hostile corpus
   moved to `tests/unit/phase/test_v2_hostile_deferred.py` (`xfail(strict=False)`).
   `phase.__all__` is new.

## Red first (new tests against `7f60f21` `phase.py`)
30 failed, 43 passed. Red for the intended reason:
- `test_canary_traceback_locals_in_phase_frames_carry_no_marker`: 15 of 17 (wrapper frame `args`
  held the input); `callee-generator` and `callee-contract-error` were already green (their frames
  held only a generator).
- `test_canary_error_has_no_context_or_cause`: 4 of 17, the callee cases (`ValueError` from a
  generator, `UnknownReasonError` twice, a callee `PhaseContractError`).
- `test_error_result_never_reads_the_exception_class_name_or_text`,
  `test_repr_and_wire_of_every_public_envelope_type_carry_no_marker`: class name in the payload.
- `test_freeze_refuses_container_subclasses_with_fixed_text`: 5 of 5 accepted.
- `test_canary_corpus_is_clean_with_every_boundary_in_place`; the 3 sentry tests (no `__all__`,
  no mark).

Already green on `7f60f21` (characterization, not red first): all 17
`test_canary_error_text_args_and_notes_carry_no_marker` cases (r7 had fixed the text), 13
non-callee context cases, 2 traceback cases, the 7 per-boundary removal tests, and 4 single tests
(walk non-vacuity, builtin error kind, internal text survives, `KeyboardInterrupt`/`SystemExit`).

## Mutations (each applied to the head, then restored)
| Mutation | Result |
|---|---|
| Re-raise inside the `except` block of `_contained` | 35 failed: 17 context, 17 traceback, corpus |
| Keep `args`/`kwargs` in the wrapper frame | 16 failed: 15 traceback, corpus |
| `error_result` reads `type(exc).__name__` | 2 failed |
| `freeze` back to `isinstance(value, (list, tuple))` | 5 failed |
| `_fixed_text` passes any text | 4 failed (`callee-contract-error` on each surface, corpus) |
| Remove `@_contained` from `freeze` | 5 failed, including the sentry |
| `_reason_wire` raises inside its handler `from None` | 0 failed: the outer boundary still drops the chain. Defense in depth only. |

Per-boundary removal (`test_canary_corpus_detects_each_boundary_removed`) is detected for
`FrozenMap.__new__`, the three `__post_init__`s, `freeze`, `to_wire` and `canonical_bytes`.
Removing it from `satisfies_required_gate` or `error_result` is not detected by any ordinary canary:
neither raises for ordinary input. The sentry still requires the mark on both.

## Gates (head)
- `pytest tests/unit/phase tests/sentry -q`: 3125 passed, 1 skipped, 2 xfailed, 5 xpassed
  (base `7f60f21`: 3055 passed, 1 skipped). `tests/sentry` alone: 2898 passed, 1 skipped (one
  sentry case is per doc file, so this record adds one). Phase files: `test_phase_contract.py` 157 passed
  (159 at r7; 2 moved to the deferred file), `test_v1_canaries.py` 70 passed,
  `test_v2_hostile_deferred.py` 2 xfailed (`BaseException` from a dunder, `__del__`), 5 xpassed.
- Module size: `phase.py` 588 lines, under 600, no census entry.
- `ruff check .`: pass. `ruff format --check .`: 1698 files already formatted.
- `mypy src/decoy_engine testflight` (the CI command): 17 errors in 8 files, the same 17 on base;
  none in `phase.py`.

## Wire and signatures
`to_wire` snapshots for six envelopes, base against head: one line differs.
```
< error_result_custom ... "payload": {"error_type": "CustomErr"} ...
> error_result_custom ... "payload": {"error_type": "Exception"} ...
```
Keys, nesting and the other five snapshots are identical, including `error_result` with `OSError`.
`inspect.signature` of `freeze`, `to_wire`, `canonical_bytes`, `satisfies_required_gate`,
`error_result`, `PhaseResult`, `PhaseCoverage`, `PhaseEvent` and `FrozenMap` is unchanged.

## Payload contract (r2)
Accepted field content (payload, identity, check_id, method, method_version, coverage scopes,
sampling_method, event name) is caller-owned evidence. The envelope passes it through `repr`
and `to_wire` unchanged, as the frozen `phase-result/v1` and `phase-event/v1` wire requires; it
cannot tell a source value from an allowed one. Keeping source values out of that content is the
producer's job (F31 value-free event families). The envelope guarantees that its errors, `args`,
notes, `__context__`/`__cause__` chains, `phase.py` traceback locals and `error_result` output
(its `repr` and `to_wire`) carry no input value. The canary tests cover those error paths, not
accepted content. `test_accepted_field_content_round_trips_unchanged` (11 fields) and
`test_accepted_payload_is_exported_exactly_as_given` pin the pass-through so it is not later
"fixed" by scrubbing.

## Limits
- Traceback locals are walked only for frames in `phase.py`. The dataclass-generated `__init__`
  frame and the caller's own frames hold the caller's arguments. A logger with
  `capture_locals=True` will show them; the engine cannot remove those frames.
- Not run: the full regression suite outside `tests/unit/phase` and `tests/sentry`, and Python
  3.10 (CI's mypy version) and 3.12.
- Still out of scope: other `type(exc).__name__` sites in the engine (`quality/synth_report.py:437`,
  `generators/_formula.py:180,243`).

## r2 (Codex gate on `dc69c5f`: 1 HIGH, 2 LOW)
Same environment as above (`uv run --frozen`, ruff 0.15.22, mypy 2.3.0 from the `lint` extra).
- HIGH (class 8 overclaim, class 1 scope): the payload contract is now stated in the `phase.py`
  docstring, the canary docstring and "Payload contract (r2)" above. Fixed as a class: every
  accepted caller field is covered, not only `payload`. No change to scrubbing, rejection or wire.
- LOW (docstring): the `phase.py` docstring now names the exact-type families (`freeze`: scalars,
  `list`, `tuple`, `FrozenMap`) and the ones that still accept subclasses (`Mapping`, `set`,
  `frozenset` in `freeze`; `list`/`tuple` and, for identity, `Mapping` in identity, reasons and
  coverage scopes). It matches every `isinstance` site (`phase.py:254,256,274,300,327,333`).
- LOW (sentry): every name in `phase.__all__` is now either checked (function or non-exception
  class, must carry the mark) or listed in `EXEMPT` with a reason (`PhaseContractError`, the
  `Phase`/`Status` Literal aliases, the two type aliases, seven constants). A name in neither
  fails. Functions and non-exception classes are checked even if listed exempt.

Red first / characterization:
- `test_sentry_flags_an_unclassified_export`: red against the `dc69c5f` sentry logic
  (`assert [] == ['Mystery is neither checked nor exempt']`), green after.
- `test_accepted_field_content_round_trips_unchanged` (11) and
  `test_accepted_payload_is_exported_exactly_as_given`: characterization, 12 passed on `dc69c5f`
  `phase.py`. They pin existing behaviour.
- `test_exempt_list_names_only_real_exports`, `test_exempt_listing_cannot_hide_an_entry_point`:
  written with the new logic, not run red first.

Mutations (each applied, then restored):
| Mutation | Result |
|---|---|
| Sentry skips an unclassified name | 1 failed (`test_sentry_flags_an_unclassified_export`) |
| Sentry consults `EXEMPT` before checking entry points | 1 failed (`test_exempt_listing_cannot_hide_an_entry_point`) |
| `to_wire` drops the payload | 7 failed, including both contract tests |
| `to_wire` hashes `check_id` | 1 failed (`test_accepted_field_content_round_trips_unchanged[check_id]`) |

Gates (head):
- `pytest tests/unit/phase tests/sentry -q`: 3140 passed, 1 skipped, 2 xfailed, 5 xpassed.
  `tests/sentry`: 2901 passed, 1 skipped. `test_v1_canaries.py`: 82 passed.
  `test_phase_containment.py`: 6 passed. `test_module_size.py`: 586 passed.
- `phase.py`: 595 lines (docstring only; under 600, no census entry).
- `ruff check .`: pass. `ruff format --check .`: 1698 files already formatted.
- `mypy src/decoy_engine testflight`: 17 errors in 8 files, unchanged.
- `to_wire` snapshots and public signatures: identical to `dc69c5f`.
- Not run: anything outside `tests/unit/phase` and `tests/sentry`; Python 3.10 and 3.12.
