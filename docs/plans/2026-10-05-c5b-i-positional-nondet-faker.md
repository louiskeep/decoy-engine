Status: plan (revision 2.1, BUILD-READY, author = Opus). Codex plan gate: round 1 REVISE folded; round 2 closed all five round-1 findings with 1 new MEDIUM (an ambiguous default-namespace encoding), folded here as length-prefixed components.

# C5b-i: position-keyed non-deterministic Faker (oracle semantics, routes held constant)

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C, slice C5b ("Default (non-deterministic) Faker selection on the Rust paths"). Split like C1b. C5b-i changes the oracle's draw and makes the metadata truthful. C5b-ii admits the result to the unified slice and the chunked native leg.

Owner decision (Cam, 2026-10-05, AskUserQuestion "Position-keyed, like C1b"):
- non-deterministic Faker selection becomes position-keyed
- the key stays `job_seed`
- the namespace is the configured one, else a per-column default
- REUSE only
- a one-time pre-GA output break for every non-deterministic REUSE Faker column

Branch: `feat/c5b-i-faker-positional` off engine main `4fe5de9d`.

## 1. Goal and scope

**New draw.** For a non-null row at `g = ctx.row_offset + i`, where `i` is the row's position in the frame the handler receives (whole-frame uses `row_offset = 0`):

```
idx = derive_index(job_seed, selection_namespace, encode_int(g), pool_size=pool.size)
value = pool.values[idx]
```

- **Nulls.** Null rows are restored positionally and still consume their ordinal (the same rule as today's REUSE and as C1b).
- **`encode_int`** is the public `decoy_engine.kernel.encode_int`. It is the canonical integer encoding `derive_index_batch` applies to an integer column, so C5b-ii's native uint64 key path is byte-identical with no rework (C1b-ii proved this for categorical).
- **Batch form.** The oracle uses the index kernel in batch over the integer keys: the compiled `derive_index_batch` when the companion is present, the byte-identical reference otherwise. This is the same "compiled if present, reference otherwise" pattern `PoolSampler._deterministic` uses. Do not use a per-row Python loop; Faker columns are large.
- **`selection_namespace`** is `plan.namespace` when it is a non-empty string. Otherwise (`None` OR `""`; rev 2) it is a per-column default built from the table and column with an UNAMBIGUOUS, length-prefixed encoding (rev 2.1): `f"faker-nd/{len(table)}:{table}/{len(column)}:{column}"`. Plain `faker-nd/{table}/{column}` would let table `a/b` + column `c` collide with table `a` + column `b/c`, since names are unrestricted strings (`config/_tables.py:61,273`). So two namespace-less columns never share a stream. That fixes the existing identical-columns bug (section 2, fact 3). The prefix keeps the default out of the space users normally write.
  - `table` is `ctx.current_table`.
  - `column` is `ctx.nested_outer_column` when the handler runs as a nested strategy's child, otherwise the handler's `column` argument (3b).
  - The pool is still built with the ORIGINAL `plan.namespace` (`None` or `""` unchanged), so pool identity is untouched.

**Pool identity and content are UNCHANGED.**
- The pool is still built from `resolve_faker_pool_identity(..., namespace=plan.namespace)` on `job_seed`.
- The selection namespace only keys the draw.
- Two columns can still share one pool (same identity); they now draw different rows from it.

**Scope.**
- Changes: non-deterministic `cardinality_mode: reuse` in the masking Faker handler (`_strategies/_faker.py`) only. The shared `PoolSampler` is not touched, so generation GP2 (`generation/_faker_pool.py:407`) and composites (`sample_bundle`) stay byte-identical.
- Unchanged: non-deterministic UNIQUE / MATCH / SCALE keep today's numpy draw (they need whole-column state).
- Unchanged: deterministic Faker.

**Routing held constant (the split boundary).** Today non-deterministic Faker:
- never runs chunked (`_chunked.py:224-230`)
- never runs out-of-core (`out_of_core/_compat.py:51-63` rejects all Faker)
- never reaches native or unified admission (JC-5 requires deterministic)
- is split-eligible on the multi-table route through the full-frame group

C5b-i keeps every one of those outcomes. A routing predicate that read "non-deterministic means unseeded" changes only its prose, never its outcome. C5b-ii opens the chunked and unified routes.

**Out of scope (C5b-ii):**
- native/unified admission
- the chunked oracle `row_offset` activation and the chunked native positional leg
- `when:` / FK-remap hazard gates
- chunked evidence

## 2. Established facts (engine `4fe5de9d`; C5b research probe)

1. **Today's draw** (`_strategies/_faker.py:85-99`, `generation/pool/_sampler.py:163-181, 259-264`): `select_seed = ctx.job_seed` for non-deterministic; `np.random.default_rng(int.from_bytes(job_seed))`; REUSE is `rng.integers(0, pool.size, n)`, one whole-column stream. `job_seed` is all zeros when no seed is set (`plan/_seed.py:50-55`), so output is already reproducible run to run.
2. **The public docs are false.** `docs/determinism.md:89-91` ("draws from an unseeded RNG; two runs differ"), `docs/strategies.md:24` ("differs run to run"), and the program doc's parity rule (`rust-engine-program.md:24`, which treats C5b as unseeded) all contradict the code.
3. **Bug.** Nothing per column is mixed into the stream. Two non-deterministic columns with the same pool size get identical index streams, and with the same pool identity (provider, locale, config, namespace, including `None`) identical values row for row (verified by probe).
4. **Namespace is optional for non-deterministic Faker today.** Validation only requires it when `deterministic` is set (`providers_v2/identifiers/_validate.py:19-57`). The config default is `deterministic: false` (`config/_tables.py:69`).
5. **Table/column identity at the handler** (corrected in rev 2). `ColumnSeed` carries no table or column, but `StrategyContext` already does:
   - `current_table` (`execution/_adapter.py:140`), stamped before every dispatch by the full-frame/sequential adapter (`_pandas_adapter.py:352-365`) and the orphan-remap path (`_strategies/_orphan.py`).
   - `nested_outer_column` (`:156`), set and restored around a nested child's dispatch (`_strategies/_nested.py:419-430`). A nested column has ONE child handler for all its leaves, which runs on the synthetic `_nested_leaves` column with flattened leaf ordinals (`_nested.py:407-428`).
   - Accepted inputs today include `namespace=""` for non-deterministic Faker: `config/_tables.py:64` has no minimum length, and validation skips non-deterministic columns (`_validate.py:38-42`). `derive_index` rejects an empty namespace (`determinism/_derive.py:265`).
6. **Stale or inaccurate determinism metadata to correct:**
   - `mask.faker` in `_determinism_protocol.py:~634-659` (claims a source-value identity and mask_key root for all Faker, call site stale at `_faker.py:102`, real :86)
   - `_draw_site_providers.py:866`
   - `native/_capabilities.py:228-240` (note says "source-keyed (deterministic mode)")
   - `docs/native/draw-site-inventory.md:172-178, 47-57`
   - tests `tests/native/test_determinism_protocol.py`
7. **Generation shares the sampler branch** (`generation/_faker_pool.py:407-409`, `sample_bundle` :505-524). Both must stay byte-unchanged.

## 3. Decisions

**3a. The draw (handler-only).** In `FakerStrategyHandler.run`, when `not plan.deterministic and plan.cardinality_mode == reuse`:
1. Build the uint64 key array `row_offset + arange(n)`.
2. Call the shared index derivation (compiled if present, else reference) with `mask_key=ctx.job_seed`, `namespace=selection_namespace`, `pool_size=pool.size`.
3. Gather `pool.values[idx]` and restore nulls positionally.

All other modes call `PoolSampler().sample(...)` exactly as today. Reuse the uint64-key construction that `native/_categorical_ext.py:149-187` (`native_categorical_positional`) already has, including its domain check, through a shared helper instead of a copy.

**3b. Where the per-column default namespace comes from** (rev 2: from the context, no plan-schema change). The handler builds the default from `ctx.current_table` and `ctx.nested_outer_column or column`.
- This needs no `ColumnSeed` field, no serialization change, and no manifest reconstruction question. It covers nested Faker leaves, whose child `ColumnSeed` is built separately at `_nested.py:179` and runs on a synthetic column.
- Rev 1's compiled `ColumnSeed.selection_namespace` was withdrawn. Codex round 1 showed it missed nested children and rested on a false premise (fact 5).
- An empty `ctx.current_table` (a minimal test double, or a caller that never stamps it) is a hard error (`StrategyError`, code `faker_positional_table_unknown`) rather than a silent shared default. Every production dispatch path stamps it; the build verifies each one (full-frame, sequential, orphan remap, nested, and any other `handler.run` caller found by grep).
- C5b-ii's native routes derive the same default from the table and column they already know. Factor the default into one helper (`faker_selection_namespace(table, column, namespace)`) used by the oracle now and the native routes later.

**3c. The key stays `job_seed`.** That is DE-02's generation-not-protection rationale for non-deterministic mode, and Cam's decision. Selection never touches `mask_key` here.

**3d. Truthful metadata.**
- Keep the strategy-level mapping `MASK_STRATEGY_TO_SITE["faker"]` (`_determinism_protocol.py:819`) on the deterministic site. It drives the capability `key_source` (`_capabilities.py:415-435`) and native Faker's key binding (`physical/_shadow_bindings.py:259`), which must not change.
- Catalogue the non-deterministic REUSE variant as a SEPARATE site (position-keyed, `job_seed` root, partitionable by row ordinal), the way C1b-i catalogued `mask.categorical_nondeterministic`. Update the golden routing coverage (`tests/native/test_determinism_goldens.py:606,692-705`) and site counts. Keep the structural checks in `tests/native/test_c1b_i_metadata_inventory.py:92-119`, which cover both inventory summaries and every table row, and extend them to the new site.
- Leave UNIQUE/MATCH/SCALE as they are, honestly labeled as whole-column numpy draws.
- Fix the stale call sites.
- Update `_draw_site_providers.py`, `native/_capabilities.py`, `docs/native/draw-site-inventory.md` (and its count summary), `docs/determinism.md`, `docs/strategies.md`, and the program doc's parity premise (`rust-engine-program.md:24`), for Faker.

**3e. Routing prose only.** Any route check whose message says non-deterministic Faker is "unseeded" or "chunk-variant" keeps its predicate, code and outcome, and gets truthful prose: for REUSE, "position-keyed; chunked implementation deferred to C5b-ii"; for UNIQUE/MATCH/SCALE, "whole-column draw; not chunk-safe". Codex round 1 confirmed no C1b-style unseeded-label route trap exists for Faker: multi-table veto sets hold only shuffle and categorical, chunked rejects at `_chunked.py:217` (the auto-chunk planner reuses it), out-of-core rejects all Faker, and native JC-5 and phase-3 admission require deterministic. In particular `_chunked.py:224-230` and its docstring. Out-of-core rejects all Faker for a different reason and is unchanged.

**3f. Owner-flagged pre-GA break.** Every non-deterministic REUSE Faker column changes output once. Two namespace-less columns that used to be identical now differ. Document this in the CHANGELOG and the compatibility contract (a determinism-contract change on the whole-frame path), as C1b-i did.

## 4. Implementation

1. `_strategies/_faker.py`: the 3a draw for non-deterministic REUSE; module docstring.
2. The shared uint64 positional-key helper (extract from `native/_categorical_ext.py`), used by categorical and Faker.
3. The `faker_selection_namespace` helper (3b), plus the empty-`current_table` guard; grep every `handler.run` caller to confirm the stamping.
4. Determinism metadata and docs (3d); routing prose (3e).
5. CHANGELOG, compatibility contract, build record `docs/records/2026-10-05-c5b-i-positional-nondet-faker-build.md`.

## 5. Acceptance tests (written first; red-before recorded)

1. **Formula KAT.** For a fixed `job_seed`, namespace and pool, row `g`'s value equals `pool.values[derive_index(job_seed, ns, encode_int(g), pool.size)]` exactly. Checked against the scalar `derive_index` with no batch kernel, both with the companion present and with the reference path; the two paths agree.
2. **Reproducibility.** Two runs give identical bytes, with seed set and with seed unset.
3. **Identical-columns bug fixed.** Two namespace-less non-deterministic first-name columns in one table differ, and the same column name in two tables differs. Two columns sharing an explicit namespace still share a stream (documented).
4. **Nulls.** Interleaved nulls are restored by position, and a null row consumes its ordinal: row k's value is unchanged whether or not row k-1 is null.
5. **Pool unchanged.** Pool identity and pool values for a non-deterministic column equal the pre-change ones (spy on `PoolBuilder.build` arguments and the resulting pool).
6. **Other modes unchanged.** Non-deterministic UNIQUE, MATCH and SCALE output is byte-identical to main, and so is deterministic Faker output.
7. **Generation unchanged.** GP2 generation and composite (`sample_bundle`) outputs are byte-identical to main for a fixed seed.
8. **Routing constant.** For a non-deterministic Faker table, the chunked, out-of-core, native, unified and multi-table route decisions and their reason codes equal main's, as a parametrized snapshot.
9. **Metadata truthful.** Determinism-protocol and inventory structural checks pass with the new split site, and no Faker-specific "unseeded" / "differs run to run" text remains in active code or public docs (grep gate, as in C1b-i step 7).
10. **3b context default.**
    - Two namespace-less nested Faker columns in one table differ. Sparse leaf matches, multiple leaves and null leaves keep their flattened ordinal rules.
    - An empty-string namespace behaves exactly like `None`: it gets the default, keeps today's pool, and does not crash (regression).
    - An empty `current_table` raises `faker_positional_table_unknown`.
    - Encoding regression: table `exports/customer` + column `name` and table `exports` + column `customer/name` resolve to DIFFERENT selection namespaces, and draw different values (each verified independently against the formula).
11. **Offsets and frames.**
    - Nonzero-offset KATs for both the scalar and batch paths (`row_offset=1000`).
    - A fixture where `mask_key` and `job_seed` differ, proving the key is `job_seed`.
    - Empty and all-null inputs.
    - A `when:` compacted frame, where `g` is the match ordinal (documented).
    - An orphan-remap frame.
    - Companion-present case: a spy proves the compiled `derive_index_batch` is actually called.
12. **Multi-table.** A job with an eligible sibling table plus a non-deterministic Faker table executes with the sibling still split-eligible, and with the same route decisions as main.

Mutation targets (each must be killed):
- key on `mask_key`
- drop the per-column default namespace
- key on the local position without `row_offset`
- skip the null ordinal
- route UNIQUE through the new draw
- build the pool with the selection namespace
- treat `""` as a real namespace
- join the default components with a plain `/` (no length prefix)
- use the synthetic `_nested_leaves` column instead of `nested_outer_column`

## 6. Risk, rollback, gates

- **Risk: R2.** An intended, owner-approved output change on the default Faker path, with no routing change. Rollback is a revert.
- **Gates:** Codex plan gate → Sonnet build (tests first) → dennis → Codex final → ci-mirror → merge under the standing full-green authority (the owner decision covers the output break).

## 7. Plan-gate history

- Round 1 (Codex, gpt-6-astra): REVISE, 2 HIGH / 3 MEDIUM, all folded in rev 2.
  - HIGH: namespace-less nested Faker leaves were missed → the default now comes from context (`current_table`, `nested_outer_column`), and the plan field is withdrawn.
  - HIGH: `namespace=""` would crash → it is normalized like `None` for selection only.
  - MEDIUM: the context premise was false (fact 5 corrected).
  - MEDIUM: test gaps → nonzero offsets, distinct keys, `when:`, orphan and multi-table tests.
  - MEDIUM: metadata consumers → keep the deterministic strategy mapping, catalogue a separate site, update goldens and structural checks.
- Codex answers: the plan field would have been safe only with serialization work (moot now); no unseeded-label route trap exists for Faker; match-ordinal semantics under `when:` are consistent with the approved contract.
- Round 2 (Codex, gpt-6-astra): all round-1 findings CLOSED. 1 new MEDIUM: the `faker-nd/{table}/{column}` default was ambiguous for path-like names. Folded in rev 2.1 as length-prefixed components plus a regression test. Proceeding to build; the final code gates verify it.
