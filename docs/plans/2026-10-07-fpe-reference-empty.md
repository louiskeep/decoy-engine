Status: plan (rev 2, BUILD-READY: Codex plan gate GO in round 2)

Rules consulted: 00-universal, development-loop, risk-and-exceptions, testing, code-review.

# FPE reference kernel: empty strings match the shipped strategy

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C. This is the prerequisite to C6a (FPE on the Rust paths; Cam's decision 2026-10-07: fix this first, as its own slice). Branch `fix/fpe-reference-empty` off engine main `9bbde63c`. Risk R1: internal only. No user-facing output changes.

## 1. Problem

`execution/native/_crypto_reference.reference_fpe()` is the pure-Python reference the native FPE kernel's contract tests compare against (`tests/native/test_crypto_ext_contract.py`). Its class docstring promises output byte-identical to the shipped strategy (`_crypto_reference.py:80-86`). For empty strings it is not:

- **Shipped strategy** (`execution/_strategies/_fpe.py:~185-230`): a non-null `""` is a missing-data cell. It is written back as `""`, never sent to the cipher, and left OUT of the values the residual-risk warnings see (`non_na_values` excludes empties).
- **Unmask** (`unmask.py:~148-160`): passes `""` through unchanged on decrypt.
- **Reference** (`_crypto_reference.py:~145-163`): sends `""` to `fpe_encrypt_value` / `fpe_decrypt_value`. The value function fails closed on an empty domain, so the reference returns `None` with a row error. It also appends `""` to the warning inputs.

C6a will build a Rust kernel byte-matched to this reference. Left as it is, the Rust kernel would reproduce the wrong behavior.

## 2. Decision

In `_ReferenceFpe._run`, a non-null value whose text is `""` is handled the way the strategy and unmask handle it:
- the output is `""`;
- there is no transform call and no row error;
- it is not appended to `non_na_values`, so the warnings see what the strategy's warnings see.

The value functions in `transforms/fpe.py` stay fail-closed for `""`. The carve-out is a strategy-level missing-data policy, as the strategy's own comment says, not a crypto-layer one.

Out of scope: any change to the strategy, unmask, or the value functions; the native extension itself (C6a).

## 3. Acceptance tests (written first)

1. **Encrypt parity.** `test_reference_fpe_matches_shipped_strategy` gains rows mixing `""`, `None` and real values (at the start, middle and end, and all-empty) for each existing config. The reference values must equal the strategy's output, `""` included, with no errors.
2. **Warning parity.** For the same inputs, the reference's warnings equal the strategy's warnings: the same codes and the same counts or fields. One case where an empty cell would change a residual-risk count if it were counted.
3. **Decrypt parity.** `decrypt_batch` on rows with `""` gives `""` with no error, matching `unmask.py`. A round trip of a mixed column returns the original values.
4. **Value functions still fail closed.** `fpe_encrypt_value("")` and `fpe_decrypt_value("")` still raise.
5. **The exact empty boundary, both directions** (Codex round 1):
   - with digits, `["", " ", "---", "123456"]` preserves only the first cell;
   - whitespace-only and separator-only values still fail closed with their existing error codes, at their original row indices after interspersed empty and null rows;
   - a list input holding a non-null object whose `str()` is `""` is treated as empty, which pins the check order (null test, then `str`, then `== ""`).
6. **More configurations:** encrypt and decrypt empty coverage also runs under explicit checksum, `preserve_separators=False`, and `join_group`. Warnings must be complete, including `fpe_join_group_active` on an all-empty batch. Missing-key and invalid-charset failures still raise before any value is processed.
7. **Never ciphered:** a spy on both reference transform functions proves that no empty cell reaches either. Warning exclusion is pinned with `["", "M000001", None]`: affected_values=1, total_values=1.
8. **Existing contract tests unchanged and green.**
9. **Mutation** on the new branch (the empty check, the skip of the warning append), each mutant killed.

## 4. Failure modes

| Risk | Closed by |
|---|---|
| The fix drifts from the strategy's notion of "empty" (e.g. whitespace) | It uses the same `== ""` test as `_fpe.py`; test 1 |
| The warnings diverge | Test 2 |
| The crypto layer loses its fail-closed check | Test 4 |

Rollback: revert the merge commit.

## 5. Review log

- **Codex plan gate, round 1: REVISE** (3 MEDIUM, all tests). Folded as tests 5-7:
  - the empty boundary in both directions;
  - checksum, separators-off and join-group configurations, including the join warning on all-empty input;
  - a spy proving empty cells never reach the transform.

  Codex confirmed the section 1 facts, the `str(value) == ""` policy on both sides, and that only the contract tests call the reference.
- **Codex plan gate, round 2: GO.** No new findings.
