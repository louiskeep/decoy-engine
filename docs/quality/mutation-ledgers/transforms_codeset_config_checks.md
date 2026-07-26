# Mutation grading: `transforms/_codeset_config_checks.py` -- LOGIC-100%

TQ step-4 pass, graded 2026-07-26. `_codeset_config_checks.py` (112 LOC) is the
fail-closed validator for a `code_set` masking config: `validate_code_set_config`
raises a typed `PlanCompileError(code, path, message)` for a missing/non-string
name, a non-scalar corpus source_version, a reserved licensed corpus name used
without a customer source, and an unknown shipped corpus.

**Grade scope: FOCUSED selection** (`tests/unit/transforms/test_code_set.py`, with
`-k "not docs_strategies_md"` -- that one test reads a repo doc absent from the
mutants/ copy).

## Numbers

**91 mutants: 54 killed (59% baseline), 37 survived -> 69 killed after this pass,
22 EQUIVALENT.** LOGIC-mutant score 100%. 0 timeouts.

- **15 LOGIC survivors killed** with 4 new machine-field tests + 3 accept/reject
  supports. 0 product bugs.
- **22 EQUIVALENT survivors** (18 error-message prose, 4 corpus_source-default
  no-ops).

## LOGIC (15): killed by new tests

All are mutations to a raised error's machine field (`code` or `path`); the new
tests assert the exact `code`+`path` for each of the four refusals.

| Refusal | Mutants (path/code -> None/XX/UPPER) | Killed by |
|---|---|---|
| `code_set_name_missing` | 9, 16, 17 | `test_missing_code_set_name_error_fields`, `test_non_string_code_set_name_is_rejected` |
| `code_set_corpus_source_version_invalid` | 33, 40, 41 | `test_corpus_source_version_non_scalar_error_fields` (+ scalar-accepted) |
| `code_set_reserved_licensed_name` | 65, 72, 73 | `test_reserved_licensed_name_error_fields` (+ allowed-via-customer-source); parametrized per `RESERVED_LICENSED_NAMES` member (finding #9) |
| `code_set_corpus_not_found` | 81, 87, 88 (code); 82, 89, 90 (path) | `test_unknown_shipped_corpus_error_fields` |

## EQUIVALENT (22)

| Mutants | Category | Why equivalent |
|---|---|---|
| `10`, `34`, `66`, `83` (message=None); `18-23`, `42-44`, `74-78` (message string edits) | error-message prose | the raised `PlanCompileError`'s `code`+`path` (the machine fields) are unchanged and asserted; only the human-readable message text differs. |
| `48`, `50`, `53`, `54` | `cfg.get("corpus_source", <default>)` -> None/dropped/`"XXshippedXX"`/`"SHIPPED"` | `source` is consumed only by `not source.startswith("customer:")`; none of these defaults start with `"customer:"`, so `is_shipped_source` is True in every case -- no observable change (unkillable without a source change). |

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `transforms/_codeset_config_checks.py`,
selection to `tests/unit/transforms/test_code_set.py` + `-k "not docs_strategies_md"`,
then `rm -rf mutants && python -m mutmut run`.
