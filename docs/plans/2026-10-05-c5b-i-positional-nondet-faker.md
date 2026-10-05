Status: plan (revision 1, author = Opus). Awaiting Codex plan gate.

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
- **`selection_namespace`** is `plan.namespace` when set. Otherwise it is a per-column default `faker-nd/{table}/{column}`, so two namespace-less columns never share a stream. That fixes the existing identical-columns bug (section 2, fact 3). The prefix keeps the default out of the space users normally write.

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
5. **ColumnSeed carries no table or column** (`plan/_types.py:~53-90`), and `StrategyContext` (`execution/_adapter.py:~100`) carries no current table. The handler gets `column` as an argument but not the table (see 3b).
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

**3b. Where the per-column default namespace comes from.** Recommended: compile it into the plan.
- When a Faker column is non-deterministic with `namespace=None`, the plan compiler sets a new optional `ColumnSeed.selection_namespace = "faker-nd/{table}/{column}"`. The compiler knows the table and column.
- The handler uses `plan.namespace or plan.selection_namespace`.
- The value is explicit, plan-visible and auditable, and the native routes in C5b-ii read the same `ColumnSeed`.
- Alternative: thread the current table through `StrategyContext` per dispatch. Rejected as less visible and only reaching the pandas adapter.

The plan gate should confirm a `ColumnSeed` field addition is safe for plan hashing and serialization (whether the plan hash is a frozen surface, and every place `ColumnSeed` is constructed or serialized). If it is not safe, fall back to the alternative.

**3c. The key stays `job_seed`.** That is DE-02's generation-not-protection rationale for non-deterministic mode, and Cam's decision. Selection never touches `mask_key` here.

**3d. Truthful metadata.**
- Split the Faker draw site into deterministic (source-keyed, `mask_key`) and non-deterministic-REUSE (position-keyed, `job_seed` root, partitionable by row ordinal).
- Leave UNIQUE/MATCH/SCALE as they are, honestly labeled as whole-column numpy draws.
- Fix the stale call sites.
- Update `_draw_site_providers.py`, `native/_capabilities.py`, `docs/native/draw-site-inventory.md` (and its count summary), `docs/determinism.md`, `docs/strategies.md`, and the program doc's parity premise (`rust-engine-program.md:24`), for Faker.

**3e. Routing prose only.** Any route check whose message says non-deterministic Faker is "unseeded" or "chunk-variant" keeps its predicate, code and outcome, and gets truthful prose: "position-keyed; chunked implementation deferred to C5b-ii". In particular `_chunked.py:224-230` and its docstring. Out-of-core rejects all Faker for a different reason and is unchanged.

**3f. Owner-flagged pre-GA break.** Every non-deterministic REUSE Faker column changes output once. Two namespace-less columns that used to be identical now differ. Document this in the CHANGELOG and the compatibility contract (a determinism-contract change on the whole-frame path), as C1b-i did.

## 4. Implementation

1. `_strategies/_faker.py`: the 3a draw for non-deterministic REUSE; module docstring.
2. The shared uint64 positional-key helper (extract from `native/_categorical_ext.py`), used by categorical and Faker.
3. `plan` compiler and `ColumnSeed`: `selection_namespace` (3b).
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
10. **3b plan field.** The compiled plan carries `selection_namespace` exactly for non-deterministic Faker without a namespace, and only there. Plan hashing and serialization pass their existing tests.

Mutation targets (each must be killed):
- key on `mask_key`
- drop the per-column default namespace
- key on the local position without `row_offset`
- skip the null ordinal
- route UNIQUE through the new draw
- build the pool with the selection namespace

## 6. Risk, rollback, gates

- **Risk: R2.** An intended, owner-approved output change on the default Faker path, with no routing change. Rollback is a revert.
- **Gates:** Codex plan gate → Sonnet build (tests first) → dennis → Codex final → ci-mirror → merge under the standing full-green authority (the owner decision covers the output break).

## 7. Open questions for the plan gate

1. Is adding `ColumnSeed.selection_namespace` safe for plan hashing, serialization and frozen surfaces (3b), or should the default come through the context instead?
2. Does any route predicate, sentry or test key on non-deterministic Faker being "unseeded" in a way that changes its outcome (the C1b-i multi-table trap)?
3. Under `when:` the handler sees a compacted frame, so `g` is the match ordinal (as with C1b). Is that acceptable for C5b-i, given C5b-ii will reject `when:` on the chunked route?
