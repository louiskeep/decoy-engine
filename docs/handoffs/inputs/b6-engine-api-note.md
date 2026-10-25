---
Status: input (platform half not started)
---
# B-6 batch token vault writer: engine API note for the platform half

Engine branch `feat/b6-batch-vault-writer-engine`. This note is the contract the
platform's `TokenVaultWriterAdapter` / `VaultStore` half (decoy-platform,
`api/jobs/v2_token_vault.py`, `api/vaults/store.py`) implements against. Nothing
here depends on F13 (PR #339); the writer the platform injects is the same object
it injects today, with two more methods.

## 1. What the engine ships

Module `decoy_engine.execution._adapter` (same import path the platform already
uses for `TokenVaultWriter`; neither Protocol is re-exported from
`decoy_engine.execution`).

```python
class BatchTokenVaultWriter(Protocol):
    def lookup_existing_many(self, namespace: str, plaintexts: Sequence[str]) -> Mapping[str, str]: ...
    def put_many(self, namespace: str, pairs: Mapping[str, str]) -> Mapping[str, str]: ...
```

`TokenVaultWriter` (`lookup_existing(namespace, plaintext) -> str | None`,
`put(namespace, plaintext, token) -> str`) is unchanged. `run_pipeline(...,
token_vault_writer=)` and `PandasExecutionAdapter.run(..., token_vault_writer=)`
are unchanged (typed `Any` / `TokenVaultWriter | None`; a batch writer passes
through them as before).

## 2. How the strategy calls the writer (per tokenize column)

1. Every non-null cell is `unicodedata.normalize("NFC", str(value))`; nulls stay
   null and never reach the vault.
2. The DISTINCT normalized plaintexts are collected in first-seen order.
3. If the writer has BOTH `lookup_existing_many` and `put_many` (callable
   attributes): one call `lookup_existing_many(namespace, distinct)`, then, for
   the misses only, one call `put_many(namespace, {plaintext: proposed_token})`.
   Otherwise (neither, or only one of the two): one `lookup_existing` per
   distinct plaintext and one `put` per miss. A half-implemented writer is a
   per-row writer; the engine never mixes a batch lookup with per-row puts.
4. The emitted token per row is the vault's: the `lookup_existing_many` hit, or
   the value `put_many` returned for that plaintext. The proposed token is a
   proposal only (`tok_` + 32 hex of `derive(mask_key, namespace,
   plaintext.encode("utf-8"))`, HKDF-SHA256 via `decoy_engine.determinism.derive`).

So a 1M-row column with 50k distinct values is 2 writer calls instead of up to
2M. Repeated values are looked up and written once.

## 3. What the platform must implement

`TokenVaultWriterAdapter` grows:

- `lookup_existing_many(namespace, plaintexts)`: return `{plaintext: token}` for
  every plaintext in the input that already has a mapping under
  `(vault, namespace)`. Absent plaintexts are simply absent (never `None`
  values). Keys must be a subset of the input. The input is a list of distinct
  strings; chunk it to the store's bound (the same fingerprint lookup as
  `lookup_existing`, batched with `IN (...)` over the fingerprints).
- `put_many(namespace, pairs)`: persist every `(plaintext, token)` under
  `(vault, namespace)` with the SAME idempotency as `put` (`ON CONFLICT DO
  NOTHING` on the vault+namespace fingerprint), then return
  `{plaintext: authoritative_token}` for EVERY key of `pairs`: the stored token,
  which under a race is the concurrent winner's, not the proposal. Omitting a key
  or returning a non-`str` / empty token aborts the job (section 4). Encrypt each
  plaintext under the vault DEK exactly as `put` does; one write lock
  acquisition per job transaction, as today (`_write_locked`).
- Keep `lookup_existing` and `put`. The engine's per-row path stays live and the
  platform's own fallback tests (`engine_a8_guard.py`) still call it.

`engine_a8_guard.assert_a8_tokenize_seam` can additionally assert
`_assert_params(BatchTokenVaultWriter.lookup_existing_many, ["namespace", "plaintexts"])`
and `_assert_params(BatchTokenVaultWriter.put_many, ["namespace", "pairs"])` once
the pin moves to an engine that ships this branch.

Durability is unchanged: all of a job's `put_many` rows are part of the job
transaction and are committed BEFORE the masked output publishes
(`v2_full_frame.py`), so no published token is ever without its reverse mapping.

## 4. Error contract (what the platform sees)

Every writer failure surfaces as one coded, value-free
`StrategyError(code="tokenize_vault_error")` (an `ExecutionError`) that aborts
the run with no output and no partial sink commit:

- the writer raised (any exception, either method, either path);
- `lookup_existing_many` returned a non-mapping, a key not in the request, or a
  value that is not a non-empty `str`;
- `put_many` returned a non-mapping, omitted a requested key, included a foreign
  key, or returned a non-`str` / empty token;
- `lookup_existing` returned a non-`None` value that is not a non-empty `str`;
- `put` returned a non-`str` / empty token (unchanged).

The engine suppresses the writer's exception entirely: it is not `__cause__`, and
it is not `__context__` either (the error is raised after the `except` block
exits). The platform must therefore log its own vault errors on its side if it
needs the database error text; the engine will never relay it. Platform logs
must not echo the plaintext (catalog class 1).

Existing codes are unchanged: `tokenize_requires_vault`,
`tokenize_requires_namespace`, `tokenize_requires_string_dtype`,
`tokenize_vault_error`, `keyed_strategy_requires_secret`. No new reason code.

## 5. Crypto-adjacent lines (for the queued retroactive crypto review)

No new primitive. Every crypto-adjacent call in
`src/decoy_engine/execution/_strategies/_tokenize.py` on this branch:

| Line (branch) | Call | Existing primitive |
|---|---|---|
| `normalized = [... unicodedata.normalize("NFC", str(value)) ...]` | NFC canonicalization before derive and before the vault key | stdlib `unicodedata`; the same canonicalization `mask.hash`'s string branch uses (`_canonicalize_source`) |
| `minted = {plaintext: _TOKEN_PREFIX + derive(mask_key, namespace, plaintext.encode("utf-8")).hex()[:_TOKEN_HEX_LEN] ...}` | surrogate token proposal | `decoy_engine.determinism.derive` (HKDF-SHA256 expansion keyed on the run's mask key, domain-separated by namespace); byte-identical to the token the per-row path minted before this branch |
| `_vault_call(...)`, `_checked_token(...)`, `_checked_tokens(...)` | none | plain control flow; they decide whether a writer result may be published, they compute nothing |

`mask_key` is `StrategyContext.mask_key` (the `SecretKeyProvider` HKDF root, or
the job seed pre-GA), unchanged. The vault's own encryption (DEK, AES-GCM) is the
platform's, untouched.

## 6. Tests on the engine side

`tests/unit/execution/test_tokenize.py`: 32 test functions, 52 cases. The 8 V0 tests
and every later per-row test pass unchanged. The 5 batch tests from the original
branch are ported to the namespace-scoped seam; 9 test functions are new
(namespace scoping of both batch calls, NFC before the batch lookup, authoritative
`put_many` return, bad return shapes for both batch calls, raising batch writers,
half-implemented writer, per-row lookup return shape, and the exception-chain
canary on both paths). Each new mechanism was mutated once and its test went red.
