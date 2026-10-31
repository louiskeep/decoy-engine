# F13 r5: mutation rerun after the `_parse_vault` fix (same scope and harness as r4)

Tool: `mutmut` 3.6.0. Scope unchanged from r4 (`pyproject.toml` `[tool.mutmut]`: `transforms/_ff1.py`,
`secret_providers/_cache.py`, `_preflight.py`, `_errors.py`, `_types.py`, `vault.py`, `keyprovider.py`;
tests `tests/secret_providers`, `tests/unit/test_de02_keyprovider.py`,
`tests/property/test_keyprovider_invariants.py`). CPython 3.10.20, `uv sync --frozen` from this branch's
`uv.lock` (extras dev, mutation, xdist, lint, hashicorp-vault), 4 CPUs. Command, from a clean `mutants/` at
head 8dc35a9: `PYTHONHASHSEED=0 .venv/bin/mutmut run --max-children 4`.

| Run | Mutants | Killed | Survived | Timeout | No tests |
| --- | --- | --- | --- | --- | --- |
| r4 final (ee12b248) | 1509 | 1457 | 51 | 1 | 0 |
| r5 (8dc35a9) | 1505 | 1455 | 47 | 3 | 0 |

The mutant count changes because `_parse_vault` changed: the second decode of the field (the line r4's
mutants 73 and 75 mutated) is gone, and the malformed-escape check and the field control-character check
add mutants, all killed. Every `_parse_vault` mutant is renumbered; the table below gives the r5 numbers.

## What the r5 gate asked (HIGH): mutants 73 and 75

Their line no longer exists, so they cannot be "killed" by name: the fix is the removal of the double
decode they lived on. What the new tests grade instead:

- The 73/75 **shape** (a lenient decode of the field) now has exactly one site, the `parse_qsl(...,
  errors="strict")` call. Its lenient mutants (`errors=None`, argument dropped) are **killed** by
  `test_r5_contract.py` rows `lone-ff-byte`, `lone-lead-byte`, `lone-continuation-byte`, `overlong-slash`,
  `truncated-three-byte`, `utf16-surrogate` (hand-mutation H4 in the red-baseline file shows the same six
  rows red). The lenient mutants of the path and mount decodes are killed by `lone-ff-in-path` and
  `lone-ff-in-mount`.
- Re-adding the second decode (hand mutation H3) turns rows `escaped-percent-then-hex`,
  `escaped-percent-twice` and `escaped-percent-then-ascii-hex` red, so the "exactly once" contract is
  pinned in the other direction as well.
- Against the ee12b248 source with 73 or 75 applied by hand, row `escaped-percent-then-hex` fails with
  `assert '�' == '%ff'` (the U+FFFD acceptance the gate described); against the unmutated ee12b248
  source it fails with `bad_secret_ref`. Details in `2026-10-30-f13-r5-red-baseline.txt`.

## Surviving mutants and why

Unchanged from the r4 report unless marked. The `_cache`, `_preflight`, `_errors`, `context_key_parts` and
`vault` survivors are the same mutants with the same reasons (r4 report, "Surviving mutants and why"),
re-verified by this run's results (`mutmut results`).

| Mutant | Reason |
| --- | --- |
| `_types._parse_vault` 15, 16, 63, 64, 70, 71 (`errors="XXstrictXX"` / `"STRICT"` on the query, path and mount decodes; r4 numbers 14, 15, 62, 63, 69, 70) | Python looks the handler name up only when a decode error happens; the unknown name then raises `LookupError`, which the `parse_ref` boundary turns into `bad_secret_ref`, the same result as `strict`. Equivalent. |
| `_types._parse_vault` 49, 50 (`or` to `and` in the three-way `is None` check; r4 48, 49) | A missing encoding alone reaches `_encoding(None)` and a missing version alone reaches `_VERSION_RE.match(None)`; both end as `bad_secret_ref` through the boundary. Equivalent. |
| `_types._parse_vault` 76 (`or not segments` to `and not segments`; r4 82) | With a netloc, `urlsplit` returns a path that is empty or starts with `/`; empty gives no segments, so both forms reject, and `/...` passes both and is judged per segment. Equivalent. |
| `_types._parse_vault` 73, 75 of r4 (the second field decode made lenient) | **Removed** with the line; see above. |
| r4's `_parse_vault` 77 (`"STRICT"` on the second field decode) | Removed with the line. |
| `_types.context_key_parts` 8, 11, 17 | As in r4. |
| `_cache` 14; `_hit_locked` 9, 12, 13; `invalidate` 10, 11; `clear` 2, 3; `_insert_locked` 3 (timeout: infinite eviction loop) | As in r4. |
| `_preflight._load_legacy` 4, 9; `_admit_boxed` 27 | As in r4. |
| `_errors._classify` 6, 26, 31, 33, 34, 35 | As in r4. |
| `vault.client_from_env` 7, 9, 12, 68; `_parse_rfc3339` 5; `_context_of` 20, 22; `describe` 6; `resolve` 7, 19; `_to_resolved` 37, 59, 74 | As in r4. |
| `transforms._ff1` `_prf` 5, `_s_block` 5, `_str_m_radix` 3, 24, `_byte_len_for_radix_power` 6 | Outside this item, as in r4 (5 survivors). |
| `transforms._ff1.min_domain_length` 11, 12 (**timeout, new in this run**) | Outside this item (F13 does not touch `_ff1.py`; the file is byte-identical to ee12b248). Neither is among r4's survivors or its one timeout. Re-adjudicated standalone after the run (`mutmut run <name>` on an idle machine): still timeout, and the diffs show why: 11 is `value = radix` and 12 is `value /= radix` in the `while value < FF1_MIN_DOMAIN` search, so the loop never terminates. A genuine hang, the same verdict class as r4's `_insert_locked` 3; not a survivor. |

## Mutations of the r5 mechanisms (by hand, each turned a named test red)

See the two hand-mutation tables in `2026-10-30-f13-r5-red-baseline.txt`: H1 to H6 for the parse boundary
(malformed-escape check, field control-character check, the second decode put back, each of the three
decodes made lenient), and M1', M2, M3, Va, Vb for the restored r3 cache-poisoning tests.
