# Mutation grading: `execution/_strategies/_bucket_perturb.py` -- LOGIC-100%

TQ step-4 sweep, 2026-07-25. `_bucket_perturb` is a thin fail-closed handler that
snaps dates to a deterministic position within their ISO week / calendar month /
quarter, delegating the compute to `transforms.bucket_perturb.apply_bucket_perturb`
(graded separately). It validates the namespace + config, resolves the `bucket`
default, and passes `date_format` through.

Graded with the FOCUSED selection `tests/unit/execution/test_bucket_perturb.py`
(16 tests, ~0.4s). Conservative lower bound (integration coverage not counted).

**56 mutants: 54 killed, 2 survived.** Baseline was 30 killed (54%); this pass
killed the 24 LOGIC survivors, leaving 2 equivalent. LOGIC-mutant score 100%.

## LOGIC killed this pass (2 new tests + 2 strengthened error tests)

| Mutants | Mutation | Killed by |
|---|---|---|
| `StrategyError` `code=` / `strategy=` field mutants on both raise sites (12: None / `XX...XX` / uppercased) | wrong machine-readable code/strategy | `test_bucket_perturb_requires_namespace` + `test_invalid_bucket_...` strengthened with `assert exc.value.code == ...` and `.strategy == "bucket_perturb"` (the existing `pytest.raises(match=)` only checked message prose) |
| `str(cfg.get("bucket", "month"))` default -> `None` / `""` / `"XXmonthXX"` / `"MONTH"` / dropped, and the `{**cfg, "bucket": bucket}` merge key | a bucket-absent config would fail validation or skip the default | `test_bucket_defaults_to_month_when_absent` |
| `date_format` key/value mutants (6: `cfg.get(None)`, `cfg.get("XXdate_formatXX")`, `cfg.get("DATE_FORMAT")`, `or None` -> `and None`, `= None`, `date_format=None`) -- all force `date_format=None` (auto-detect) | for an AMBIGUOUS date, auto-detect picks a different field order than the configured format, landing the value in a different month | `test_configured_date_format_is_honored_over_autodetect` (day-first `%d-%m-%Y` on `05-02-2024` = 5 Feb; auto-detect picks month-first -> different month) |

The `date_format` gap is a real coverage improvement: the pre-existing tests all
used unambiguous ISO dates (`%Y-%m-%d`), where auto-detect recovers the same
format, so nothing pinned that the configured format is actually consulted.

## EQUIVALENT (2): error-message prose only
`message=None` and the `message=` kwarg drop (`StrategyError.message` defaults to
`""`, so a drop still raises with the right `code`/`strategy`). Tests assert
`.code`/`.strategy`, so pure-message variants survive.

## Regenerate
Repoint `[tool.mutmut]` `only_mutate` to this module + selection
`tests/unit/execution/test_bucket_perturb.py`, then `rm -rf mutants && python -m mutmut run`.
