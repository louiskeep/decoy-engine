# Equivalent-mutant ledger: `keyprovider.py`

TQ crown-jewels pass, 2026-07-25. A mutmut run against `keyprovider.py` (the
keyed-mask secret boundary, DE-02) left **46 survivors**. Every survivor was
classified LOGIC or EQUIVALENT per `docs/quality/module-test-quality-playbook.md`
("Scope the score to LOGIC, not error-message wording"). **21 LOGIC survivors**
were killed with new tests in `tests/property/test_keyprovider_invariants.py`;
**25 survive and are equivalent** (message-prose-only, or a no-op codec-name
case variant), listed below with the one-line argument for why no input can
distinguish them from the original.

Covering tests: `tests/unit/test_de02_keyprovider.py` (path-completeness,
example-based) + `tests/property/test_keyprovider_invariants.py` (this
module's oracle layer -- FAIL-CLOSED, DETERMINISM, ISOLATION, NO LEAKAGE, and
ERROR TAXONOMY properties, plus the 12 targeted tests added in this pass).

Bugs found in `keyprovider.py`: none. Every LOGIC mutant traces to a real
test gap, not a defect in the module itself.

## WORDING (23): error-message prose only

mutmut lowercases/uppercases a message fragment, wraps it in `XX...XX`, sets
`message=None`, or drops the `message=` kwarg entirely (defaulting it to
`""`). In every case the literal is consumed only inside a raised
`MaskSecretError`'s (or `MissingMaskSecret`'s) human `message`; it never
becomes the exception's `code`, a return value, or a comparison target. Tests
assert the `code` (and, where the module docstring calls out redaction, that
specific safe values like `<redacted ref, N chars>` appear), so only
pure-wording variants survive.

| Mutant | Mutation |
|---|---|
| `MissingMaskSecretǁ__init__ǁmutmut_1` | default `message=""` -> `message="XXXX"` (never observed: every call site passes an explicit message) |
| `MissingMaskSecretǁ__init__ǁmutmut_3` | `message=message` -> `message=None` in the `super().__init__` passthrough |
| `MissingMaskSecretǁ__init__ǁmutmut_5` | `message=message` kwarg dropped from the `super().__init__` passthrough |
| `WeakMaskSecretǁ__init__ǁmutmut_1` | default `message=""` -> `message="XXXX"` (same dead-default reasoning as above) |
| `_decode_secret_materialǁmutmut_6` | empty-material error: `message="secret material is empty."` -> `message=None` |
| `_decode_secret_materialǁmutmut_8` | empty-material error: `message=` kwarg dropped |
| `_decode_secret_materialǁmutmut_11` | empty-material error: message wrapped in `XX...XX` |
| `_decode_secret_materialǁmutmut_12` | empty-material error: message uppercased |
| `_decode_secret_materialǁmutmut_20` | hex-nor-base64 error: `message=...` -> `message=None` |
| `_decode_secret_materialǁmutmut_22` | hex-nor-base64 error: `message=` kwarg dropped (`from exc` is untouched -- verified via `mutmut show`, not a chaining mutation) |
| `_decode_secret_materialǁmutmut_25` | hex-nor-base64 error: message wrapped in `XX...XX` |
| `_decode_secret_materialǁmutmut_26` | hex-nor-base64 error: message uppercased |
| `resolve_mask_secret_refǁmutmut_7` | "env ref has no variable name" error: `message=None` |
| `resolve_mask_secret_refǁmutmut_9` | "env ref has no variable name" error: `message=` dropped |
| `resolve_mask_secret_refǁmutmut_12` | "env ref has no variable name" error: message wrapped in `XX...XX` |
| `resolve_mask_secret_refǁmutmut_13` | "env ref has no variable name" error: message uppercased |
| `resolve_mask_secret_refǁmutmut_17` | `MissingMaskSecret(f"environment variable {name!r}...")` positional message -> `None` |
| `resolve_mask_secret_refǁmutmut_25` | "file ref has no path" error: `message=None` |
| `resolve_mask_secret_refǁmutmut_27` | "file ref has no path" error: `message=` dropped |
| `resolve_mask_secret_refǁmutmut_30` | "file ref has no path" error: message wrapped in `XX...XX` |
| `resolve_mask_secret_refǁmutmut_31` | "file ref has no path" error: message uppercased |
| `resolve_mask_secret_refǁmutmut_39` | `MissingMaskSecret(f"mask secret file {path!r}...")` positional message -> `None` |
| `resolve_mask_secret_refǁmutmut_41` | invalid-UTF-8-file error: `message=None` (`from exc` untouched, same as mutmut_22) |

## NO-OP (1): a codec name case variant

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `resolve_mask_secret_refǁmutmut_37` | `open(path, encoding="utf-8")` -> `encoding="UTF-8"` | Python's codec registry (`codecs.lookup`) normalizes encoding names case-insensitively (and strips `-`/`_`/space variants); `"utf-8"` and `"UTF-8"` resolve to the identical codec, so the file decodes identically either way. The new `test_resolve_ref_file_open_uses_utf8_encoding_explicitly` test deliberately asserts `str(encoding).lower() == "utf-8"` (not literal equality) so this true no-op is not flagged as a false gap -- it distinguishes the codec name from `None`/a dropped kwarg (the two LOGIC mutants below), which are not case variants of the same codec. |

## LOGIC (21): killed by new tests in this pass

All in `tests/property/test_keyprovider_invariants.py`, appended after
`TestErrorTaxonomy` under the "TQ crown-jewels mutation-kill pass" heading.

| Mutant | Mutation | Killed by |
|---|---|---|
| `_decode_secret_materialǁmutmut_3` | `"".join(text.split())` -> `"XXXX".join(...)` (splices junk between whitespace-separated chunks) | `test_decode_secret_material_strips_whitespace_without_inserting_separator` |
| `_decode_secret_materialǁmutmut_5` | empty-material error: `code="bad_secret_ref"` -> `code=None` | `test_decode_secret_material_whitespace_only_is_bad_secret_ref_code` |
| `_decode_secret_materialǁmutmut_7` | empty-material error: `code=` kwarg dropped (required kwarg-only param -> raises bare `TypeError`, not `MaskSecretError`) | same |
| `_decode_secret_materialǁmutmut_9` | empty-material error: `code="XXbad_secret_refXX"` | same |
| `_decode_secret_materialǁmutmut_10` | empty-material error: `code="BAD_SECRET_REF"` | same |
| `_decode_secret_materialǁmutmut_15` | `base64.b64decode(stripped, validate=True)` -> `validate=None` (falsy -- silently strips invalid characters instead of rejecting them) | `test_decode_secret_material_rejects_invalid_base64_characters` |
| `_decode_secret_materialǁmutmut_17` | same call: `validate=True` kwarg dropped (defaults to `False`, same falsy effect) | same |
| `_decode_secret_materialǁmutmut_18` | same call: `validate=True` -> `validate=False` | same |
| `_redact_refǁmutmut_2` | `ref.startswith(("env:", "file:"))` -> `("XXenv:XX", "file:")` (a real `env:` ref no longer matches -> over-redacted) | `test_redact_ref_shows_env_and_file_refs_verbatim` |
| `_redact_refǁmutmut_3` | same: `"env:"` -> `"ENV:"` | same |
| `_redact_refǁmutmut_4` | same: `"file:"` -> `"XXfile:XX"` | same |
| `_redact_refǁmutmut_5` | same: `"file:"` -> `"FILE:"` | same |
| `_redact_refǁmutmut_6` | `return repr(ref)` -> `return repr(None)` | same |
| `resolve_mask_secret_refǁmutmut_33` | `open(path, encoding="utf-8")` -> `encoding=None` (falls back to the locale-dependent default) | `test_resolve_ref_file_open_uses_utf8_encoding_explicitly` |
| `resolve_mask_secret_refǁmutmut_35` | same call: `encoding="utf-8"` kwarg dropped (same locale-dependent-default effect) | same |
| `key_provider_from_refǁmutmut_6` | `SecretKeyProvider(resolve_mask_secret_ref(ref), key_version=key_version)` -> `key_version=` kwarg dropped (silently falls back to `SecretKeyProvider`'s own `"v1"` default) | `test_key_provider_from_ref_uses_the_given_key_version` |
| `resolve_mask_keyǁmutmut_1` | `provider: KeyProvider \| None = key_provider` -> `= None` (discards the caller's provider) | `test_resolve_mask_key_prefers_explicit_key_provider` |
| `resolve_mask_keyǁmutmut_3` | `if provider is None and mask_secret_ref:` -> `if provider is not None and mask_secret_ref:` (inverted -- breaks both the ref-fallback path and the "explicit provider wins" precedence) | `test_resolve_mask_key_resolves_via_mask_secret_ref_when_no_provider` + `test_resolve_mask_key_explicit_key_provider_wins_over_mask_secret_ref` |
| `resolve_mask_keyǁmutmut_4` | `provider = key_provider_from_ref(mask_secret_ref)` -> `provider = None` (ref given but never resolved) | `test_resolve_mask_key_resolves_via_mask_secret_ref_when_no_provider` |
| `resolve_mask_keyǁmutmut_5` | same line: `key_provider_from_ref(mask_secret_ref)` -> `key_provider_from_ref(None)` | same |
| `resolve_mask_keyǁmutmut_7` | `return require_mask_key(plan, provider)` -> `return require_mask_key(plan, None)` (drops the resolved provider on the final return) | `test_resolve_mask_key_prefers_explicit_key_provider` |

## Regenerate

```
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut run
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut results
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut show <mutant-id>
```
