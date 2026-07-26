# Mutation grading: `transforms/code_set.py` -- LOGIC-100%

**Grade scope: FOCUSED selection only.** This grade ran mutmut against
`transforms/code_set.py` with the test selection restricted to
`tests/unit/transforms/test_code_set.py`, run as
`-k "not docs_strategies_md"`. `test_docs_strategies_md_lists_every_registry_corpus`
is excluded from the selection: it reads `docs/strategies.md` by a CWD-relative
path and is a docs drift guard, not a `code_set` logic test, so it neither
exercises nor grades this module's behavior. Integration and pipeline suites
that also drive the `code_set` strategy (and the out-of-core `CodeSetHandler`
twin) were NOT in the selection, so the survivor count is a conservative lower
bound: some mutants counted "survived" here may be killed by tests outside this
file.

TQ crown-jewels pass, 2026-07-26. `code_set` replaces a healthcare code column
value with a different code from a named corpus (ICD-10, HCPCS, NDC, MCC, or a
customer file). Mask mode picks a replacement by HMAC-SHA256 of the input
(`_pick_from_seq`, domain-exclusion so output != input); gen mode picks per row
via `derive_index` keyed on the column namespace; `chapter_preserve` restricts
the candidate set to the input's chapter and fails closed on a missing chapter
column, an absent chapter, or a sole-member bucket. A mutmut run produced
**342 mutants, 234 killed (68% baseline), 108 survived**. Every survivor was
classified LOGIC or EQUIVALENT per
`docs/quality/module-test-quality-playbook.md` ("Scope the score to LOGIC, not
error-message wording"; crypto-adjacent selection is held to the stricter
"changes the picked code for any input => LOGIC" bar). **61 LOGIC survivors**
were killed with **20 new tests** in `tests/unit/transforms/test_code_set.py`;
the remaining **47 are EQUIVALENT** (error/log message prose, message=None,
proven-identical control flow, defaults that land in the same branch, dead
defensive branches, and message-only args), tabled below with the one-line
argument for why no input distinguishes each from the original.

The mask and gen picks are pinned end-to-end: `apply_code_set` KATs exercise the
HMAC/`derive_index` selection plus the chapter logic, and namespace-sensitivity
tests lock the key derivation so any change to the derived bytes changes an
asserted output. `PlanCompileError(code, path, message)` takes all three as
required arguments, so a dropped `code`/`path`/`message` kwarg raises TypeError
(caught as a non-`PlanCompileError` by the guard tests) -- those dropped-arg
mutants are LOGIC, killed by the same `pytest.raises(PlanCompileError)` tests
that assert the machine fields.

Bugs found in `code_set.py`: none introduced or newly exposed by this pass.

Survivor spread by function (raw / LOGIC-killed / EQUIVALENT):

| Function | Survived | LOGIC killed | EQUIVALENT |
|---|---|---|---|
| `_apply_chapter_preserve` | 54 | 29 | 25 |
| `apply_code_set` | 23 | 13 | 10 |
| `describe_loaded_corpus` | 9 | 9 | 0 |
| `_pick_mask` | 8 | 4 | 4 |
| `resolve_corpus_record` | 5 | 4 | 1 |
| `_pick_from_seq` | 4 | 0 | 4 |
| `_resolve_corpus_path` | 3 | 0 | 3 |
| `_pick_gen` | 1 | 1 | 0 |
| `_get_chapter` | 1 | 1 | 0 |
| **Total** | **108** | **61** | **47** |

## LOGIC (61): killed by new tests in this pass

All killing tests live in `tests/unit/transforms/test_code_set.py`.

### `resolve_corpus_record` (4)

| Mutants | Mutation | Killed by |
|---|---|---|
| `__mutmut_4` | `override_path` positional -> `None` -- a customer corpus is looked up as a shipped name (`not found`) | `TestCorpusResolution::test_customer_record_resolves_without_provenance` |
| `__mutmut_11` | `is_shipped=override_path is not None` -- a no-provenance customer corpus fails closed instead of loading | same |
| `__mutmut_6`, `10` | `expected_source_version=None` / kwarg dropped -- the version pin is never checked | `TestCorpusResolution::test_version_pin_mismatch_fails_closed` |

### `describe_loaded_corpus` (9)

| Mutants | Mutation | Killed by |
|---|---|---|
| `__mutmut_7`, `8` | evidence KEY `"source"` -> `"XXsourceXX"` / `"SOURCE"` | `TestDescribeLoadedCorpusEvidence` (both tests: `summary["source"]` KeyErrors) |
| `__mutmut_16`, `17` | evidence KEY `"license"` -> `"XXlicenseXX"` / `"LICENSE"` | same (`summary["license"]` KeyErrors) |
| `__mutmut_9`, `12`, `15`, `18` | no-provenance fallback `""` -> `"XXXX"` (source / source_version / effective_date / license) | `test_no_provenance_customer_fallback_values` |
| `__mutmut_21` | no-provenance `is_seed` fallback `False` -> `True` | same |

### `_get_chapter` (1)

| Mutant | Mutation | Killed by |
|---|---|---|
| `__mutmut_3` | unknown-code chapter `code[0]` -> `code[1]` | `TestGetChapterFallback::test_unknown_code_uses_first_character` |

### `apply_code_set` (13)

| Mutants | Mutation | Killed by |
|---|---|---|
| `__mutmut_1`, `2` | `mode` default `"mask"` -> `"XXmaskXX"` / `"MASK"` -- omitting mode fails as unsupported | `TestApplyDefaultsAndDispatch::test_mode_defaults_to_mask` |
| `__mutmut_3` | `row_index` default `0` -> `1` | `test_row_index_defaults_to_zero` |
| `__mutmut_47` | `_pick_mask(..., namespace=None)` -- mask key ignores the namespace | `test_mask_namespace_is_threaded_into_key_derivation` |
| `__mutmut_56`, `57`, `62`, `63`, `64`, `65` | gen-namespace guard `code`/`path` -> None / `XX..XX` / case | `test_gen_without_namespace_fails_closed` |
| `__mutmut_59`, `60`, `61` | gen-namespace guard `code`/`path`/`message` kwarg dropped -> TypeError | same |

### `_apply_chapter_preserve` (29)

| Mutants | Mutation | Killed by |
|---|---|---|
| `__mutmut_1` | `row_index` default `0` -> `1` | `TestPrivateRowIndexDefaults::test_apply_chapter_preserve_row_index_defaults_to_zero` |
| `__mutmut_3` | chapter-column guard `or` -> `and` -- misroutes a no-chapter corpus to `chapter_absent` | `TestChapterPreserveGuards::test_missing_chapter_column_code_and_path` |
| `__mutmut_9`, `10`, `15`, `16`, `17`, `18` | `code_set_chapter_column_missing` `code`/`path` -> None / `XX..XX` / case | same |
| `__mutmut_8` | chapter-presence check `rows[0]` -> `rows[1]` -- IndexErrors on a one-row corpus | `test_single_row_chapter_corpus_is_plan_error_not_indexerror` |
| `__mutmut_25`, `26`, `27`, `30` | input-chapter derivation collapses the index lookup to `value[0]` (`= None` / `_get_chapter(None, ...)` / `_get_chapter(value, None)` / `is None` -> `is not None`) | `test_chapter_from_index_not_first_character` |
| `__mutmut_42`, `49`, `50` | `code_set_chapter_absent` `path` -> None / `XX..XX` / case | `test_chapter_absent_path` |
| `__mutmut_67`, `74`, `75` | `code_set_sole_member_bucket` `path` -> None / `XX..XX` / case | `test_sole_member_bucket_code_and_path` |
| `__mutmut_84` | `_pick_from_seq(..., namespace=None)` -- chapter mask key ignores the namespace | `test_chapter_preserve_mask_namespace_threaded` |
| `__mutmut_92`, `93`, `98`, `99`, `100`, `101` | gen-namespace guard `code`/`path` -> None / `XX..XX` / case | `test_chapter_preserve_gen_without_namespace_fails_closed` |
| `__mutmut_95`, `96`, `97` | gen-namespace guard `code`/`path`/`message` kwarg dropped -> TypeError | same |

### `_pick_mask` (4)

| Mutants | Mutation | Killed by |
|---|---|---|
| `__mutmut_12`, `19`, `20` | `code_set_single_row_corpus` `path` -> None / `XX..XX` / case | `TestPickMaskSingleRow::test_single_row_corpus_code_and_path` |
| `__mutmut_29` | `_pick_from_seq(..., namespace=None)` -- mask key ignores the namespace | `TestApplyDefaultsAndDispatch::test_mask_namespace_is_threaded_into_key_derivation` |

### `_pick_gen` (1)

| Mutant | Mutation | Killed by |
|---|---|---|
| `__mutmut_1` | `row_index` default `0` -> `1` | `TestPrivateRowIndexDefaults::test_pick_gen_row_index_defaults_to_zero` |

## EQUIVALENT (47)

### Error / log message prose (31)

Each literal below is interpolated only into the human `message` of a raised
`PlanCompileError` (or the unsupported-mode `ValueError`); it is never returned,
compared, serialized, or used as a `code`/`path`/key. mutmut sets `message=None`,
wraps a fragment in `XX..XX`, or changes its case. The guard tests assert the
`code` and `path` (machine fields) and do not match message prose, so these
survive by design.

| Site | Mutants |
|---|---|
| `apply_code_set` gen-namespace `message` | `__mutmut_58` (None), `66`, `67`, `68`, `69`, `70` (prose) |
| `apply_code_set` unsupported-mode `ValueError` | `__mutmut_79` (`ValueError(None)`; type stays `ValueError`) |
| `_apply_chapter_preserve` chapter-column-missing `message` | `__mutmut_11` (None), `19`-`24` (prose) |
| `_apply_chapter_preserve` chapter-absent `message` | `__mutmut_43` (None), `51`-`55` (prose) |
| `_apply_chapter_preserve` sole-member `message` | `__mutmut_68` (None) |
| `_apply_chapter_preserve` gen-namespace `message` | `__mutmut_94` (None), `102`-`106` (prose) |
| `_pick_mask` single-row `message` | `__mutmut_13` (None), `21`, `22`, `23` (prose) |

### Message-only positional args (2)

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `apply_code_set__mutmut_20` | `_check_source_version_pin(None, ...)` -- `name` -> None | `name` reaches only the mismatch error's message; `code`/`path` are literals set inside the callee |
| `apply_code_set__mutmut_21` | `_check_source_version_pin(..., None, ...)` -- `path` -> None | `path` here drives only the message's `" at {path}"` fragment, not the error's `path` field |

### Proven-identical control flow (6)

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `_resolve_corpus_path__mutmut_2` | `source == "shipped" or not source` -> `and` | the `"shipped"` fast-path and the unknown-source fallthrough both `return (config.code_set, None)`, so the tuple is identical for every source value |
| `_resolve_corpus_path__mutmut_4`, `5` | compared literal `"shipped"` -> `"XXshippedXX"` / `"SHIPPED"` | same: a source that no longer matches `"shipped"` falls through to the identical `(config.code_set, None)` return |
| `_apply_chapter_preserve__mutmut_31`, `32`, `33` | empty-input fallback `value[0] if value else ""` -> `None` / `value[1]` / else `"XXXX"` | this branch is reachable only when the input is empty (a non-empty unknown code returns `code[0]`, not None), so `value` is falsy and every variant yields a chapter absent from every bucket; control still raises `chapter_absent`, only the interpolated message differs |

### Default lands in the same branch (2)

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `_apply_chapter_preserve__mutmut_37` | `buckets.get(input_chapter, None)` (was `[]`) | the default is consulted only on a bucket miss, and both `None` and `[]` are falsy, so `if not bucket` still raises `chapter_absent` |
| `_apply_chapter_preserve__mutmut_39` | `buckets.get(input_chapter, )` -- default dropped -> None | same: `None` on a miss, identical `chapter_absent` branch |

### Unreachable via any config-reachable input (2)

| Mutant | Mutation | Why equivalent |
|---|---|---|
| `resolve_corpus_record__mutmut_5` | `is_shipped=None` | for a customer config `is_shipped` is already `False` and both take the customer branch; for a shipped config only bundled corpora are reachable, and they carry complete, identity-matched, current provenance that validates identically whether or not the shipped-only strictness runs, so no config-reachable input distinguishes `None` from `True` |
| `apply_code_set__mutmut_10` | `is_shipped=None` (self-resolving path) | same argument as `resolve_corpus_record__mutmut_5` |

### Dead defensive branch (4)

| Mutants | Mutation | Why equivalent |
|---|---|---|
| `_pick_from_seq__mutmut_17`, `18`, `19`, `20` | `raise ValueError("hmac_hex returned None ...")` message -> None / `XX..XX` / case | `hmac_hex` never returns None for a non-None `key_value` (every call passes one), so this guard body is dead; its message is never observed |

Section totals: message prose 31, message-only args 2, proven-identical control
flow 6, default-same-branch 2, unreachable 2, dead defensive 4 = **47**.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to
`src/decoy_engine/transforms/code_set.py` with test selection
`tests/unit/transforms/test_code_set.py`, then:

```
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut run
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut results
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut show <mutant-id>
```

Run the covering tests directly (fast, no mutmut) with:

```
python -m pytest tests/unit/transforms/test_code_set.py -q -k "not docs_strategies_md"
```
