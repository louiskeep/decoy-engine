Status: record

# F32 E1 build, half 1 of 2: support bundle types

Plan: Platform `plan/f32-support-bundles` `39c2772c` (revision 5), sections 1.2, 1.3, 3.1, 4 rows 1/3/10, 6.2 T-E1, 10 row E1. This branch `feat/f32-e1-types` and its stacked sibling `feat/f32-e1-labels` split the single branch `feat/f32-e1` (head `55b28933`, base `492f35ac`) because that branch was 1,029 changed lines against the 800 cap. Both halves are built on `origin/main` `9eec6123`. The sibling record `2026-11-02-f32-e1-labels-build.md` covers the allocator, the shape cells and the no-network sentry.

Code status: `_shape.py` is the reviewed `55b28933` code plus the review fixes applied here (a dataclass field is accepted only when it is a `_Checked` bundle type; `Label` rebuilds with its token under pickle, `copy` and `asdict`; `Version` parts are bounded; `TableShape` refuses repeated column labels; `Timings` refuses repeated phases). `_labels.py` and the sentry are not in this half.

## 1. Seam

`_labels.py` imports `_shape.py`, never the reverse, so the types stand alone. `LabelAllocator` and its `__init__` export move to the labels half; this half's tests mint labels with the private token. The no-network sentry (75 lines) and the shape negative cells move to the labels half to keep this half under 800. `StrategyKind` members and the `CODESET`/`TOPCODE` renames (the reason-registry scanner reads `*_CODE*` constants as reason codes, `tests/sentry/_reason_scan.py:28`) are unchanged: 30 members from `SCALAR_HANDLERS` (25), `from_parent`, `reference`, `sequence`, `statistical` and `other`. `test_mirrored_enums_match_engine` pins them, `CountKey`, `NativeStatus` and `OrphanPolicy` against the live sources.

## 2. Red and green

Red: the tests-only commit, run at base `9eec6123`: `ModuleNotFoundError: No module named 'decoy_engine.support'` at collection for every cell. Green at head.

| ID | Test | Head |
|---|---|---|
| T-E1 | `test_bundle_fields.py::test_no_free_str_field` (closure from `SupportBundle`, 13 reachable types, positive control) | pass |
| T-E1 | `::test_label_rejects_name`, `::test_reason_ref_rejects_unregistered`, `::test_versions_from_live_constants`, `::test_version_rejects_free_text` | pass |
| self-attack | `::test_types_reject_lookalikes`, `::test_nested_plain_dataclass_is_refused` | pass |
| drift | `::test_mirrored_enums_match_engine` | pass |
| review fix | `::test_label_survives_pickle_copy_asdict` | pass |

## 3. Mutations (each applied once, run, reverted; tree clean after)

| # | Mutation | Test | Result |
|---|---|---|---|
| M1 | add `name: str` to `TableShape` (reviewed head) | `test_no_free_str_field` | red |
| M2 | drop the token check in `Label.__new__` (reviewed head) | `test_label_rejects_name` | red: `DID NOT RAISE` |
| M3 | skip the registry lookup in `ReasonRef` (reviewed head) | `test_reason_ref_rejects_unregistered` | red |
| M4, M5 | three-part-only regex; `Version` accepts any `str` (reviewed head) | `test_versions_from_live_constants`, `test_version_rejects_free_text` | red |
| M9 | accept any dataclass annotation (`hasattr(tp, "__dataclass_fields__")`) | `test_nested_plain_dataclass_is_refused` | red |
| M10 | rename `Label.__reduce__` | `test_label_survives_pickle_copy_asdict` | red |
| M11 | rename `Label.__deepcopy__` | same | red |
| M12 | `Version` parts `[0-9]+` | `test_version_rejects_free_text` | red |

M1 to M5 were run on `55b28933` before the split; the code they hit is unchanged. M6 to M8 and the shape-cell mutants are in the labels record.

## 4. Gates (T1 only, no full suite)

`ruff check --no-cache src tests testflight scripts`: All checks passed. `ruff format --check src tests`: clean. `mypy src/decoy_engine`: Success, 678 source files. `pytest tests/unit/support tests/sentry/test_public_import_boundary.py tests/sentry/test_reason_registry.py`: 48 passed. The full suite, the secret census and the licence gates ran on `55b28933` only (record `2026-11-02-f32-e1-build.md`: 32294 passed, 0 failed) and were not rerun on this half.

## 5. Not verified

- The full suite, `tests/sentry` as a whole and the module-size sentry on this head.
- A forged `Label` with a well-formed value from hostile in-process code stays V2-LOG.
- `Version` still accepts four-part `1.0.0.0` (V2-LOG, as before).
- No Platform or CLI code was run; Python 3.10 only.
