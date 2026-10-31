# F13 r4: mutation report (secret-provider cache, admission, errors, types, Vault adapter, keyprovider)

Tool: `mutmut` 3.x. Scope in `pyproject.toml` `[tool.mutmut]`: `_ff1.py` plus `secret_providers/_cache.py`,
`_preflight.py`, `_errors.py`, `_types.py` and `vault.py` (the last two added this round because r4
changed them) and `keyprovider.py`. Tests: `tests/secret_providers`, `tests/unit/test_de02_keyprovider.py`,
`tests/property/test_keyprovider_invariants.py`. CPython 3.10.20, `uv sync --frozen` from this branch's
`uv.lock`. Command: `uv run --frozen mutmut run --max-children 8` from a clean `mutants/`.

| Run | Mutants | Killed | Survived | Timeout | No tests |
| --- | --- | --- | --- | --- | --- |
| 1 (r4 fix, before kill tests) | 1503 | 1298 | 189 | 3 | 13 |
| 2 (with `test_r4_mutation_kills.py`, `parse_ref` boundary) | 1513 | 1447 | 65 | 1 | 0 |
| final (five more kills; local query decoding left lenient) | 1509 | 1457 | 51 | 1 | 0 |

Of the 51 final survivors, 5 are in `transforms/_ff1.py` (outside this item, untouched). The other 46 are
listed below with the reason each one survives. The one timeout (`_cache._insert_locked` 3) turns the
capacity-eviction loop into an infinite loop, which the harness reports as a hang.

The two traceback-locals tests (`test_leak_sentry` and `test_r4_contract`, `capture_locals`) and the
audit-count test are skipped under `MUTANT_UNDER_TEST`, as in r3: mutmut's trampoline frames hold call
arguments, and `docs/` is not copied. The BaseException boundary is still graded under mutmut by
`test_an_extension_base_exception_is_rebuilt_from_a_fixed_code`, which inspects the chain, notes and
args, not locals.

## Corrections to the r3 report (class 8)

- `_errors.closed_error` 6 and 16 ("getattr default dropped") were justified as "the object is a
  `MaskSecretError`, which always has `code` and `message`". **False**: a `MaskSecretError` subclass that
  does not call `__init__` has neither. Verified on bf76e761: the mutant raises `AttributeError` inside
  the `except` block, so it escapes `_contained` with the extension error as `__context__`. Now killed
  by `test_a_hostile_error_object_is_classified_without_escaping[no-init]` (r4 `_classify` 6 survives
  only because `failure_of` is now total; see below).
- `keyprovider.key_provider_from_ref` 13 (`SecretCachePolicy()` to `None`) was justified as "`prepare_secret`
  replaces `None`". **Incomplete**: a custom provider's `preflight` receives `None` directly. Now the
  same call lives in `_preflight._ref_boxed` and is killed by
  `test_key_provider_from_ref_hands_a_default_policy_to_the_provider`.
- The r3 "each new test fails on r2" claim is corrected in `2026-10-30-f13-r4-red-baseline.txt`.

## Surviving mutants and why

| Mutant | Reason |
| --- | --- |
| `_cache.__init__` 14 (`_epoch = 1`); `invalidate` 10, 11; `clear` 2, 3 | Unchanged from r3 and still true: epochs are only compared for equality with their value at load start, so the start value and the step size are irrelevant. |
| `_cache._hit_locked` 9, 12, 13 (`zip` `strict`) | Unchanged from r3 and still true: the full key embeds one id per owner, so two owner tuples that share a key have the same length. |
| `_preflight._load_legacy` 4, 9; `_admit_boxed` 27 (`KeyedStrategyRequiresSecret(None)`) | The error crosses the boundary, which rebuilds it from its code with `KEY_REQUIRED_GUIDANCE`. Asserted at the public boundary by `test_keyed_gates_fail_with_the_guidance_text`. |
| `_errors._classify` 6 (`getattr(exc, "code")` without default) | A missing `code` raises `AttributeError`, which `failure_of` turns into `secret_provider_unavailable`: the same result as the default `None`. |
| `_errors._classify` 26 (`next(..., None)`) | `error_for` uses `text or BAD_REF_MESSAGE`, so `None` and `BAD_REF_MESSAGE` build the same error. |
| `_errors._classify` 31, 33, 34 (fallback code `None`, missing, or `""`) | `error_for` looks the code up in `_CLOSED`; `None` and `""` miss, and a miss builds `SecretProviderUnavailable`, the same as the original fallback. |
| `_errors._classify` 35 (text `"XXXX"` for a non-bad-ref code) | `error_for` reads the text only for `bad_secret_ref`. |
| `_types._parse_vault` 14, 15, 62, 63, 69, 70, 76, 77 (`errors="XXstrictXX"` / `"STRICT"`) | Python looks the handler name up only when a decode error happens. The unknown name then raises `LookupError`, which the `parse_ref` boundary turns into `bad_secret_ref`: the same result as `strict`. |
| `_types._parse_vault` 48 (`fld is None and encoding is None`) | A missing encoding alone then reaches `_encoding(None)`, which rejects it with `bad_secret_ref`. |
| `_types._parse_vault` 49 (`version is None and fld is None`) | A missing version alone then reaches `_VERSION_RE.match(None)`, a `TypeError` that the `parse_ref` boundary turns into `bad_secret_ref`. |
| `_types._parse_vault` 73, 75 (second `unquote` of the field without `errors="strict"`) | **Not equivalent, and not a justified survivor** (r5 gate HIGH): the field was decoded twice, so `%25ff` was rejected by the strict second decode and accepted as U+FFFD by the lenient mutant, and `%2525` read as `%`. r5 removed the second decode (the mutated line no longer exists), pinned one strict decode per component with an accept/reject table, and reran the module: see `2026-10-30-f13-r5-mutation-report.md`. |
| `_types._parse_vault` 82 (`and not segments`) | With a netloc, `urlsplit` returns a path that is empty or starts with `/`. Empty gives no segments, so both forms reject; `/...` passes both and is judged per segment. |
| `_types.context_key_parts` 8 (scheme exact-type check merged into the `__str__` check) | A non-`str` scheme is a field value of a non-scalar type, so `_immutable_value` rejects it. |
| `_types.context_key_parts` 11 (`params is None and not params.frozen`) | When `params` is `None` the mutant raises `AttributeError`, which the boundary maps to `secret_provider_unavailable`, the same as the rejection. A non-frozen dataclass subclass of a frozen dataclass cannot be declared (`dataclasses` raises `TypeError`). |
| `_types.context_key_parts` 17 (`cast(None, ...)`) | `typing.cast` is a no-op at run time. |
| `vault.client_from_env` 7, 9, 12 (`VAULT_SKIP_VERIFY` default `None`, missing or `"XXXX"`) | The default is stringified and compared to the truthy set; `"none"` and `"xxxx"` are not in it, the same as `""`. |
| `vault.client_from_env` 68 (`cert_file or key_file`) | A half pair is rejected a few lines earlier, so `or` and `and` see the same inputs. |
| `vault._parse_rfc3339` 5 (`rstrip("XXZXX")`) | `rstrip` takes a character set. `X` cannot end a Vault timestamp, so only `Z` is ever stripped. |
| `vault._context_of` 20, 22 (`verify` default `None` or missing) | `None` is not `bool` or `str`, so the next line normalises it to `True`, the original default. |
| `vault.describe` 6 (`renewable=False` dropped) | `False` is the field default. |
| `vault.resolve` 7 (`response = ""`) | The value is overwritten by the read or never used, because a failure raises first. |
| `vault.resolve` 19; `_to_resolved` 37 (`ref.version or 1`) | The Vault grammar requires `version` (`_VERSION_RE`, `[1-9]...`), so the fallback never runs. |
| `vault._to_resolved` 59 (`ref.field or "XXXX"`) | The Vault grammar requires a non-empty `field`. |
| `vault._to_resolved` 74 (`"UTF-8"`) | Same codec. |

## Mutations of the r4 mechanisms (applied by hand before the run, each turned a named test red)

| Mutation | Red test |
| --- | --- |
| Vault reads through the caller's client kept on the provider | `test_a_barrier_forced_vault_client_switch_mid_load_cannot_change_the_read_context`, `test_a_vault_provider_is_immutable...`, `test_the_client_security_context_scopes_the_cache[3]` |
| Engine re-reads `resolve_context()` at read time instead of reusing the keyed object | `test_a_barrier_forced_switch_mid_load_cannot_change_the_read_context[context]` |
| Boundary catches only `Exception` | `test_an_extension_base_exception_*` (10 cases) |
| Boundary also swallows `KeyboardInterrupt` / `SystemExit` | `test_interrupts_and_exits_pass_through_untouched[2]` |
| Engine error raised inside the `except` block | 62 tests (chain assertions across the suite) |
| Vault provider made mutable | `test_a_vault_provider_is_immutable_and_never_renders_its_settings` |
| Context repr, scheme or field-type check removed | `test_a_context_that_is_not_immutable_and_value_free_fails_closed[dataclass-repr / other-scheme / mutable-field]` |
| Context frozen check removed | survives; see `context_key_parts` 11 above (`dataclasses` already forbids it) |
| `key_provider_from_ref` bypasses the boundary | `test_an_extension_base_exception_*[key_provider_from_ref-preflight]` |
