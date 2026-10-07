# FPE reference empty strings: build record

Status: record
Date: 2026-10-07. Branch `fix/fpe-reference-empty`, base engine main `9bbde63c`. Plan:
`docs/plans/2026-10-07-fpe-reference-empty.md` (rev 2, Codex plan gate GO). Nothing is pushed or merged.

## Commits

- `a294fa0b` tests first: plan tests 1-7. 27 cases failed before the fix, all with an
  `fpe_unencryptable_domain` row error on `""` or a warning-count mismatch.
- `6fe72a60` the fix in `_ReferenceFpe._run`: a non-null value whose `str()` is `""` is output as
  `""`, skips the transform, and is not added to the warning inputs.
- `8b61821f` adds `_crypto_reference.py` to the `permitted_non_physical` ledger in
  `tests/sentry/test_physical_seam_disconnection.py`. That sentry fails any change under
  `execution/` outside `physical/` that is not listed; every earlier slice added its own entry.
  No assertion changed.

## What changed

- `src/decoy_engine/execution/native/_crypto_reference.py`: the empty branch only.
- `tests/native/test_crypto_ext_contract.py`: six more parity rows (start, middle, end, all-empty;
  digits, separators, ALPHANUM, Luhn). No existing row or assertion changed.
- `tests/native/test_crypto_ext_fpe_empty.py`: warning parity, decrypt and round trip across six
  configurations, value functions still fail closed, the empty boundary in both directions with
  error indices, an object whose `str()` is empty, missing key and bad charset, and a spy proving
  no empty cell reaches either transform.

## Hand mutation (plan test 9)

| Mutant | Result |
|---|---|
| empty check replaced by `False` | killed (27 fail) |
| `text.strip() == ""` | killed (4 fail) |
| warning append moved before the skip | killed (2 fail) |
| `value == ""` instead of `text == ""` | killed (4 fail) |
| `isinstance` shortcut before `str()` | equivalent, same behavior |

## Not in scope

Strategy, unmask and the value functions in `transforms/fpe.py` are untouched. The native extension
is C6a.
