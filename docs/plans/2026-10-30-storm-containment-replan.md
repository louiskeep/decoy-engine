Status: plan only. No implementation, PR or merge is approved by this document.

Baseline: `decoy-engine` `claude/eager-clarke-14deh1` at `7f60f21f` (read-only). Planning branch: `plan/storm-containment-replan`.

# STORM phase envelope: containment by construction (replan after r5, r6, r7)

Rules consulted: `docs/handoffs/DEFECT-CATALOG.md` and `docs/handoffs/HOSTILE-PLAN-REVIEW.md` (decoy-platform `handoffs/queue` at `2b92dd2`), engine `CLAUDE.md` (established methodology, 600/700 LOC module sizing, pre-GA hard delete, comments explain why), and the F31 plan `docs/plans/2026-10-30-f31-shared-safe-output-boundary.md` (engine `plan/f31-safe-output` at `a909118`).

Risk: R2. Security boundary on a public engine API with one live consumer (Platform). Pre-GA, so mechanisms are hard-deleted, but the `phase-result/v1` and `phase-event/v1` wire shapes and the two Platform call signatures are frozen.

## 1. Where we are

### Verdict on the current architecture

Wrapping every public entry point in `_contained` (`src/decoy_engine/phase.py:78-91`) is the wrong architecture. It tries to make hostile code safe after it has run, and every round has found another place it runs or another channel its exception rides out on. It cannot converge for four structural reasons, each confirmed on `7f60f21f`:

1. **Exceptions are value-bearing objects.** The replacement is raised inside the active `except` (`phase.py:87-89`), so `__context__` keeps the hostile exception (`from None` only sets `__suppress_context__`), and the wrapper frame keeps `args`/`kwargs` as locals. Probe: `freeze(H())` gives `__context__ is not None`, and the marker appears in `TracebackException(..., capture_locals=True)`. The same pattern recurs at `phase.py:376-379` and `phase.py:384-387`.
2. **Pass-through needs an authority, and a class is not one.** `phase.py:85-86` re-raises any `_RejectedError` instance unchanged. Any caller can import and raise it. Probe: a hostile `__iter__` raising `_RejectedError(marker)` comes out with that exact type and the marker in its text.
3. **The entry-point set is open.** Python surfaces include dataclass-generated `__eq__`/`__hash__`/`__repr__`, attributes assigned after class definition, aliases, partials and callable objects. The source-scanning sentry (`tests/sentry/test_phase_containment.py:23-37`) sees none of these. Probe: on a forged envelope, `r == r2` and `hash(r)` raise a raw `RuntimeError` carrying the marker, and `repr(r)` returns it. A method assigned to `PhaseEvent` after import is not flagged.
4. **Validation runs foreign code.** `isinstance(value, Mapping)` (`phase.py:235`, `:162`, `:182`) consults a hostile `__class__` property. `tuple(pairs)` (`phase.py:106`) iterates a hostile iterable. `isinstance(value, (list, tuple))` (`phase.py:244`) admits subclasses. Probe: a `__class__` property raising `RuntimeError(marker)` runs during `freeze`. A `tuple` subclass and a `FrozenMap` subclass are both accepted (the FrozenMap subclass is silently thawed to a list of pairs). That contradicts r7 record lines 41-44 and 72-73.

**Root cause class.** Catalog class 6 (untrusted producer) feeding class 1 (value-bearing output). Exception metadata and frames are the output surface. The defect is that the module calls into objects it has not type-checked, then tries to clean up the exception. Patching more entry points is the third-time patch the catalog forbids (section 5).

**Two new findings beyond the r7 gate**, same class:
- `error_result` writes `type(exc).__name__` into the payload (`phase.py:478`). A class whose `__name__` is the marker reaches `to_wire` output (probe confirmed). Catalog class 1 says "never read `__name__`".
- `_contained` catches `Exception` only (`phase.py:87`). A hostile dunder raising a `BaseException` subclass escapes raw with the marker (probe confirmed). The r7 catch-all (`tests/unit/phase/test_phase_contract.py:730-737`) also catches only `Exception`, so it cannot see this.

Gate findings re-verified on `7f60f21f` with probes in the `uv.lock` venv (Python 3.11.17): both HIGHs, the sentry MEDIUM (post-definition method), the `freeze` subclass MEDIUM, and the LOW (`test_phase_contract.py:687-690` silently skips the set variant for unhashables). Existing suites: `tests/unit/phase` and `tests/sentry/test_phase_containment.py`, 161 passed.

### Done and worth keeping

- The wire contract and registry: `phase-result/v1`, `phase-event/v1`, `decoy-reasons/v1` (`99326fe`).
- Construction-time coercion and deep freeze. Validation is repeated at export because `object.__setattr__` can forge a frozen dataclass (`da06ed8`, `86cd474`, `c97e69f`).
- Bounds: signed 64-bit ints, depth 64, finite floats, set ordering by (type rank, value), `default_factory` coverage, and the gate running the real encoder (`9b55653`).
- Exact-type scalar check `_is_scalar` (`phase.py:131-143`) and fixed-text header and reason errors (`7f60f21`).
- The r7 field walk over `dataclasses.fields` (`test_phase_contract.py:771-776`) and every pre-r7 behavioural test.

### Wrong, to be replaced

- `_contained`, `_RejectedError` and every `raise ... from None` inside an `except` (`phase.py:74-91`, `:376-379`, `:384-387`).
- `isinstance` and ABC dispatch on inputs (`phase.py:162`, `:164`, `:182`, `:208`, `:235`, `:241`, `:244`).
- Dataclass-generated `__init__`/`__eq__`/`__hash__`/`__repr__` on envelopes. The generated `__init__` frame holds every raw argument as a local while `__post_init__` raises.
- `type(exc).__name__` in `error_result`.
- The code-object sentry (`tests/sentry/test_phase_containment.py:15-37`) and the leak check that reads only `str`/`args` (`test_phase_contract.py:730-737`).
- r7 record claims at lines 41-44, 65 and 72-73. They are corrected in Slice 0.

### Keep vs revert (commits)

Revert nothing with `git revert`. Every commit from `99326fe` to `7f60f21f` carries semantics we keep, and the replaced mechanisms are confined to `phase.py` and one sentry file. Build forward on `7f60f21f`:

| Commit | Decision |
|---|---|
| `99326fe` envelope + registry | Keep (frozen contract) |
| `da06ed8`, `86cd474`, `c97e69f` | Keep semantics; the code moves into the core (Slice 2) |
| `9b55653` | Keep bounds and gate encoding; its `_contained` is superseded |
| `7f60f21f` | Keep `_is_scalar`, fixed texts and the r7 tests; hard-delete `_contained`, `_RejectedError` and the code-object sentry |

## 2. New design in one paragraph

A pure core never calls a method of any object that is not an exact builtin or an exact phase type. It dispatches on `type(x) is ...` before any `len`, iteration, hash, compare or attribute read, so no hostile dunder can run and no foreign exception can start inside the module. The core reports a rejection only as a member of a closed `Code` enum, carried by a module-private `_Halt`. One helper, `_attempt`, runs the core, keeps only an identity-checked `Code` (any other exception, forged or real, becomes `Code.INTERNAL`) and returns after its `except` block has ended. Each public callable then deletes its input locals and calls `_deliver`, which raises a fresh `PhaseContractError(code)` outside any `except`, with fixed text and no cause, notes, chain or input-bearing frame. A live-surface sentry enumerates `vars(phase)` and every class `__dict__` at test time, and an AST sentry allows exactly one `try` and two `raise` shapes. Together these make the closure checkable rather than argued.

Why not return a result object everywhere: Platform calls `to_wire(error_result(...))` and uses the returned `dict` and `PhaseResult` (Section 6). Raising stays at the public edge. The result object exists inside (`_attempt` returns `(value, code)`), and `satisfies_required_gate` already returns `bool`.

Threat model: hostile objects passed as arguments, or forged into envelope or `ReasonRef` fields with `object.__setattr__`. Out of scope, as in the F31 plan (F31 line 316): code that uses `gc`, `sys._getframe`, `ctypes`, or that monkeypatches `phase` or its classes at runtime. The source-level "assigned after definition" case is in scope (sentry S1).

## 3. Invariants as properties

- **P1 No foreign execution.** For every public callable `f` and every argument or forged field `h` in the hostile corpus, `f` returns or raises without invoking any Python-level method defined outside `builtins` and `decoy_engine.phase`. Measured by a per-object call counter that stays at zero.
- **P2 Closed error.** Every exception leaving `decoy_engine.phase` is exactly `PhaseContractError` (or a fresh `KeyboardInterrupt()`, see O3), with `args == (TEXT[code],)`, `vars(e) == {"code": code}`, `code in Code`, `e.__cause__ is None`, no `__notes__`, and `e.__context__` identical to the exception the caller was already handling at call entry (`None` outside an `except`).
- **P3 No retained input.** No frame in `e.__traceback__` whose `f_code.co_filename == phase.__file__` holds, in `f_locals`, an object identical to or containing (bounded `gc.get_referents` walk) any hostile input. `TracebackException(e, capture_locals=True).format()` contains no marker.
- **P4 Closed output.** Every successful output (`to_wire` dict, `canonical_bytes`, `freeze` value, `repr`, `error_type` label) is built only from exact builtins, fixed module text and validated caller strings of the contract. `error_type` is a member of the closed `ERROR_TYPES` label set.
- **P5 Live surface closed.** Every callable reachable from a public name of `phase` or a public class `__dict__`, evaluated at test time, is either an edge (S1 registry) or an approved foreign object, matched by identity. Nothing else is callable.
- **P6 Contract unchanged.** For valid inputs, `to_wire` and `canonical_bytes` outputs are byte-identical to `7f60f21f` across the existing corpus, including Platform's `{"error_type": "TypeError"}`.

## 4. Class table

| # | Class | Applies | Closing structure | Sentry or property test | Mutation that must turn it red |
|---|---|---|---|---|---|
| 1 | Value-bearing output via exception text, context, notes, frames or class names | Yes | `_deliver` raises a fresh closed-code error after `del` of inputs; `ERROR_TYPES` identity table replaces `__name__` | T2 chain walk, T3 frame-locals walk, T9 label test | Raise inside `_attempt`'s `except`; drop `del value`; restore `type(exc).__name__` |
| 2 | Cleanup on an exit path: references to inputs left in frames after a rejection | Yes | Every public body has the shape `out, code = _attempt(core, ...)`, then `del` inputs, then `_deliver(out, code)`; AST rule S2c | T3 over every public callable, success and failure exit | Remove one `del` in one constructor |
| 3 | State and concurrency: input mutated by another thread mid-walk | Yes, narrow | Core snapshots exact containers (`tuple(exact_list)`, `tuple(exact_dict.items())`) before walking; any interpreter error becomes `Code.INTERNAL` | T15 barrier-forced mutation test | Iterate the live dict instead of the snapshot |
| 4 | Route parity: every public route uses the same edge | Yes | One `_attempt`/`_deliver` pair; S1 live-surface registry | T6 behavioural fuzz over every enumerated callable | Add a public alias to a core function |
| 5 | Referential integrity | N/A: the envelope carries no FK or row relations; it is evidence about a phase, not table data | N/A: no relations exist to preserve | N/A: nothing to test | N/A: no FK code exists |
| 6 | Untrusted producer: hostile dunders, subclasses, forged fields, forged `_Halt` or errors | Yes | Exact-type dispatch before any operation; closed `Code` rebuilt by identity; non-subclassable `FrozenMap` and envelopes via `__init_subclass__` | T1 call-counter property, T4 forged instances, T8 exact types | Replace one `type(x) is dict` with `isinstance(x, Mapping)` |
| 7 | TOCTOU between check and use of an input | Yes, narrow | Validate the snapshot that is then stored; never re-read the caller's container | T15 (same test as class 3), plus T1 counting `__iter__` calls | Validate the caller's list, then store `tuple(caller_list)` |
| 8 | Weak tests and overclaimed evidence | Yes (r6 and r7 records overclaimed) | Every corpus test asserts its own size and variant counts; red-first runs recorded per test id | T10 variant-count non-vacuity; Slice 0 record correction | Silently drop the set variant for unhashables |
| 9 | Behaviour-preserving refactor drift | Yes | Characterization snapshot of `canonical_bytes` for the full existing corpus taken before Slice 2 | T12 snapshot equality | Change key order or int formatting in `_wire` |
| 10 | Frozen contract drift (`phase-result/v1`, `phase-event/v1`, Platform signatures) | Yes | Wire key-set snapshot and Platform call-shape test; additive `PhaseContractError.code` only | T12, T13 | Rename `error_type` or change the `error_result` signature |

## 5. Enumerations from the code

### Exit paths (every way control leaves a public callable at `7f60f21f`)

- `_contained` wrapper re-raises a `_RejectedError` instance unchanged: `src/decoy_engine/phase.py:85-86`.
- `_contained` converts `Exception` inside the active `except`, keeping `__context__`: `src/decoy_engine/phase.py:87-89`.
- `BaseException` subclasses pass through untouched: `src/decoy_engine/phase.py:87`.
- `_reason_wire` raises inside `except UnknownReasonError`: `src/decoy_engine/phase.py:376-379` (the source text is built from input at `src/decoy_engine/reasons.py:102`).
- `_encode` raises inside `except Exception`: `src/decoy_engine/phase.py:384-387`.
- `satisfies_required_gate` returns `False` from inside `except PhaseContractError`: `src/decoy_engine/phase.py:451-454`.
- Normal returns: `phase.py:118`, `:259`, `:400`, `:443`, `:455`, `:471`.
- Interpreter argument-binding `TypeError` at the call site, before any phase frame runs (O4).

### Routes (execution routes into the module)

- Public callables at `7f60f21f`: `FrozenMap.__new__` (`phase.py:104-118`), `PhaseCoverage.__post_init__` (`phase.py:283-287`), `PhaseResult.__post_init__` (`phase.py:320-329`), `PhaseEvent.__post_init__` (`phase.py:353-355`), `freeze` (`phase.py:249-259`), `to_wire` (`phase.py:390-400`), `canonical_bytes` (`phase.py:441-443`), `satisfies_required_gate` (`phase.py:446-455`), `error_result` (`phase.py:458-479`).
- Generated and unguarded: dataclass `__init__`, `__eq__`, `__hash__`, `__repr__`, `__setattr__`, `__delattr__` on the three envelopes (`phase.py:273`, `:307`, `:345`). Tuple-inherited `FrozenMap` methods (`__eq__`, `__hash__`, `__repr__`, `__iter__`, `count`, `index`).
- Platform consumer: `decoy-platform` `claude/eager-clarke-14deh1` at `a502b00`, `api/post_scan/integrity_hook.py:279-293` (`to_wire(error_result(...))`, called inside `except TypeError`) and `api/pre_scan/router.py:949-960`.
- CLI consumer: none. `git grep` on `decoy` `claude/eager-clarke-14deh1` (`d697c6d`) and `claude/storm-split-cli` finds no `decoy_engine.phase` import.
- Engine internal consumers: none. `grep` of `src/decoy_engine` outside `phase.py` finds no import.

### FK shapes

N/A: the phase envelope holds no table rows or keys, so no FK shape exists.

### Fields (every field of every public type)

- `PhaseCoverage` (`phase.py:274-281`): `requested_scope`, `observed_scope`, `rows_examined`, `exact`, `sampling_method`, `limits`, `omissions`.
- `PhaseResult` (`phase.py:308-318`): `phase`, `check_id`, `status`, `method`, `method_version`, `coverage`, `reasons`, `identity`, `payload`, `schema_version`.
- `PhaseEvent` (`phase.py:346-351`): `phase`, `event`, `status`, `reason`, `schema_version`.
- `ReasonRef` (`src/decoy_engine/reasons.py:43-48`): `namespace`, `code`. It is forgeable, and it is reached by `_require_reason`.
- `FrozenMap` (`phase.py:94-118`): its tuple items.
- `PhaseContractError` (`phase.py:70-71`): today `args` and the inherited exception metadata. New: `code`.

### Output surfaces

- Exception `str`, `args`, `__dict__`, `__context__`, `__cause__`, `__suppress_context__`, `__notes__`, and `__traceback__` frame locals.
- `to_wire` dict and `canonical_bytes` bytes (`phase.py:390-443`).
- `repr`, `==` and `hash` on envelopes and `FrozenMap` (generated, see Routes).
- `satisfies_required_gate` bool (`phase.py:446-455`).
- `error_result` payload `error_type` (`phase.py:478`), persisted by Platform (`integrity_hook.py:285-306`).
- Logs, stdout and stderr: none. `phase.py` has no logging or print call.

## 6. Frozen contracts touched

- `phase-result/v1` and `phase-event/v1` wire key sets and value types: unchanged (T12).
- `error_result(phase, check_id, method, method_version, reason, exc) -> PhaseResult` and `to_wire(item) -> dict`: unchanged signatures (T13). The Platform test at `decoy-platform` `tests/test_post_scan_integrity_hook.py:322-331` must keep passing: `payload == {"error_type": "TypeError"}`.
- `error_type` value domain narrows to the closed `ERROR_TYPES` labels. An unlisted class becomes `"Exception"`. This is breaking only for foreign or extension class names, which are exactly the leak.
- `PhaseContractError.code` attribute: additive.
- Behaviour changes, pre-GA: `freeze` accepts only exact `dict`, `list`, `tuple`, `set`, `frozenset` and `FrozenMap` (no `Mapping` ABC, no subclasses); `FrozenMap` and envelope classes cannot be subclassed; envelope `==` and `hash` are over `canonical_bytes`, and a forged envelope compares unequal and hashes to a `PhaseContractError`.
- Not touched: `decoy-reasons/v1` (`reasons.py`). `get_reason` text at `reasons.py:102` stays; phase never surfaces it.

## 7. F31 assumptions

F31 exists only as a plan (`plan/f31-safe-output` `a909118`). Its baseline has no `phase.py` (F31 plan line 126). This plan assumes nothing from F31 is landed and adds no prerequisite on it. It relies only on these F31 promises, for later convergence:
- F31 will migrate `phase.py` onto explicit phase event families (F31 line 380). The free-form `str` and payload fields are F31's problem, not this plan's.
- F31's `SafeErrorType` is an exact-type closed table, and `__name__` is never read (F31 lines 288, 327). Our `ERROR_TYPES` uses the same rule and the builtin names as labels, so F31 can map one to one.
- F31's propagation rule is a fresh error raised outside `except`, with cause and context cleared (F31 line 335). P2 is that rule.

## 8. Reuse inventory

- `_is_scalar`, `_check_int`, `_check_depth`, `_SET_RANK` (`phase.py:121-143`, `:216-226`): reused unchanged in the core. Their `raise _RejectedError` becomes `raise _Halt(Code.X)`.
- `require_known` / `REASONS` (`reasons.py:98-106`): the core looks up `REASONS[(ns, code)]` with `.get`, after an exact-str check, instead of catching `UnknownReasonError`.
- r7 field walk and hostile type factory (`test_phase_contract.py:616-776`): extended, not replaced. `_exercise` gains the T2 and T3 checks.
- Module-size sentry (`tests/sentry/test_module_size.py`) and public-import boundary (`tests/sentry/test_public_import_boundary.py`): unchanged. If `phase.py` would pass 600 lines, split the core into `decoy_engine/_phase_core.py`; S2 and P3 then cover both files.
- New machinery: `Code` enum, `_Halt`, `_attempt`, `_deliver`, `ERROR_TYPES`, and sentries S1 and S2. Each replaces a deleted mechanism; none duplicates an existing helper (the F31 adapter `reason_adapter.py` does not exist at this baseline).

## 9. Hostile corpus and acceptance tests, red first

All tests are written in Slice 1 and run against `7f60f21f` before any `phase.py` change. The red or green result is recorded per test id. A test that is green on `7f60f21f` is labelled "regression guard", not red-first.

Hostile corpus (`tests/unit/phase/_hostile.py`): the r7 set (`test_phase_contract.py:616-680`), plus:
- every dunder raising a `BaseException` subclass;
- a `__class__` property;
- a metaclass with hostile `__hash__`, `__eq__` and `__name__`;
- an exception class whose `__name__` is the marker;
- forged `_Halt` instances, forged `PhaseContractError` instances with marker `args`, pre-attached `__notes__`, a marker `__cause__` and a non-`Code` `code`, and an instance from `_Halt.__new__(_Halt)` with no args;
- `tuple`, `list`, `dict`, `set` and `FrozenMap` subclasses, and `MappingProxyType`;
- a `__del__` that raises (recorded as residual, see Pre-mortem).

Every hostile object counts its own dunder calls in a shared counter. Variants: bare, tuple, list, dict value, dict key where hashable, pair, set where hashable, and forged into each field. The unhashable list is explicit.

| Id | Test | Property | Expected on `7f60f21f` |
|---|---|---|---|
| T1 | `test_no_foreign_method_runs` | P1, counter stays 0 for every callable and variant | RED (`isinstance` reads `__class__`; `tuple(pairs)` iterates) |
| T2 | `test_exception_chain_walk` (`__context__`, `__cause__`, `__notes__`, `args`, `vars(e)`) | P2 | RED (`__context__` set) |
| T3 | `test_traceback_frame_locals_walk` (identity plus `capture_locals` marker scan, phase frames only) | P3 | RED (wrapper locals hold `args`) |
| T4 | `test_forged_instances_rebuild_closed_code` | P2 for forged `_Halt`, `_RejectedError`, `PhaseContractError` | RED (forged `_RejectedError` passes through) |
| T5 | `tests/sentry/test_phase_surface.py::test_live_surface_is_closed` plus seeded fake modules: post-definition method, class attribute alias, `functools.partial`, callable object, `staticmethod` alias, alias with a foreign `__module__` | P5, each seed flagged | RED on the old sentry for each seed |
| T6 | `test_every_enumerated_callable_fuzzed` (signature-driven, uses the S1 list) | P1 to P3 | RED |
| T7 | `test_generated_dunders_on_forged_envelopes` (`==`, `hash`, `repr`) | P2, P4 | RED (raw `RuntimeError`, repr marker) |
| T8 | `test_freeze_exact_types_only` and `test_phase_types_not_subclassable` | P1, P4 | RED (tuple and FrozenMap subclasses accepted) |
| T9 | `test_error_type_is_closed_label` (marker `__name__`, hostile metaclass, builtin names) | P4 | RED (marker in wire) |
| T10 | `test_corpus_is_not_vacuous` (exact variant counts; set variant built for every hashable hostile; unhashables listed) | non-vacuity | RED (the set variant is silently skipped) |
| T11 | `tests/sentry/test_phase_surface.py::test_ast_shape` S2 plus seeded mutants | S2 | RED (several `try` blocks; raises in `except`) |
| T12 | `test_wire_snapshot_unchanged` (key sets plus `canonical_bytes` snapshot of the existing corpus, generated at `7f60f21f`) | P6 | GREEN, regression guard |
| T13 | `test_platform_call_shape` (`to_wire(error_result(..., TypeError(secret)))` inside an `except`, equal to the Platform expectation) | P6, P2 | GREEN for shape, regression guard |
| T14 | `test_context_is_callers_own` (call inside an `except`; `e.__context__ is` the caller's exception) | P2 | RED (context is the hostile exception) |
| T15 | `test_concurrent_mutation_is_contained` (barrier: second thread mutates the input list and dict mid-walk) | P2, class 3 and 7 | Recorded; expected RED or flaky-raw |
| T16 | `test_keyboard_interrupt_is_fresh` (a hostile `__iter__` cannot raise it, by P1; a signal injected through `_attempt`'s test hook gives a fresh `KeyboardInterrupt()` with no context) | O3 | Recorded |

Sentries (`tests/sentry/test_phase_surface.py`, replacing `tests/sentry/test_phase_containment.py`):
- **S1 live surface.** After import, enumerate every non-underscore name in `vars(phase)` and, for each phase class, every entry in `cls.__dict__`, dunders included. Unwrap `staticmethod`, `classmethod` and `property`. Each callable must be (a) a function whose `__code__` is in `phase._EDGES` (a frozenset of edge code objects), (b) an entry in the approved-foreign identity set (for example `json`, `typing.Any`, `ReasonRef`, `DecoyError`), or (c) a phase class. Anything else fails, including partials, callable instances and functions from other modules. `__all__` must equal the public names in (a) and (c).
- **S2 AST shape** over `phase.py` (and `_phase_core.py` if split):
  - (a) exactly one `try` statement, inside `_attempt`;
  - (b) every `raise` is `raise _Halt(Code.NAME)` or the single `raise` in `_deliver`, and no `raise ... from`;
  - (c) every function in `_EDGES` has the body shape `_attempt`, then `del`, then `_deliver` (or `return` for the bool gate);
  - (d) no `isinstance`, `issubclass`, `__name__`, `__qualname__`, `getattr` with a non-literal name, `str()`/`repr()`/`format()` on a non-literal, or f-string with an expression;
  - (e) a core function (transitively, any function that raises `_Halt` or calls one) is referenced outside core only as `_attempt`'s first argument.

## 10. Design detail

- `Code`: a closed `enum.Enum`. Members: `MALFORMED`, `UNFROZEN`, `INT_RANGE`, `NON_FINITE`, `DEPTH`, `UNKNOWN_SCHEMA`, `UNKNOWN_PHASE`, `UNKNOWN_STATUS`, `UNKNOWN_REASON`, `NOT_ENVELOPE`, `NO_ENCODING`, `INTERNAL`. `TEXT` is a `MappingProxyType` from `Code` to fixed text.
- `_Halt(Exception)`: `__init__(self, code)` stores `args = (code,)`. `_attempt` reads only `type(e) is _Halt`, then `e.args` (always a C-level tuple), with `len == 1` and identity membership in `_CODE_MEMBERS`. Otherwise `Code.INTERNAL`. It catches `BaseException`. An exact `KeyboardInterrupt` sets a flag, and a fresh `KeyboardInterrupt()` is raised after the block (O3). Nothing else is read from a caught object.
- `_deliver(out, code)`: returns `out` if `code is None`. Otherwise it builds `PhaseContractError(TEXT[code])`, sets `code`, and raises. It is called outside any `except`.
- Envelopes: `@dataclass(frozen=True, init=False, eq=False, repr=False)` keeps `dataclasses.fields`, which the r7 field walk and T12 rely on. Explicit `__init__` with the same parameters and defaults: `_attempt(_build_*, ...)`, then `del` every parameter, then `_deliver`. Only then are validated values assigned with `object.__setattr__`, so `self` holds no input at raise time. Explicit `__eq__`, `__hash__` and `__repr__` go through `_attempt(_wire_core, self)`. `__init_subclass__` raises `TypeError` with fixed text.
- `FrozenMap.__new__(cls, pairs=())`: refuses `cls is not FrozenMap`. It accepts only an exact `tuple`, `list` or generator from the module's own `_freeze` (internal path), and snapshots before validating.
- `freeze`: exact `dict` (exact-str keys, snapshot `tuple(d.items())`), exact `list`/`tuple`, exact `set`/`frozenset` of exact scalars, exact `FrozenMap`, and scalars.
- `error_result`: `ERROR_TYPES` is a tuple of `(cls, label)` pairs over reviewed builtins and `decoy_engine.errors` exact classes. Lookup is a linear `type(exc) is cls`, so no hash or eq runs on a hostile metaclass. The default is `"Exception"`.
- `satisfies_required_gate`: `_attempt(_gate_core, item)`. It returns `code is None and status == "passed"` and never raises.

## 11. Pre-mortem: how a gate reviewer breaks this when it is built

1. **Interpreter argument binding.** `PhaseResult(**{marker: 1})` raises a `TypeError` naming the keyword before any phase frame runs. That is caller-authored code, not data. T6 asserts that no phase frame is in its traceback. The text residual is O4.
2. **Finalizers.** A hostile `__del__` runs on GC during a walk. Its exception is unraisable and goes to `sys.unraisablehook` with a repr. That is ambient output and belongs to F31 (F31 line 351). Recorded in T1 as a known residual, never silently skipped.
3. **f_locals semantics differ by version.** CI uses 3.10 (`.github/workflows/ci.yml:37`). `requires-python` is `>=3.10,<3.13` (`pyproject.toml:13`). P3 must run on 3.10, 3.11 and 3.12. Run T3 on each.
4. **`self` repr while half-built.** `capture_locals` calls `repr(self)` on the unassigned envelope. The explicit `__repr__` goes through `_attempt` and returns fixed text. T3 covers it.
5. **Caller's own context.** The Platform calls inside `except TypeError`. P2 allows only the caller's own exception as context, and T14 checks identity.
6. **Approved-foreign set grows.** A reviewer adds `dumps = json.dumps`. S1 matches by identity, `json.dumps` is not in the set, so it fails. The set is reviewed like a census.
7. **AST rule evaded by indirection** (`h = _Halt; raise h(...)`). S2(b) requires the literal call form. A name-binding of `_Halt` outside its definition is flagged by S2(e).
8. **Snapshot cost.** `tuple(d.items())` doubles memory for big payloads. Payloads are evidence, not data. Depth 64 and the int bounds already apply. Accepted.
9. **Module size.** It may cross 600. Split per Section 8; no census entry is needed if each file stays under 600.
10. **Mechanism tests deleted.** `test_catch_all_detects_containment_removed_from_a_constructor` (`test_phase_contract.py:778-788`) depends on `__wrapped__`. It is replaced by T11 and T6 mutants, which prove the same non-vacuity (removing the edge turns tests red). This needs owner sign-off (O5). It is not a weakened assertion.

Self-attack on this plan's arguments (mutation, then the test that catches it): exact dispatch replaced by `isinstance` (T1); raise moved into `except` (T2, T14); a `del` removed (T3); a forged code passed through (T4); a post-definition alias added (T5); `eq=True` restored (T7); `__name__` read (T9); the set variant dropped (T10); a second `try` added (T11); wire key order changed (T12).

## 12. Slices

All slices are in `decoy-engine` on one integration branch cut from `7f60f21f`. Each slice is separately gateable. Commands run from `uv.lock`:

```bash
uv sync --frozen --all-extras
```

| Slice | Content | Size | Acceptance |
|---|---|---|---|
| 0 | Correct the r7 record (lines 41-44, 65, 72-73) and the r6 claim. No code change | XS | Review only |
| 1 | Hostile corpus, T1 to T16, sentries S1 and S2 written against `7f60f21f`; red or green recorded per id in `docs/records/` | M | Expected column in Section 9 reproduced |
| 2 | Core rewrite: `Code`, `_Halt`, `_attempt`, `_deliver`, exact dispatch, explicit envelope methods, `ERROR_TYPES`; hard-delete `_contained`, `_RejectedError` and `test_phase_containment.py` | M | T1 to T16, S1, S2 green; every pre-r7 test green unchanged; T12 snapshot identical |
| 3 | Self-attack: apply each Section 11 mutation as a temporary patch; record the red test id; revert | S | One red id per mutation in the record |
| 4 | Platform verification only: run the Platform suite against the engine branch, with no Platform code change | S | `tests/test_post_scan_integrity_hook.py` and pre-scan tests green |

Exact commands:

```bash
uv run --frozen pytest -q tests/unit/phase tests/sentry/test_phase_surface.py
```

```bash
uv run --frozen pytest -q tests/sentry
```

```bash
uv run --frozen ruff check src/decoy_engine/phase.py tests/unit/phase tests/sentry/test_phase_surface.py
```

```bash
uv run --frozen mypy src/decoy_engine/phase.py
```

Run the phase tests on 3.10 and 3.12 too, for P3:

```bash
uv run --frozen --python 3.10 pytest -q tests/unit/phase
```

## 13. Worker evidence rules

- Name the catalog class of every finding, and fix the class structure in this plan, not only the instance.
- Write the Section 9 tests first and record them red against `7f60f21f`. A test that is green before is labelled a regression guard.
- Attack your own diff: apply every Section 11 mutation and show the red test id.
- Run baselines from `uv.lock` in the same environment as the branch. State Python versions run and anything not run.
- Never widen a threshold, skip a test or weaken an assertion. Deleting the two mechanism tests needs O5 approval first.
- If a new escape of class 1 or 6 appears after Slice 2, stop and report it. Do not patch a third time.

## 14. Open questions for the owner

- **O1** Is the narrowing of `freeze` to exact builtin containers (no `Mapping` ABC, no `OrderedDict` or `MappingProxyType`) acceptable? There is no in-repo or Platform caller of `freeze`.
- **O2** Envelope `==`/`hash` over `canonical_bytes`, or identity semantics (`eq=False` with no override)? No caller compares envelopes today.
- **O3** Should `_attempt` re-raise a fresh `KeyboardInterrupt` and turn every other `BaseException` (including a `SystemExit` from an application signal handler) into `Code.INTERNAL`, or let `SystemExit` pass?
- **O4** Is the interpreter's argument-binding `TypeError` (caller-authored keyword names) out of scope, as proposed?
- **O5** Approve replacing `test_catch_all_detects_containment_removed_from_a_constructor` and `test_phase_containment.py` with the T11/T6 mutants and S1.
- **O6** Platform's `pass_failed_with: type(exc).__name__` (`integrity_hook.py:305`) and `_safe_failure_msg` (`api/pre_scan/router.py:944-946`) leak dynamic class names outside this envelope. Leave them to the F31 Platform slice, or file a separate Platform fix?
