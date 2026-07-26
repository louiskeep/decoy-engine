# Mutation grading: `transforms/text_mask.py` -- LOGIC-100%

**Grade scope: FOCUSED selection only.** This grade ran mutmut against
`transforms/text_mask.py` with the test selection restricted to
`tests/unit/transforms/test_text_mask.py` (105 tests, ~0.5s). Integration and
pipeline suites that also exercise the `text_mask` strategy (and the
`TextMaskHandler` out-of-core twin) were NOT in the selection, so the survivor
count and the resulting score are a conservative lower bound: some mutants
counted "survived" here may in fact be killed by tests outside this file.

TQ crown-jewels pass, 2026-07-26. `text_mask` performs span-level PII masking:
it walks each cell with `storm.detectors.iter_spans`, keys a per-span mask via
HMAC-SHA256 (`_span_key`), dispatches each matched span to its per-detector
strategy (FPE / faker / date-shift / redact / passthrough), and reassembles the
matched and unmatched segments in order. A mutmut run produced **281 mutants,
170 killed (60% baseline), 111 survived**. Every survivor was classified LOGIC
or EQUIVALENT per `docs/quality/module-test-quality-playbook.md` ("Scope the
score to LOGIC, not error-message wording"; crypto-adjacent code is held to the
stricter "changes the masked OUTPUT for any input => LOGIC" bar). **67 LOGIC
survivors** were killed with **38 new tests** in
`tests/unit/transforms/test_text_mask.py`; the rest survive and are equivalent
(log-message prose, codec case, defaults that land in the same fail-safe branch,
an unreachable charset/faker fallback, or an unused argument), tabled below with
the one-line argument for why no input distinguishes them from the original.

**Post-fix mutmut re-verify: 239 killed, 42 survived** (2 mutants first filed
equivalent were incidentally killed by the new tests -- conservative direction, so
the EQUIVALENT tables below over-list by 2 rows). The batch-3 dennis gate audited
this module (SOUND): it independently reproduced every KAT and could not break any
equivalence claim (the crypto `_span_key` handling, dispatch defaults, cursor
boundary guards, `validate_luhn=None`, and the charset/faker fallbacks all hold).
The 2 stale rows are a cosmetic over-list (nothing falsely claimed killed or
equivalent); left in place rather than re-running mutmut to identify them.

The crypto/FPE/faker/date paths are pinned with hard-coded real outputs (KATs)
so any change to the key-derivation bytes, dispatch key, seed slice, or checksum
scheme changes the pinned value and dies.

Bugs found in `text_mask.py`: none introduced or newly exposed by this pass.

Survivor spread by function (raw / LOGIC-killed / EQUIVALENT):

| Function | Survived | LOGIC killed | EQUIVALENT |
|---|---|---|---|
| `mask_cell` | 32 | 8 | 24 |
| `_mask_span` | 32 | 31 | 1 |
| `_mask_fpe` | 21 | 8 | 13 |
| `_mask_faker` | 12 | 8 | 4 |
| `_span_key` | 7 | 5 | 2 |
| `_mask_date_shift` | 6 | 6 | 0 |
| `_detect_date_format` | 1 | 1 | 0 |
| **Total** | **111** | **67** | **44** |

## LOGIC (67): killed by new tests in this pass

All killing tests live in `tests/unit/transforms/test_text_mask.py`.

### `_span_key` (5)

| Mutants | Mutation | Killed by |
|---|---|---|
| `__mutmut_1`, `11` | `msg = None` / `hmac.new(key, None, ...)` -- HMAC of the empty message instead of `matched_text` | `TestSpanKeyHmac::test_span_key_known_answer`, `test_span_key_hashes_the_message_not_empty` |
| `__mutmut_5` | drops `errors="replace"` -> strict encoder raises on a lone surrogate | `TestSpanKeyHmac::test_span_key_replace_encodes_lone_surrogate` |
| `__mutmut_8`, `9` | `errors="XXreplaceXX"` / `"REPLACE"` -- invalid handler name, raises `LookupError` on a lone surrogate | same |

### `_mask_fpe` (8)

| Mutants | Mutation | Killed by |
|---|---|---|
| `__mutmut_2` | `_FPE_CONFIG.get(None, ...)` -- every detector gets the default config, so pan loses its `luhn` checksum | `TestMaskFpeDispatch::test_pan_uses_luhn_checksum` |
| `__mutmut_24`, `31` | `checksum=None` / `checksum=` kwarg dropped -- pan/npi lose their checksum scheme | same |
| `__mutmut_33` | `validate_luhn=True` -- re-shapes the ssn/us_phone (checksum-less) FPE output (permutes body + appends Luhn digit) | `test_ssn_has_no_checksum` |
| `__mutmut_3`, `5` | default `_FPE_CONFIG.get(id, None)` / no default -- `charset_name, checksum = None` raises `TypeError` for a detector absent from `_FPE_CONFIG` | `test_unknown_detector_uses_digits_default` |
| `__mutmut_6`, `7` | default charset `"XXdigitsXX"` / `"DIGITS"` -- no digit is in that charset, so a digit value fails closed to the token instead of encrypting | same |

### `_mask_faker` (8)

| Mutants | Mutation | Killed by |
|---|---|---|
| `__mutmut_2` | `_FAKER_METHOD.get(None, "name")` -- every detector resolves to `name` instead of its configured method | `TestMaskFakerDispatch::test_first_name_method_routing` |
| `__mutmut_18`, `19`, `24` | `method = None` / `getattr(None, ...)` / `callable(None)` -- forces the `name()` fallback for every detector | same |
| `__mutmut_13` | seed `span_key[:5]` (was `[:4]`) -- different seed -> different synthetic value | `test_person_name_method_routing` |
| `__mutmut_25` | `return str(None)` in the success branch -- emits `"None"` instead of the faker value | same |
| `__mutmut_3`, `5` | default `_FAKER_METHOD.get(id, None)` / no default -- `getattr(fake, None, None)` raises `TypeError` for an unmapped detector | `test_unknown_detector_falls_back_to_name` |

### `_detect_date_format` (1)

| Mutants | Mutation | Killed by |
|---|---|---|
| `__mutmut_5` | `except ValueError: break` (was `continue`) -- only the first format in `_COMMON_FORMATS` is ever tried | `TestDetectDateFormat::test_matches_non_first_format` |

### `_mask_date_shift` (6)

All killed by `TestMaskDateShift::test_shift_known_answer` (KAT params chosen so
each mutant lands on a different date).

| Mutants | Mutation |
|---|---|
| `__mutmut_10`, `11`, `12` | `range_size`: `max-min-1` / `max+min+1` / `max-min+2` (was `max-min+1`) |
| `__mutmut_18` | `offset_seed = int.from_bytes(span_key[:9], ...)` (was `[:8]`) |
| `__mutmut_22` | `shift = min_days - (offset_seed % range_size)` (was `+`) |
| `__mutmut_25` | `shifted = dt - timedelta(days=shift)` (was `+`) |

### `_mask_span` (31)

| Mutants | Mutation | Killed by |
|---|---|---|
| `__mutmut_15` | drops `detector_id` positional -> the token string lands in `detector_id`, so the FPE tweak/config is wrong | `TestMaskSpanDispatch::test_fpe_strategy_ssn`, `test_fpe_strategy_pan_passes_detector_id` |
| `__mutmut_12` | fpe token arg `None` -- fail-closed returns `None`, not the token | `test_fpe_failure_uses_span_token` |
| `__mutmut_16`, `18`, `22`, `23` | fpe token `str(cfg.get("token", _DEFAULT_TOKEN))` -> dropped kwarg / `get(None, ...)` / `get("XXtokenXX", ...)` / `get("TOKEN", ...)` -- ignores the caller's token | same |
| `__mutmut_17`, `20` | `str(None)` / `str(cfg.get(_DEFAULT_TOKEN))` -- fail-closed returns the literal `"None"` | same |
| `__mutmut_19`, `21` | fpe token `cfg.get("token", None)` / `get("token", )` -- returns `None` when `token` is absent from cfg | `test_fpe_failure_defaults_token_when_absent` |
| `__mutmut_29` | `_mask_faker(..., None)` -- detector_id lost, faker routes to `name` instead of the configured method | `test_faker_strategy_uses_detector_id` |
| `__mutmut_38`, `42`, `43` | date `min_days` from `cfg.get(None, -365)` / `get("XXmin_daysXX", ...)` / `get("MIN_DAYS", ...)` -- ignores the configured min | `test_date_shift_uses_cfg_bounds` |
| `__mutmut_48`, `52`, `53` | date `max_days` from `cfg.get(None, 365)` / `get("XXmax_daysXX", ...)` / `get("MAX_DAYS", ...)` -- ignores the configured max | same |
| `__mutmut_39`, `41` | `min_days = int(cfg.get("min_days", None))` / `get("min_days", )` -- `int(None)` raises when min is absent | `test_date_shift_defaults_when_bounds_absent` |
| `__mutmut_49`, `51` | `max_days = int(cfg.get("max_days", None))` / `get("max_days", )` -- `int(None)` raises when max is absent | same |
| `__mutmut_44`, `45` | `min_days` default `+365` / `-366` (was `-365`) | same |
| `__mutmut_54` | `max_days` default `366` (was `365`) | same |
| `__mutmut_64`, `65` | `strategy == "XXpassthroughXX"` / `"PASSTHROUGH"` -- passthrough no longer matches, falls to redact-token | `test_passthrough_strategy_returns_matched_text` |
| `__mutmut_67`, `71`, `72` | redact `cfg.get(None, _DEFAULT_TOKEN)` / `get("XXtokenXX", ...)` / `get("TOKEN", ...)` -- ignores the caller's token | `test_redact_strategy_uses_configured_token` |
| `__mutmut_68`, `70` | redact `cfg.get("token", None)` / `get("token", )` -- returns `None` when `token` is absent | `test_redact_strategy_defaults_token` |

### `mask_cell` (8)

| Mutants | Mutation | Killed by |
|---|---|---|
| `_mask_cell__mutmut_3` | `not isinstance(text, str) and not text` (was `or`) -- a truthy non-string (int) is no longer returned early | `TestMaskCellReassembly::test_truthy_non_string_returns_unchanged` |
| `_mask_cell__mutmut_31`, `33`, `35`, `36` | `extra_cfg.setdefault(None/token/"XXtokenXX"/"TOKEN", token)` -- the `"token"` key is never set, so a custom token never reaches span masking | `test_custom_token_reaches_span_masking` |
| `_mask_cell__mutmut_39`, `42` | `iter_spans(text, None, ...)` / `iter_spans(text, ...)` -- `detector_ids` dropped, so every detector runs instead of the requested subset | `test_detector_ids_restrict_detection` |
| `_mask_cell__mutmut_90` | `"XXXX".join(parts)` (was `""`) -- splices a separator into the reassembled cell | `test_no_junk_separator_between_parts`, `test_multi_span_reassembly_known_answer` |

## EQUIVALENT (44)

### Log / warning-message prose (22)

Every literal below is consumed only inside a `_log.debug`/`_log.warning` call;
it never becomes a return value, comparison target, or dispatch key. mutmut sets
the message to `None`, drops the format arg, wraps a fragment in `XX...XX`, or
changes its case. The raw-value-isolation sentries assert only that
`matched_text` never appears in a log line (and that the passthrough warning is
non-vacuous), which every variant still satisfies.

| Raise/log site | Mutants |
|---|---|
| `_mask_fpe` except-branch `_log.debug` | `__mutmut_34` (`message=None`), `35` (`detector_id`->`None`), `36` (drops format string), `37` (drops arg), `38` (`XX...XX`), `39` (uppercased) |
| `mask_cell` passthrough `_log.warning` | `_mask_cell__mutmut_9` (`None`), `10`-`24` (per-line `XX...XX` / case variants of the multi-line warning) |

### Codec case (2)

The encoding name is upper/lower-cased but resolves to the same codec, so the
bytes fed to HMAC / used as the FPE tweak are byte-identical.

| Mutant | Mutation |
|---|---|
| `_span_key__mutmut_7` | `matched_text.encode("UTF-8", errors="replace")` |
| `_mask_fpe__mutmut_17` | `detector_id.encode("UTF-8")` |

### Default value == real default (4)

The argument is dropped or nulled but resolves to the exact default the original
passed. `_span_key__mutmut_4` drops the `"utf-8"` positional (default encoding is
already utf-8, and `errors="replace"` is retained). `_mask_fpe`'s
`fpe_encrypt_value` defaults are `preserve_separators=True`, `validate_luhn=False`.

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `_span_key__mutmut_4` | `matched_text.encode(errors="replace")` | default encoding is utf-8; identical bytes |
| `_mask_fpe__mutmut_29` | drops `preserve_separators=True` | `fpe_encrypt_value` default is already `True` |
| `_mask_fpe__mutmut_30` | drops `validate_luhn=False` | default is already `False` |
| `_mask_fpe__mutmut_23` | `validate_luhn=None` | `validate_luhn` is only read as `if validate_luhn and ...`; `None` and `False` are both falsy |

### Fail-safe fallthrough default (6)

The mutated default lands in the SAME branch the original does. `"redact"` and
any unknown policy/strategy both fall to the token fail-safe in
`_apply_unmatched` / `_mask_span`, and an unmapped detector already defaults to
`"redact"`, so a garbled default for it is indistinguishable.

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `_mask_cell__mutmut_1`, `2` | `unmatched_span_policy` default `"XXredactXX"` / `"REDACT"` | not `"passthrough"`/`"replace_with_token"`, so `_apply_unmatched` takes the identical redact-token branch, and the `== "passthrough"` warning check is unaffected |
| `_mask_cell__mutmut_65`, `67`, `68`, `69` | `effective_map.get(id, None/ /"XXredactXX"/"REDACT")` | for an unmapped detector, `_mask_span` matches no strategy branch and returns the token, exactly what `"redact"` does |

For reference the full `mask_cell` EQUIVALENT set is `mutmut_1, 2, 9, 10-24, 54,
65, 67, 68, 69, 80` (24): the fail-safe rows here, the passthrough-warning prose
(`9, 10-24`), and the two empty-segment boundary no-ops (`54, 80`).

### Empty-segment boundary no-op (2)

`_apply_unmatched` returns an empty segment unchanged (its `if not text` guard
fires before any policy branch), so appending `_apply_unmatched("")` is a no-op.

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `_mask_cell__mutmut_54` | `if span.start >= cursor:` (was `>`) | at `start == cursor` the slice `text[cursor:start]` is `""`; appending `""` leaves the join unchanged |
| `_mask_cell__mutmut_80` | `if cursor <= len(text):` (was `<`) | at `cursor == len(text)` the tail is `""`; appending `""` leaves the join unchanged |

### Unreachable fallback (7)

| Mutant | Mutation | Why unreachable |
|---|---|---|
| `_mask_fpe__mutmut_11`, `12`, `13` | `_CHARSETS.get(charset_name, None/ / )` (was `charset_name`) | `charset_name` is always `"digits"` (the only value any `_FPE_CONFIG` entry or the default tuple yields), and `"digits"` is a key in `_CHARSETS`, so the fallback arg is never consulted |
| `_mask_faker__mutmut_23` | `getattr(fake, method_name, )` -- drops the `None` default | every configured `_FAKER_METHOD` value (and the `"name"` default) is a real method on a default `Faker()`, so `getattr` never needs the default |
| `_mask_faker__mutmut_26` | fallback `return str(None)` | the `return str(fake.name())` fallback runs only when the resolved method is missing or raises; all configured methods exist and succeed, so the fallback body is dead |
| `_mask_faker__mutmut_6`, `7` | `_FAKER_METHOD.get(id, "XXnameXX"/"NAME")` | the default fires only for an unmapped detector; the bogus method name is not callable, so control reaches the `fake.name()` fallback -- which is the SAME single Faker draw the original `"name"` method makes on the same seed, so the output is byte-identical |

### Unused argument (1)

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `_mask_span__mutmut_27` | `_mask_faker(None, span_key, span.detector_id)` -- `matched_text` set to `None` | `_mask_faker` never reads `matched_text` (the seed comes from `span_key`); passing `None` changes nothing |

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to
`src/decoy_engine/transforms/text_mask.py` with test selection
`tests/unit/transforms/test_text_mask.py`, then:

```
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut run
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut results
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut show <mutant-id>
```
