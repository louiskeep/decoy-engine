# F06 RED evidence on baseline `fdd08a89`

Slice 0 test files were committed before any F06 source change (`git diff fdd08a8 <tests-commit> -- src` is empty).

Command:

```
pytest tests/unit/declared_casting tests/acceptance tests/property/test_f06_declared_casting.py \
       tests/sentry/test_f06_declared_casting.py tests/security/test_f06_quarantine_boundary.py -q -p no:cacheprovider
```

Result on the baseline: **200 failed, 14 passed** (per-file counts in `f06-r2/red-slice0-on-baseline.txt`). Failure classes:

| Failure signature | Meaning |
|---|---|
| `ModuleNotFoundError: decoy_engine.declared_casting` | kernel/boundary/quarantine module absent (A2-A8, B4, B6, C1-C5, sentries) |
| `ValidationError ... extra_forbidden` on `logical_type`/`parse_format`/`protected_quarantine` | config surface absent (A1, A8, B1-B5, C5) |
| `assert '<declared_*>' in {'extra_forbidden'}` | the specific registered code is not yet produced (B5, C1) |
| `reasons.find(...) is None` | F29 identities not registered (C6) |
| `PLAN_VERSION == 2` | Plan v3 not stamped (B7) |

Tests that pass on the baseline by design are legacy-compatibility guards (`test_a1_unmodified_legacy_*`,
`test_a9_layout_without_exact_width_*`, `test_b5_legacy_configs_*`, `test_b7_legacy_run_outputs_*`) and
the pure scanner-mutant checks in `tests/sentry/test_f06_declared_casting.py`; they must stay green.
