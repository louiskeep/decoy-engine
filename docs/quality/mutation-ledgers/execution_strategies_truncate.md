# Mutation grading: `execution/_strategies/_truncate.py` -- LOGIC-100%

TQ step-4 sweep, 2026-07-25. `_truncate` keeps the first (or last) N characters of
a value, optionally masking the dropped span with a fill char; the actual
truncation runs in the shared Arrow kernel (`kernel.truncate_array`), so this
handler is a fail-closed CONFIG VALIDATOR that delegates the computation.

Graded with the FOCUSED selection `tests/unit/execution/test_truncate_keep_mask_char.py`
(15 tests, ~0.3s). Other suites exercise truncate through the pipeline; the score
here is a conservative lower bound (integration kills are not counted).

**90 mutants: 76 killed, 14 survived, 0 skipped.** Baseline was 65 killed (72%);
this pass killed the 11 LOGIC survivors, leaving 14 equivalent. LOGIC-mutant
score 100%.

## LOGIC killed this pass (2 new tests + 3 strengthened assertions)

| Mutants | Mutation | Killed by |
|---|---|---|
| `length < 1` -> `<= 1`, `< 2` | reject the smallest valid length (1) | `test_length_one_is_valid` (asserts `length=1` truncates, does not raise) |
| `strategy=None` / `"XXtruncateXX"` / `"TRUNCATE"` on the length / keep / mask_char raises (9 mutants) | wrong `StrategyError.strategy` attribution | `test_invalid_length_raises`, `test_unknown_keep_value_raises`, `test_mask_char_rejects_multi_char` each strengthened with `assert exc.value.strategy == "truncate"` |

## EQUIVALENT (14)

### WORDING (12): error-message prose only
`StrategyError` carries a machine `code` + `strategy` (both asserted) and a human
`message`. mutmut sets `message=None`, drops the `message=` kwarg (it defaults to
`""`, so the raise still succeeds -- contrast `strategy=`, which is required and
in the LOGIC table), wraps a fragment in `XX...XX`, upper/lowercases it, or
mutates `type(x).__name__` -> `type(None).__name__` inside the f-string. Every one
flows only into the human message; tests assert `.code`/`.strategy`, so only
pure-wording variants survive. (`run__mutmut_16`/`53`/`72` are the `message=`
kwarg drops.)

### DEFAULT NO-OP (2)
`from_end_legacy = bool(cfg.get("from_end", False))` -> `cfg.get("from_end", None)`
and the dropped default. `bool(False) == bool(None) == False`, so when `from_end`
is absent the result is identical; when present, the default is not consulted.
No input distinguishes them.

## Regenerate
Repoint `[tool.mutmut]` `only_mutate` to this module + test selection
`tests/unit/execution/test_truncate_keep_mask_char.py`, then `rm -rf mutants && python -m mutmut run`.
