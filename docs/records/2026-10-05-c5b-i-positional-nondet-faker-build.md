# C5b-i position-keyed non-deterministic Faker: build record

Status: record

Date: 2026-10-05. Plan: `docs/plans/2026-10-05-c5b-i-positional-nondet-faker.md` revision 2.1. Branch `feat/c5b-i-faker-positional` off engine main `4fe5de9d`. Risk R2. Gates pending at the time of writing: dennis, then Codex final.

## What shipped

Non-deterministic `cardinality_mode: reuse` Faker selects by row position. Row `g = ctx.row_offset + i` takes `pool.values[derive_index(job_seed, selection_namespace, encode_int(g), pool.size)]`. The selection namespace is the configured one, else `faker-nd/{len(table)}:{table}/{len(column)}:{column}` (a nested child uses the outer column; `None` and `""` both mean "not configured"). The pool is built exactly as before from the original plan namespace. Routes are unchanged.

| Piece | Where |
| --- | --- |
| The draw and the empty-table guard (`faker_positional_table_unknown`) | `execution/_strategies/_faker.py`, `_strategies/_faker_positional.py` (new) |
| Shared uint64 position-key helper, used by categorical's native positional kernel and Faker | `execution/_positional_keys.py` (new), `native/_categorical_ext.py` |
| New site `mask.faker_nondeterministic`, corrected `mask.faker`, capability note, provider registry | `native/_determinism_protocol.py`, `_draw_site_providers.py`, `_capabilities.py` |
| Truthful chunked rejection prose; predicate and code unchanged | `execution/_chunked.py` |
| Proof manifest sample names (three first names come from this draw) | `docs/proof-manifest.json` |
| Docs | CHANGELOG (owner-flagged output break), `compatibility-contract.md`, `determinism.md`, `strategies.md`, `native/draw-site-inventory.md`, program doc parity rule |

Every `handler.run` caller was checked for the table stamp: the full-frame and sequential adapter (`_dispatch_mask_node`), the orphan-remap closure (`_orphan.py`), and the nested child (`_nested.py`, which adds the outer column). No other caller exists.

## Pre-existing tests that changed

None of the existing unit, native, physical or sentry tests pinned the old numpy-stream values or the "unseeded" prose for Faker. Three files changed for a reason tied to this slice:

- `tests/sentry/test_physical_seam_disconnection.py`: the execution-diff allowlist gains `_positional_keys.py`, `_strategies/_faker.py` and `_strategies/_faker_positional.py`. None imports `execution.physical`.
- `docs/proof-manifest.json`: regenerated with `scripts/gen_proof_manifest.py`. The diff is three sample `first_name` values from the non-deterministic reuse column; `tests/sentry/test_proof_manifest.py` failed until then. The decoy-web proof page needs the same re-sync.
- `tests/native/test_determinism_goldens.py`: a golden for the new site, and the locked routed-site counts move from 21 to 22 (21 reproducing output, one keyed-material).

## Evidence

Red-before (helper modules present, handler unchanged): 44 failed, 62 passed across the new unit file, the new metadata file and the goldens. Every failure was a draw, default-namespace, metadata or prose assertion. The route-snapshot, other-mode and generation tests passed on main unchanged, which is what they pin.

Mutation (each hand-applied, one at a time, reverted): key on `mask_key`, drop the default namespace, key on local position without `row_offset`, skip the null ordinal, route UNIQUE through the new draw, build the pool with the selection namespace, treat `""` as a real namespace, join the default with a plain `/`, use `_nested_leaves` instead of the outer column. All nine killed.

Suites (companion venv, `PYTHONPATH=src`, `pytest-one`): new unit file 71, new metadata file 16, goldens 1 added. `tests/unit/execution` + `tests/native` + `tests/physical` + `tests/sentry` + `tests/parity/native`: 15164 passed, 7 skipped, 59 xfailed, with three `test_module_size` failures that this slice then fixed (below). The rest of the Faker-touching tree (`tests/integration`, `tests/parity`, `tests/unit/generation`, `plan`, `config`, `providers_v2`, `transforms` and the others that grep `faker`) : 5564 passed, 140 skipped, 59 xfailed. After the fix, `tests/sentry` 2465 passed with the new tests and `tests/native` 5117 passed.

Module-size sentry: `_determinism_protocol.py` and `_draw_site_providers.py` are legacy over-max modules that may not grow. The new site and its provider therefore live in the existing pool-Faker siblings (`_draw_sites_gen_pool.py`, `_draw_site_providers_gen_pool.py`), and the edits to the capped modules are line-neutral (927 and 985, the recorded censuses). `_chunked.py` went from 618 to 619 lines and its census is bumped.

Divergence from the plan: none, except that the new site is catalogued in the gen-pool sibling file rather than a new module. The route snapshot (test 8) pins the planner decision and its rejection keys, the chunked reason code, the out-of-core codes, the native pool rejection and the multi-table veto sets by their main values. Dispatched siblings in test 12 differ from the whole-frame run only in pandas schema metadata, as they do on main for any job.
