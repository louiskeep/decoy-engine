Status: review

# Codex hostile review of the storm-replan (2026-10-30)

Reviewer: Codex (gpt-5.6-sol), read-only, against the plan at its pushed head. This review carries no authority; the orchestrator re-verifies.

Verdict: **NO-GO**: 4 BLOCKER, 4 HIGH, 3 MEDIUM.

Reviewed the plan, contract, baseline `7f60f21f`, Platform `a502b00`, CLI `d697c6d`, and F31 `a909118`. `plan-lint` passes, but substantive closure does not. No files changed.

## BLOCKER

1. **P1 knowingly excludes hostile finalization while claiming universal closure.**  
   The hostile corpus includes `__del__`, but the pre-mortem accepts its output as an F31 residual ([plan:68](<docs/plans/2026-10-30-storm-containment-replan.md:68>), [plan:161](<docs/plans/2026-10-30-storm-containment-replan.md:161>), [plan:211](<docs/plans/2026-10-30-storm-containment-replan.md:211>)). Required `del` operations can synchronously invoke hostile `__del__` or suspended-generator cleanup inside the phase edge. P1’s call counter therefore cannot stay zero. Either narrow P1 explicitly or depend on F31/process containment.

2. **`FrozenMap` remains an open execution surface.**  
   It remains a tuple subclass ([phase.py:94](<src/decoy_engine/phase.py:94>)); the design only replaces `__new__` ([plan:206](<docs/plans/2026-10-30-storm-containment-replan.md:206>)). I reproduced:

   - `count`, `index`, and `h in frozen_map` invoking hostile `__eq__`
   - `tuple.__new__(FrozenMap, (h,))` forging an exact instance
   - hashing that instance invoking hostile `__hash__`

   S1 only scans `cls.__dict__`, not the MRO ([plan:191](<docs/plans/2026-10-30-storm-containment-replan.md:191>)). Replace tuple inheritance or contain and test the complete inherited surface.

3. **S1’s alias proof is false.**  
   S1 accepts any function whose code object belongs to `_EDGES` ([plan:192](<docs/plans/2026-10-30-storm-containment-replan.md:192>)). Thus `PhaseEvent.alias = freeze` and `staticmethod(freeze)` pass, although T5 says class aliases must fail ([plan:178](<docs/plans/2026-10-30-storm-containment-replan.md:178>)). Registry identity must include owner, attribute name, and descriptor kind-not only code identity.

4. **The proposed design cannot pass its own S2.**  
   S2 allows only `_Halt` raises and the single `_deliver` raise ([plan:194](<docs/plans/2026-10-30-storm-containment-replan.md:194>)), while the design requires:

   - raw `TypeError` from `__init_subclass__` ([plan:205](<docs/plans/2026-10-30-storm-containment-replan.md:205>))
   - fresh `KeyboardInterrupt` from `_attempt` ([plan:203](<docs/plans/2026-10-30-storm-containment-replan.md:203>))

   The raw `TypeError` also violates P2 and retains the new subclass in traceback locals.

## HIGH

5. **Grounding enumeration is incomplete.**  
   I spot-checked 52 citation targets. The cited lines and files are generally real; no phantom citation was found. However:

   - Exit paths omit generated constructor/dunder exits, `PhaseContractError`, and `satisfies_required_gate`’s line-450 return.
   - Routes list only six inherited `FrozenMap` operations although its MRO exposes many more, including hostile comparison routes ([plan:92](<docs/plans/2026-10-30-storm-containment-replan.md:92>), [plan:103](<docs/plans/2026-10-30-storm-containment-replan.md:103>)).
   - Transitions and temporary-location producers lack explicit N/A enumerations.
   - The 22 envelope fields and two `ReasonRef` fields are correctly grounded.
   - Platform’s two positional call sites and `{"error_type": "TypeError"}` expectation are real.

6. **Classes 3 and 7 have no deterministic non-vacuity proof.**  
   T15 is only “recorded; expected RED or flaky-raw” ([plan:188](<docs/plans/2026-10-30-storm-containment-replan.md:188>)). No synchronization seam explains how the barrier interrupts C-level list/dict snapshotting, and the mutation is absent from Slice 3. A flaky race does not satisfy the catalog’s barrier-forced requirement.

7. **Frozen behavior and signatures are only partly characterized.**  
   T12 freezes wire bytes, but not valid `repr`, equality, hash, constructor defaults, keyword behavior, or signatures before replacing generated methods. T13 calls the API; it does not compare `inspect.signature`. The plan also explicitly narrows the `error_type` value domain ([plan:137](<docs/plans/2026-10-30-storm-containment-replan.md:137>)), which is a breaking wire-domain change even if Platform’s current builtin case remains unchanged.

8. **Slice ordering is not buildable.**  
   Slice 1 tests `_Halt`, `_EDGES`, the new AST structure, and a test hook before Slice 2 creates them ([plan:172](<docs/plans/2026-10-30-storm-containment-replan.md:172>), [plan:234](<docs/plans/2026-10-30-storm-containment-replan.md:234>)). O1–O5 also leave API semantics, `BaseException` handling, and test deletion unresolved.

## MEDIUM

9. **P3 test scope is underspecified.**  
   `capture_locals=True` can render caller frames that legitimately retain inputs. If `_phase_core.py` is created, P3’s `filename == phase.__file__` condition misses its frames. Generator frames, cycles, caller context, and exception notes need explicit filtering rules.

10. **Module allocation is decided too late.**  
    `phase.py` is currently 479 LOC. The 600/700 policy is correctly cited, but Slice 1 needs to know whether `_phase_core.py` exists. The proposed lint/type commands omit that file if it is created.

11. **Plan bloat is concentrated in brittle syntax policing.**  
    Sixteen tests plus a transitive AST sentry hard-code implementation syntax while still missing inherited routes and valid aliases. The Rules line also omits the required universal, development-loop, testing, security, architecture, and API rulebooks and the R2 established-solutions section.

## Catalog closure

| Class | Result |
|---|---|
| 1 Value-bearing output | Not closed: finalizers and inherited tuple routes |
| 2 Cleanup | Structure plausible only after finalization/P3 scope is fixed |
| 3 State/concurrency | Snapshot plausible; proving test non-deterministic |
| 4 Route parity | Not closed: MRO and edge-alias holes |
| 5 Referential integrity | Valid N/A |
| 6 Untrusted producer | Not closed: `FrozenMap`, raw subclass error |
| 7 TOCTOU | Structure plausible; mutation proof missing |
| 8 Weak evidence | Not closed: T15 and Slice-1 dependency |
| 9 Refactor drift | Wire snapshot real; dunder behavior omitted |
| 10 Frozen contract | Wire keys covered; signatures/value domain incomplete |

## Ten gate attacks

| Attack | Red-first test? |
|---|---|
| Forged `FrozenMap` repr/hash | No |
| Inherited count/index/containment | No |
| Class alias to an existing edge | No |
| Staticmethod alias to an existing edge | No |
| Partial/callable-object alias | Yes, T5 |
| Hostile finalizer or generator cleanup | No; accepted residual |
| Raw `__init_subclass__` failure | No; contradicts S2 |
| Fresh `KeyboardInterrupt` path | No mutation proof |
| Forced live-container mutation | No; T15 is undefined/flaky |
| Valid dunder/signature drift | No; T12/T13 are partial |

A remote worker is unlikely to finish within two remediation rounds. A third round would most likely come from the `FrozenMap` redesign, owner/name-aware alias registry, finalizer/generator scope decision, or late `_phase_core.py` split.
2026-10-30T17:04:49.468436Z ERROR codex_core::session: failed to record rollout items: thread 01a18dbf-a2a6-7a02-82be-e3c872fda5d8 not found
tokens used
102,177
Verdict: **NO-GO**: 4 BLOCKER, 4 HIGH, 3 MEDIUM.
