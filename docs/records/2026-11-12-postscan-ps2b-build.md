# POSTSCAN-STREAM PS-2b build record: one blocking verdict and key baseline status

Status: record

Plan: `docs/plans/2026-11-12-postscan-stream.md` on `plan/postscan-stream` (4.2.2, 4.8, AT-13, AT-32, AT-37, AT-39, AT-40).
Branch `feat/postscan-ps2b`, cut from `origin/main` 9e451dac, main merged once afterwards.

## What was built

- `verification/postmask/_blocked.py`: `post_scan_verdict`, `PostScanVerdict`, `severity_counts`, `block_location`, `PostScanBlockedError`.
- `_keys.py`: `baseline` and `baseline_error_class` per key; source counts are `null` unless `measured` or `no_source`.
- `reasons.reason_ref_payload`; `phase._reason_wire` and `_keys.py` use it.
- `_load` width guard in `profiling/_scan_integrity.py`; `passes_through` on the three `runner.py` handlers.
- New reason `post_scan/finding_blocked`; `PostScanBlockedError` is a coded family in `reason_adapter._CODED`.
- Four sentries (`tests/sentry/post_scan/`): blocking, pass-through, key core, reason-identity serializer.

## Decisions the plan left open

- Constructor is `PostScanBlockedError(report, verdict=None)`; M4's takes `report` only. `code` is the first blocking code when registered in `post_scan`, else `None`.
- `post_scan.ner_blocked` is an unregistered `ReasonRef` constant fed by the `ner_blocked` flag; M4 registers the code.
- An `fk_preservation` fail or error adds a code only when no key already blocks.
- Source table absent beats source column absent in the baseline.
- The width guard raises `PipelineConfigError` (an existing pass-through family), naming the load role and key label, since `KeyBatches` has no table name.
- `test_a_source_side_type_mismatch_gives_a_zero_baseline` also widened to expect `key_type_mismatch` among the codes (4.2.2 table).
- Registry test edits: stale `_reason_wire` ADAPTERS row removed; one row added for `PostScanBlockedError.__init__`.

## Red and mutations

Red commit d61b3b77: 55 failed on the stubs. Mutations on the final source head, each in a detached copy, applied with a string replace:

| AT | Mutation | Red |
|---|---|---|
| 13 | sort codes by name instead of the fixed order | 7 |
| 32 | verdict ignores findings (keys only) | 12 |
| 32 | skip keys whose status is not `checked` | 11 |
| 37 | report source counts of an unmeasured baseline | 1 |
| 37 | drop `passes_through` from the source-side handler | 2 |
| 39 | drop the width guard | 3 |
| 40 | drop `passes_through` from the fk handler of `runner.py` | 2 |
| 40 | put `{exc}` in the policy_validation message | 1 |
| sentry | raise `PostScanBlockedError` in `_keys.py` | 1 |
| sentry | `import duckdb` in `_keys.py` | 1 |
| sentry | compare `own.schema != parent.schema` in `_keys.py` | 1 |
| sentry | inline `{namespace, code}` in `_blocked.py` | 2 |
| sentry | drop the guard from the source-side handler | 2 |

T1 on a node: 6168 passed, 10 failed (the expected `test_import_layers.py` failures).
