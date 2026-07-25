# Mutation grading: `quality/dp_provenance.py` -- LOGIC-100%

TQ crown-jewels pass, re-graded 2026-07-25. `dp_provenance.py` is the fail-closed
proof-stack provenance gate for the DP fit (an artifact's `(epsilon, delta)`
guarantee is only honest on the exact stack it was tested against). A baseline
mutmut run left **87 survivors out of 309 mutants (217 killed at baseline)**.
Every survivor was classified LOGIC or EQUIVALENT per
`docs/quality/module-test-quality-playbook.md` ("Scope the score to LOGIC, not
error-message wording"). **36 LOGIC survivors** were killed with 17 new direct
tests (plus two strengthened assertions) in
`tests/unit/quality/test_dp_provenance.py`; **51 survive and are equivalent**
(message-prose-only, one encode-case, one `str(exc)` arg, and three
environment-conditional version-guard mutants), listed below with the one-line
argument for why no input on this interpreter can distinguish them.

After this pass: 253/309 killed (raw), **LOGIC-mutant score 100%** (0 logic
mutants survive). The logic-mutant score is the one the bar applies to, and it
clears the bar for this fail-closed gate.

## Why the baseline survived everything

The pre-existing suite validates the fail-closed GATE logic
(`check_fit_environment`, `validate_recorded_provenance`,
`assert_lock_matches_installed`) by monkeypatching the real helpers
(`compute_lock_fingerprint`, `installed_distribution_set`, `current_platform`)
with synthetic certified / near-miss stand-ins, so mutations INSIDE those real
functions, and in the uncovered malformed-record and lock-parse branches, could
not be killed. The new tests grade those implementations DIRECTLY (real
fingerprint, real `_any_marker_true`, real `current_platform`) and cover the
branches the monkeypatched gate tests never reached.

Covering tests: `tests/unit/quality/test_dp_provenance.py` (example-based gate +
this pass's direct implementation tests) + `tests/property/test_dp_provenance_invariants.py`
(the Hypothesis oracle layer, Phase A).

Bugs found in `dp_provenance.py`: none. Every logic mutant is killed; the module
is correct.

## LOGIC (36): killed by new/strengthened tests in this pass

All in `tests/unit/quality/test_dp_provenance.py`. Note: `ProvenanceError.__init__`
requires BOTH `code` and `message` as keyword-only args with no defaults, so a
mutant that DROPS either kwarg raises `TypeError` at the raise site instead of
`ProvenanceError` -- observable, and killed by any test that expects
`ProvenanceError` on that branch. (This is the opposite of `ExecutionError`,
whose `message` defaults to `""`; see the `_fk_keys.py` ledger.)

| Mutant | Mutation | Killed by |
|---|---|---|
| `x_installed_distribution_set__mutmut_32` | collapse `continue` -> `break` (drops every dist after a same-version duplicate) | `test_installed_distribution_set_processes_dists_after_a_collapsed_duplicate` |
| `x__canonical_serialization__mutmut_2` | join separator `"\n"` -> `"XX\nXX"` (changes the pre-hash bytes) | `test_canonical_serialization_is_bare_newline_joined_sorted` + `test_compute_lock_fingerprint_known_answer` |
| `x_current_platform__mutmut_2` | `libc_ver()[0] or "unknown"` -> `... and "unknown"` (a glibc host reports `"unknown"`) | `test_current_platform_libc_is_the_real_family_not_unknown` |
| `x_current_platform__mutmut_3` | `libc_ver()[0]` -> `libc_ver()[1]` (reads the version, not the family) | same |
| `x_current_platform__mutmut_4` | fallback `"unknown"` -> `"XXunknownXX"` (RETURNED as `.libc`, compared vs certified) | `test_current_platform_libc_falls_back_to_unknown_when_absent` |
| `x_current_platform__mutmut_5` | fallback `"unknown"` -> `"UNKNOWN"` | same |
| `x__coerce_platform__mutmut_30` | else-branch `code=None` | `test_validate_recorded_provenance_rejects_platform_of_unsupported_type` |
| `x__coerce_platform__mutmut_32` | else-branch `code=` kwarg dropped (-> `TypeError`) | same |
| `x__coerce_platform__mutmut_33` | else-branch `message=` kwarg dropped (-> `TypeError`) | same |
| `x__coerce_platform__mutmut_34` | else-branch `code="XX...XX"` | same |
| `x__coerce_platform__mutmut_35` | else-branch `code="DP_PROVENANCE_RECORD_MALFORMED"` | same |
| `x__coerce_platform__mutmut_42` | fields-all-str `code=None` | `test_validate_recorded_provenance_rejects_platform_fields_not_all_strings` |
| `x__coerce_platform__mutmut_44` | fields-all-str `code=` kwarg dropped (-> `TypeError`) | same |
| `x__coerce_platform__mutmut_45` | fields-all-str `message=` kwarg dropped (-> `TypeError`) | same |
| `x__coerce_platform__mutmut_46` | fields-all-str `code="XX...XX"` | same |
| `x__coerce_platform__mutmut_47` | fields-all-str `code="DP_PROVENANCE_RECORD_MALFORMED"` | same |
| `x_assert_lock_matches_installed__mutmut_24` | no-`[[package]]` `code=None` | `test_assert_lock_matches_installed_no_package_array_is_parse_error` |
| `x_assert_lock_matches_installed__mutmut_26` | no-`[[package]]` `code=` kwarg dropped (-> `TypeError`) | same |
| `x_assert_lock_matches_installed__mutmut_27` | no-`[[package]]` `message=` kwarg dropped (-> `TypeError`) | same |
| `x_assert_lock_matches_installed__mutmut_28` | no-`[[package]]` `code="XX...XX"` | same |
| `x_assert_lock_matches_installed__mutmut_29` | no-`[[package]]` `code="DP_LOCK_PARSE_ERROR"` | same |
| `x_assert_lock_matches_installed__mutmut_32` | entry guard `... or "version" not in entry` -> `... and "version" not in entry` (a name-only entry is indexed then `entry["version"]` crashes) | `test_assert_lock_matches_installed_skips_entries_missing_name_or_version` |
| `x_assert_lock_matches_installed__mutmut_33` | entry guard `not isinstance(entry, dict) or ...` -> `... and ...` (a version-only entry is indexed then `entry["name"]` crashes) | same |
| `x_assert_lock_matches_installed__mutmut_41` | entry-skip `continue` -> `break` (abandons the loop, dropping every entry after a malformed one) | `test_assert_lock_matches_installed_continues_past_a_malformed_entry` |
| `x_assert_lock_matches_installed__mutmut_48` | `_any_marker_true(markers, Marker)` -> `_any_marker_true(None, Marker)` (every marked entry treated inactive) | `test_assert_lock_matches_installed_honors_a_true_marker` |
| `x_assert_lock_matches_installed__mutmut_49` | `_any_marker_true(markers, Marker)` -> `_any_marker_true(markers, None)` (marker construction always raises -> inactive) | same |
| `x_assert_lock_matches_installed__mutmut_52` | marker-skip `continue` -> `break` (abandons the loop after a marker-inactive entry) | `test_assert_lock_matches_installed_continues_past_a_marker_inactive_entry` |
| `x_assert_lock_matches_installed__mutmut_72` | `strays.append(f"{name}=={version}")` -> `strays.append(None)` (report loses the offending name) | `test_assert_lock_matches_installed_detects_stray_install` (strengthened: asserts `bar==1.0` in message) |
| `x_assert_lock_matches_installed__mutmut_74` | `drift.append(f"...")` -> `drift.append(None)` (report loses the offending name) | `test_assert_lock_matches_installed_detects_version_drift` (strengthened: asserts `foo==2.0` in message) |
| `x__any_marker_true__mutmut_1` | `if not isinstance(markers, list):` -> `if isinstance(markers, list):` (guard inverted; a real list returns False) | `test_any_marker_true_evaluates_a_true_marker` |
| `x__any_marker_true__mutmut_2` | non-list guard `return False` -> `return True` (a non-list markers value is counted active) | `test_any_marker_true_non_list_is_false` |
| `x__any_marker_true__mutmut_3` | `if not isinstance(marker, str):` -> `if isinstance(marker, str):` (every str marker skipped) | `test_any_marker_true_evaluates_a_true_marker` |
| `x__any_marker_true__mutmut_4` | non-str-element `continue` -> `break` (a non-str element aborts the scan) | `test_any_marker_true_ignores_non_string_elements_but_evaluates_the_rest` |
| `x__any_marker_true__mutmut_5` | `marker_cls(marker)` -> `marker_cls(None)` (construction always raises -> never satisfied) | `test_any_marker_true_evaluates_a_true_marker` |
| `x__any_marker_true__mutmut_6` | satisfied `return True` -> `return False` (a true marker is not counted) | `test_any_marker_true_evaluates_a_true_marker` |
| `x__any_marker_true__mutmut_7` | except-branch `continue` -> `break` (a malformed marker aborts the scan) | `test_any_marker_true_malformed_marker_is_not_satisfied_and_does_not_stop` |

Two LOGIC survivors were found BEYOND the planned kill list and killed anyway:
`current_platform` mutmut_4/mutmut_5 (the `"unknown"` fallback literal is RETURNED
as `PlatformTriple.libc` and compared against the certified platform, so it is a
logic value, not message prose; killed by monkeypatching `libc_ver` to report no
family), and the `message=`/`code=` KWARG-DROP mutants (they raise `TypeError`
because `ProvenanceError` has no kwarg defaults).

## EQUIVALENT (51): no input on this interpreter can kill them

### WORDING (46): error-message prose only

The literal is interpolated into a raised `ProvenanceError`'s human `message`
and never becomes the exception's `code`, a return value, or a comparison target.
mutmut sets `message=None`, wraps a fragment in `XX...XX`, or upper/lowercases it;
tests assert `.code`, so only pure-wording variants survive. (`message=None` is a
no-op because `message` accepts any value; contrast the `message=` KWARG-DROP
mutants, which raise `TypeError` and ARE in the LOGIC table.)

| Function | Mutants |
|---|---|
| `installed_distribution_set` | `mutmut_8`, `13`, `14`, `15`, `16`, `17`, `25`, `30`, `31` |
| `check_fit_environment` | `mutmut_4`, `9`, `10`, `11`, `12`, `22`, `27`, `28`, `29`, `30`, `31`, `32`, `33` |
| `validate_recorded_provenance` | `mutmut_5`, `16` |
| `_coerce_recorded` | `mutmut_3`, `8`, `20`, `30`, `35`, `36` |
| `_coerce_platform` | `mutmut_11`, `20`, `31`, `36`, `37`, `38`, `39`, `43`, `48`, `49` |
| `assert_lock_matches_installed` | `mutmut_13`, `18`, `25`, `30`, `83`, `84` |

`_coerce_recorded__mutmut_8` and `_coerce_platform__mutmut_39` mutate
`type(recorded)` / `type(value)` to `type(None)` INSIDE the f-string message; the
result flows only into the message prose, so they are wording too.

### ENCODE-CASE (1): case-insensitive codec name

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `x_compute_lock_fingerprint__mutmut_6` | `.encode("utf-8")` -> `.encode("UTF-8")` | Python codec names are case-insensitive, so both produce byte-identical UTF-8 and the identical sha256. No input can distinguish them. |

### STR-ARG (1): the `str(exc)` argument only

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `x_ProvenanceError_____init______mutmut_3` | `super().__init__(f"[{code}] {message}")` -> `super().__init__(None)` | Only changes `str(exc)` / `exc.args[0]`. Callers branch on `exc.code` (asserted everywhere) and read `exc.message` (a separately-set attribute); no test in the suite inspects `str(exc)` or `exc.args`, verified by inspection. Nothing observable in the tested surface changes. |

### VERSION-GUARD, ENVIRONMENT-CONDITIONAL (3): unkillable on this Python 3.10 env

| Mutant | Mutation | Why equivalent HERE |
|---|---|---|
| `x_assert_lock_matches_installed__mutmut_1` | `sys.version_info >= (3, 11)` -> `> (3, 11)` | On CPython 3.10 (this test env) all three predicates evaluate to the SAME branch (`import tomli`), so no input on this interpreter selects a different import. Only a 3.11+ interpreter could distinguish `>=`/`>` (and `(3, 12)`), where the dependency-matrix workflow exercises those rows. Catalogued, not chased. |
| `x_assert_lock_matches_installed__mutmut_2` | `>= (3, 11)` -> `>= (4, 11)` | Same: false on any realistic interpreter, so the else branch (`import tomli`) is always taken here; indistinguishable on 3.10. |
| `x_assert_lock_matches_installed__mutmut_3` | `>= (3, 11)` -> `>= (3, 12)` | Same: on 3.10 both are false -> `import tomli`; only 3.12+ would distinguish. |

## Regenerate

```
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut run
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut results
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut show <mutant-id>
```
