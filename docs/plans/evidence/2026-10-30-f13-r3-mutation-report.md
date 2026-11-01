# F13 r3: mutation report (secret-provider cache, admission, error translation, keyprovider)

Tool: `mutmut` 3.x, scope in `pyproject.toml` `[tool.mutmut]` (`_ff1.py` plus `secret_providers/_cache.py`,
`_preflight.py`, `_errors.py` and `keyprovider.py`). Tests: `tests/secret_providers`,
`tests/unit/test_de02_keyprovider.py`, `tests/property/test_keyprovider_invariants.py`.
CPython 3.10.20, `uv sync --frozen`. Command: `uv run --frozen --python 3.10 mutmut run`.

| Run | Mutants | Killed | Survived | Timeout | No tests |
| --- | --- | --- | --- | --- | --- |
| 1 (before new tests) | 879 | 786 | 84 | 3 | 6 |
| 2 (with `test_r3_mutation_kills.py`) | 879 | 853 | 25 | 1 | 0 |
| 2 + anchored message matches and the two second-invalidation tests, four mutants re-run | 879 | 857 | 21 | 1 | 0 |

Of the 21 survivors, 5 are in `transforms/_ff1.py` (outside this item, untouched). The 16 in scope are
all equivalent or unreachable, as below. The one timeout (`_cache._insert_locked` mutant 3) turns the
capacity-eviction loop into an infinite loop, which is detected as a hang.

Two tests cannot run inside mutmut's working copy and are skipped when `MUTANT_UNDER_TEST` is set:
the traceback-locals canary test (mutmut wraps each function in a trampoline frame that holds the call
arguments, a property of the harness, not of the engine) and the audit-count test (`docs/` is not copied).
Those two are verified by their red-first runs instead (`2026-10-30-f13-r3-red-baseline.txt`).

## Surviving mutants and why

| Mutant | Reason |
| --- | --- |
| `keyprovider.key_provider_from_ref` 13 (`SecretCachePolicy()` -> `None`) | `prepare_secret` replaces `None` with `SecretCachePolicy()`; identical behavior. |
| `_cache.__init__` 14 (`_epoch = 1`) | The epoch is only compared for equality with its value at load start; the starting value is irrelevant. |
| `_cache._hit_locked` 9, 12, 13 (`zip` `strict`) | Owner tuples have equal length by construction: the key embeds one id per owner, so tuples of different length never share a key. |
| `_cache.invalidate` 10, 11; `_cache.clear` 2, 3 (`-= 1`, `+= 2`) | Any change away from the start value fences a loader; these differ from `+= 1` only in which non-start value results, and the epochs are never ordered, only compared for equality. (`invalidate` 9 and `clear` 1 (`= 1`) ARE distinguishable and are killed by the two `a_second_..._fences_a_load_that_began_after_the_first` tests.) |
| `_preflight._digest` 5 (`"UTF-8"`) | Same codec. |
| `_preflight._prepare_boxed` 20 (`ttl_seconds >= 0`) | A TTL of 0 sent through the cache path never hits (`older_than(0)` is always true) and never inserts (`remaining` is 0); same observable result as the direct path. |
| `_preflight._load_legacy` 4, 9; `_preflight._admit_boxed` 27 (`KeyedStrategyRequiresSecret(None)`) | The error crosses `closed_error`, which rebuilds it from its code with `KEY_REQUIRED_GUIDANCE`; the raised message is the same. Asserted at the public boundary in `test_keyed_gates_fail_with_the_guidance_text`. |
| `_errors.closed_error` 6, 16 (`getattr` default dropped) | The object is a `MaskSecretError`, which always has `code` and `message`. |
