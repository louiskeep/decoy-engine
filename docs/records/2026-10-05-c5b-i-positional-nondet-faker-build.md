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

Divergence from the plan: none. The route snapshot (test 8) pins the planner decision and its rejection keys, the chunked reason code, the out-of-core codes, the native pool rejection and the multi-table veto sets by their main values. Dispatched siblings in test 12 differ from the whole-frame run only in pandas schema metadata, as they do on main for any job.
