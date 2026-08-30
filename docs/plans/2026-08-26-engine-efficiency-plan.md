---
Status: plan
Created: 2026-08-26
Updated: 2026-08-28 (Part 1 hot-path scope and evidence-gated Part 2)
Author: Opus (principal-engineer authoring), for Cam
Primary target repo: decoy-engine (85-90% of the work), with decoy-platform route-selection changes (10-15%)
Location: `decoy-engine/docs/plans/` is the canonical home for this engine-owned plan. `decoy-platform/docs/ROADMAP.md` indexes it.
Repos: decoy-engine, decoy-platform
---

# Engine Efficiency: Part 1 Hot Path and Deferred Part 2

> **For agentic workers:** Use the repository development loop for each active slice. Complete the slice gate before you start its successor.

**Goal:** Move the measured masking hot path out of Python per-value loops first. DuckDB owns bounded data movement, Arrow is the batch ABI, and Rust handles keyed derivation. This work targets lower memory use and faster jobs without a change to logical values.

**Architecture:** Python validates config, compiles a backend-neutral `NativeExecutionPlan`, opens DuckDB and Arrow resources, and coordinates publication. DuckDB owns relational and external-memory operations. Arrow `RecordBatch` is the kernel boundary. The determinism protocol preserves each existing draw sequence. A security-reviewed Rust extension wraps the established keyed-derivation primitive. FPE remains on the pandas oracle in Part 1.

**Tech Stack:** Python 3.10-3.13, DuckDB, PyArrow, `decoy_engine.determinism`, NumPy PCG64, Python MT19937, Faker/Mimesis, and Rust through the Arrow C Data Interface. Platform work remains in the FastAPI job runner under `decoy-platform/api/jobs`.

**Spec:** `/home/cam/obsidian-vault/10_Projects/decoy-engine-native-masking-concept.md` (the verified read-only investigation this plan makes concrete). Prior review findings are incorporated into this plan. Executors read the spec and this plan.

## Decisions (Cam, 2026-08-28) - binding

1. **Split the program into two parts.** Part 1 ships the measured hot path. Part 2 stays deferred until benchmark or customer evidence activates a dependency-closed slice.
2. **Keep all Phase 0 foundations.** Implementation is complete. The Opus review reported merge-ready at `13f91491`. Scratch cleanup, the exact-HEAD repository gate, and merge remain open.
3. **Keep Phase 1 narrow and all-or-nothing.** If every table qualifies, the job can stream. One non-streamable table sends the full job to the oracle.
4. **Limit the Part 1 Phase 2 scope.** Native work includes `passthrough`, `redact`, `truncate`, and keyed `hash`. It also includes the Rust `KeyedDerivationKernel`. Native FPE moves to Part 2.
5. **Limit the Part 1 Phase 3 scope to the C1 masking path.** Build bounded pools only for poolable Faker providers that C1 uses. Select values natively with source-keyed masking identity. Add only the cache and state that this path uses.
6. **Keep synthetic generation out of Part 1.** `generate_columns` and other `pool_native` provider families move to Part 2 unless separate evidence activates them.
7. **Enforce `reject_large` during claim-time admission in Part 1.** Unknown or arbitrary Python providers can use the oracle only below an explicit priced small-job limit.
8. **Freeze evidence before each optimization slice.** Record the workload, baseline, target, resource budget, warmup, repetitions, variance method, and tail-reporting method.
9. **Freeze C1 before implementation.** Record the exact recipe, dataset tier, end-to-end latency, hash and Faker throughput, peak RSS, and spill use. Set a minimum speed target or an explicit non-regression limit.
10. **Prove that Part 1 used the intended route.** If the oracle completes, admission rejects the job, or the engine falls back, the C1 gate fails.
11. **Hold the 100M-row cap.** Review the cap only after the Part 1 parity suite, benchmark harness, and prod-sim C1 run complete.
12. **Keep Phase 5 held.** Formula, nested JSON, text/NER, and arbitrary Python providers stay on the pandas full-frame oracle.

## Global Constraints

- **Single-org, one box.** Conservative ~100M-row cap. No distributed/cluster machinery, no multi-node planners. Right-size every design to one host with 8-16GB RAM plus spill disk.
- **Logical value parity is mandatory and EXACT; byte parity is NOT the bar.** For a given seed, the native/streaming route must produce identical logical VALUES, row order, null placement, warnings, and row errors as the pandas full-frame oracle. Physical representation differences (Parquet row-group/dictionary/metadata, Arrow type) are allowed ONLY from an enumerated, tested allow-list recorded per strategy/backend (Part E, T1). Byte-identical artifacts are required ONLY where `decoy-engine/docs/compatibility-contract.md` already binds it (binding once `RELEASE_PHASE` flips to `"ga"`).
- **The determinism decision gates everything.** Phase 0 met this gate. No later kernel can bypass the frozen protocol or its seeded golden vectors.
- **Use established methodology.** Wrap stdlib/`cryptography` crypto and the existing `decoy_engine.determinism` + `generators/derivation.py` envelopes; never reinvent HKDF/HMAC/FPE or invent a new RNG. Before designing streaming k-anonymity, keyed pseudonymization, or external shuffle, survey how columnar engines (DuckDB, Arrow, Polars, SDV) do it and cite the source pattern in the implementing module docstring.
- **Complementary to admission/memory-safety, not a replacement.** The memory ledger in `decoy-platform/docs/plans/2026-08-26-admission-memory-safety.md` stays necessary and its CLAIM-TIME route classifier + estimator are an explicit LANDING DEPENDENCY of this plan (Part F.3). Route selection, the memory reservation, the single lease commit, and the disk preflight all happen at claim time in the admission layer, BEFORE cloud download and worker dispatch. This plan's orchestration consumes the chosen route and the committed lease; it does NOT acquire another lease or register per-table budgets.
- **Determinism protocol is versioned per RNG family.** Any output-shifting change bumps the relevant family's version inside the umbrella protocol (and `SEED_PROTOCOL_VERSION` where it is the single knob, `determinism/_derive.py`, currently 6) with a release-notes line. Pre-GA is a hard cutover; no manifests exist in the wild yet.
- **Whole-job preflight route selection; no mid-stream fallback.** The route is chosen once, before any output is staged or any native kernel runs. After publication or native execution begins there is NO fallback. A missing or ABI-incompatible extension fails or reroutes BEFORE staging.
- **Module sizing.** Engine orchestration modules cap at ~600 LOC (`internal/` import-linter-enforced). Platform `api/jobs/` / `api/scheduler/` cap at 600 LOC (best-practices §4.1).
- **Writing.** No em-dashes anywhere (period/comma/colon). Comments explain why, not what.
- **The pandas full-frame path is the permanent parity oracle**, pinned to `substrate="pandas"`, `execution_mode="full_frame"`, `auto_chunk=False`. Unsupported work continues to use this route.

---

## Part A: Two-Part Roadmap

### A.1 Conditional end-state architecture

This diagram shows the full architecture boundary. Part 1 funds only the hot-path components. Every Part 2 component requires a separate activation decision.

```text
Python config / profile / NativeExecutionPlan compiler   (orchestration only)
          |
          v
NativeExecutionPlan
  - resolved input projections (which columns DuckDB scans, per node)
  - per-node execution requirements (static strategy capabilities + schema/config-resolved requirements)
  - per-output Arrow schema (fixed; nodes with indeterminate type are excluded from the native route)
  - key source (mask_key vs generation seed), determinism family + version per node
  - required prepasses + disk-backed state tables
  - diagnostic reducers (per-code warning/error globalizers)
  - fallback policy per node (native | python_only | reject_large)
  - warning / row-error channel schemas
          |
          v
DuckDB: scan (CSV/Parquet) -> project -> FK join -> stable sort / window -> global aggregate -> spill
          |
          v  (Arrow RecordBatch = the ABI boundary; Arrow C Data Interface to the extension)
          v
Arrow / DuckDB built-in expressions  +  small native strategy kernels
  (keyed derivation, FPE, pool index selection, per-family deterministic draws)
          |
          v
Stage-only writers -> table-atomic publish + honest job manifest
```

Python does: validate config, compile and order the plan, select/init providers and build bounded pools, open DuckDB/Arrow, coordinate first/second passes, aggregate warnings/errors/provenance/metrics, publish per-table and record the manifest. Python does not: touch a per-row value.

### A.2 Why this substrate (decision, not open question)

The spec settled the substrate: DuckDB + Arrow + limited native extensions. Not Polars primary (a second eager relational planner beside the working DuckDB spill/join path, with several "native" registrations that still call pandas: `execution/polars/_strategies/__init__.py:54`). Not pure Arrow compute (no external FK join / global count / external sort / spill machinery). Not DuckDB Python UDFs (they keep the interpreter crossing, GIL pressure, and Python object allocation, and miss the "Python orchestrates only" target). This plan implements that decision; it does not re-litigate it.

### A.3 Phase sequence

| Part | Phase | Scope and value | Status or gate |
|---|---|---|---|
| 1 | 0 | Foundations define determinism, planning, crypto, provider, and parity contracts. | Implementation complete. Merge-ready review at `13f91491`. Cleanup, exact-HEAD gate, and merge remain. |
| 1 | 1 | Narrow multi-table streaming removes full-frame memory amplification for the admitted masking set. | Task 1.1 is implemented at `b84054b4`. Tasks 1.2-1.5 wait for the admission classifier. |
| 1 | 2 | Native `passthrough`, `redact`, `truncate`, and keyed `hash` remove Python per-value work from the masking hot path. | Freeze the workload and performance gate before implementation. Include the Rust `KeyedDerivationKernel` gate. |
| 1 | 3 | The C1 masking slice adds bounded Faker pools, native source-keyed selection, and exact route widening. | Prove C1 admission, `pool_quality`, parity, fidelity, bounded state, speed, memory, and intended-route execution. |
| 2 | 2-4 | Deferred strategies, providers, state, generation, global operations, and relational work. | Activate the smallest dependency-closed slice from measured workload or customer evidence. |
| Held | 5 | Formula, nested JSON, text/NER, and arbitrary Python providers. | Keep these on the pandas oracle. |

### A.4 Dependency graph

```text
Part 1
Phase 0 (implementation complete, merge still open)
  0.1 draw-site inventory ---> 0.3 umbrella determinism protocol (GATE: goldens pass) ---.
  0.2 native planning boundary + 0.2b NativeExecutionPlan compiler ----------------------|--> everything below
  0.4 keyed-crypto contract (pure-Python ref + FPE ref + KAT vectors) -------------------|
  0.5 provider classification (python_only | reject_large | pool_native) ----------------|
  0.6 parity harness (oracle pinned pandas/full_frame/auto_chunk=False) -----------------'
   |
   v
Phase 1 (multi-table streaming default, narrow surface, fail-closed, table-atomic publish)
   |   depends on Phase 0: umbrella protocol (for the parity gate), the parity harness,
   |   the native planning boundary (for eligibility), and the admission route classifier
   |   (LANDING DEPENDENCY from the separate memory-safety effort)
   v
Phase 2 hot path (passthrough/redact/truncate/keyed hash + Rust KeyedDerivationKernel)
   v
Phase 3 C1 masking slice (bounded Faker pools + source-keyed native selection)
   |
   v
Part 1 evidence gate (parity + benchmarks + prod-sim C1, intended route must run)
   |
   v
Part 2 activation decision
   |- remaining Phase 2 strategies and diagnostics
   |- remaining Phase 3 state, providers, and generation
   `- Phase 4 global and relational work

Held: Phase 5 stays on the pandas oracle
```

Phase 1 does not need the compiled crypto extension. It needs the frozen protocol, parity harness, planning boundary, and admission route classifier.

---

## Part B: Phase 0 (Foundational) - Detailed Task-by-Task

**Status: implementation complete, not merged.** The Opus whole-branch review reported merge-ready at `13f91491`. Scratch cleanup and the exact-HEAD repository gate remain. Merge is Cam-gated.

Phase 0 changed no routing or output. It made the determinism decision, defined the native planning boundary, froze the crypto contract, classified providers, and built the parity oracle. The detailed task checklists below remain as the implementation record.

### File structure introduced in Phase 0

- `decoy-engine/src/decoy_engine/execution/native/__init__.py` (new package)
- `decoy-engine/docs/native/draw-site-inventory.md` (new: the exhaustive RNG draw-site catalog, Task 0.1)
- `decoy-engine/src/decoy_engine/execution/native/_determinism_protocol.py` (new: the umbrella protocol facade with per-family semantics)
- `decoy-engine/src/decoy_engine/execution/native/_capabilities.py` (new: static strategy capabilities)
- `decoy-engine/src/decoy_engine/execution/native/_requirements.py` (new: schema/config-resolved node requirements)
- `decoy-engine/src/decoy_engine/execution/native/_plan.py` (new: `NativeExecutionPlan` + compiler + public compatibility query)
- `decoy-engine/src/decoy_engine/execution/native/_crypto_ext.py` (new: keyed-crypto + FPE extension CONTRACT + pure-Python references)
- `decoy-engine/src/decoy_engine/execution/native/_provider_class.py` (new: provider execution classification)
- `decoy-engine/tests/parity/native/_fixtures.py`, `test_parity_matrix.py` (new)
- `decoy-engine/tests/native/test_*.py` (new)
- Modify `decoy-engine/src/decoy_engine/execution/_runner.py` (attach requirements to `WorkNode` without changing execution)

### Task 0.1: Complete RNG draw-site inventory (COMPLETE)

The determinism rework CANNOT be designed without knowing every place randomness is drawn today. This task produces the exhaustive catalog Codex requires before any protocol is defined.

**Files:**
- Create: `decoy-engine/docs/native/draw-site-inventory.md`
- Test: `decoy-engine/tests/native/test_draw_site_inventory_coverage.py`

**Interfaces:**
- Produces: a catalog document plus a machine-checkable `DRAW_SITES` list (one entry per site) in `execution/native/_determinism_protocol.py`. Each entry records: `draw_site_id` (the stable per-site key Task 0.3 versions by, e.g. `mask.shuffle`, `mask.faker`), `family` (one of `numpy_pcg64`, `python_mt19937`, `per_row_reseed`, `source_keyed_hmac`, `per_group_stream`, `faker_seed_instance`, `gen_derive_context`), `call_site` (module:line), `entropy_root` (`mask_key` | `generation_seed`), `seed_derivation` (the EXACT expression seeding the RNG today, e.g. `derive(mask_key, namespace, column)[:8]`), `api_operation` + `call_shape` (e.g. `permutation(n)` whole-column, `row_int(family,i)` per-row), `consumes_variable_draws` (bool), `identity` (`row_index` | `source_value` | `group_key` | `global_row_number`), `null_draw_behavior` (does a null row still consume a draw), `partitionable` (bool: can partitioned execution reproduce it), and `config_fingerprint_source` + `provider_version`.

- [x] **Step 1: Write the failing coverage test** asserting `DRAW_SITES` includes at least the known families and that every strategy/generation kind that draws randomness maps to at least one entry.

```python
# tests/native/test_draw_site_inventory_coverage.py
from decoy_engine.execution.native._determinism_protocol import DRAW_SITES

REQUIRED_FAMILIES = {
    "numpy_pcg64", "python_mt19937", "per_row_reseed",
    "source_keyed_hmac", "per_group_stream", "faker_seed_instance",
    "gen_derive_context",
}

def test_all_known_families_catalogued():
    assert REQUIRED_FAMILIES <= {s.family for s in DRAW_SITES}

def test_shuffle_and_statistical_and_faker_present():
    sites = {s.call_site.split(":")[0].split("/")[-1] for s in DRAW_SITES}
    assert "_shuffle.py" in sites          # numpy permutation
    assert "_sample.py" in sites           # statistical per-row reseed
    assert "synthesize.py" in sites        # sequential random.Random + Faker
```

- [x] **Step 2: Run to verify it fails** (module/list missing). Expected: FAIL.
- [x] **Step 3: Enumerate every draw site** by reading the RNG-bearing code: `execution/_strategies/_shuffle.py` (whole-column NumPy permutation), `generation/statistical/_sample.py` (per-row `random.Random` reseed), `generation/synthesize.py:~415` (sequential `random.Random` + Faker), `generators/derivation.py` (`GenDeriveContext`, `GEN_FAMILIES`, per-family disjoint keys), `transforms/formula.py` (single sequential `random.Random` exposing `randint`/`choice`/`random`), `transforms/grouped_series.py` (per-group RNG stream for `monotone_walk`), `transforms/windowed_date.py` (per-row index-seeded NumPy), the source-keyed HMAC path (`kernel/_scalar.py` via `determinism.derive`), and the Faker pool builder (`generation/pool`). Write each into the catalog doc AND the `DRAW_SITES` list.
- [x] **Step 4: Run to verify passing.** Expected: PASS.
- [x] **Step 5: Cross-check against the config surface** by asserting each masking strategy and generation kind in the live registries (not a copied constant) resolves to a `DRAW_SITES` entry or is explicitly recorded as deterministic-no-draw.
- [x] **Step 6: Commit.** `git commit -m "docs(native): complete RNG draw-site inventory + machine-checkable catalog"`

### Task 0.2: Native planning boundary (COMPLETE)

Replaces the earlier single `locality` enum (which conflated orthogonal properties: hashing is BOTH keyed AND row-local). Splits static strategy capabilities from schema/config-resolved node requirements, and covers scalar, composite, grouped, generation, and provider nodes.

**Files:**
- Create: `decoy-engine/src/decoy_engine/execution/native/_capabilities.py`
- Create: `decoy-engine/src/decoy_engine/execution/native/_requirements.py`
- Test: `decoy-engine/tests/native/test_capabilities.py`, `test_requirements.py`

**Interfaces:**
- Produces:
  - `StrategyCapabilities` frozen dataclass with ORTHOGONAL boolean/enum fields: `is_row_local: bool`, `is_keyed: bool`, `is_order_sensitive: bool`, `is_global: bool`, `needs_global_row_identity: bool`, `output_type_is_static: bool`, `draw_family: str | None` (from Task 0.1), `key_source: Literal["mask_key","generation_seed",None]`, PLUS the machine-classifiable diagnostics/quality fields Phase 1 eligibility needs: `row_error_modes: tuple[str,...]` (empty = a strategy that provably cannot emit row errors), `warning_codes: tuple[str,...]` (every warning code the strategy can emit; empty = zero-warning), `quality_obligations: tuple[str,...]` (class-A quality checks the strategy's output requires; empty = none), `quarantine_required: bool`. `capabilities_for(strategy: str) -> StrategyCapabilities`.
  - `NodeRequirements` frozen dataclass (schema/config-resolved, per WorkNode): `required_input_columns: tuple[str,...]`, `output_arrow_schema: pa.Schema | None` (None = indeterminate, excluded from native route), `lowering_id: str`, `required_prepasses: tuple[str,...]`, `required_state_tables: tuple[str,...]`, `diagnostic_reducers: tuple[str,...]` (per-code warning/error globalizers), `fallback_policy: Literal["native","python_only","reject_large"]`. `requirements_for(node, plan, profile) -> NodeRequirements`.
  - Node-kind coverage: scalar, `composite_fk_group`, grouped, generation, and provider nodes each get a capabilities/requirements mapping (composite/group nodes carry placeholder strategy names today, so the mapping keys on `node.kind` + resolved strategy, not the placeholder string alone).

- [x] **Step 1: Write failing tests** for the orthogonality Codex requires and for node-kind coverage.

```python
# tests/native/test_capabilities.py
from decoy_engine.execution.native._capabilities import capabilities_for

def test_hash_is_both_keyed_and_row_local():
    c = capabilities_for("hash")
    assert c.is_keyed and c.is_row_local
    assert c.key_source == "mask_key"

def test_shuffle_is_global_and_order_sensitive():
    c = capabilities_for("shuffle")
    assert c.is_global and c.is_order_sensitive
    assert c.needs_global_row_identity

def test_formula_output_type_not_static():
    assert capabilities_for("formula").output_type_is_static is False

def test_zero_error_zero_warning_set_is_machine_classifiable():
    for s in ("hash", "redact", "truncate", "passthrough"):
        c = capabilities_for(s)
        assert c.row_error_modes == ()      # provably cannot emit row errors
        assert c.warning_codes == ()        # zero-warning
        assert c.quality_obligations == ()  # no class-A obligation
    # A strategy known to emit row errors is flagged, not blank.
    assert capabilities_for("bucketize").row_error_modes != ()
```

- [x] **Step 2: Run to verify it fails.**
- [x] **Step 3: Implement `_capabilities.py` and `_requirements.py`** from the spec gap table and Task 0.1. Static capabilities are strategy-only; resolved requirements read the compiled node + profile (e.g. `date_shift` with explicit `date_format` resolves `output_type_is_static=True` and no prepass; without it, a `format_detect` prepass and `is_global=True`). Populate `row_error_modes` / `warning_codes` / `quality_obligations` from each strategy's actual error/warning surface (read the strategy handlers), so "error-capable" and "quality-sensitive" become field reads, not a hardcoded list.
- [x] **Step 4: Run to verify passing.**
- [x] **Step 5: Totality test against LIVE registries.** Assert `capabilities_for` is total over every strategy the live masking registry and generation registry expose (import and enumerate them, do not copy the out-of-core constant set), failing loudly on any unclassified strategy, AND that every strategy's `row_error_modes`/`warning_codes` is populated (never a default-blank for a strategy that actually can error/warn).
- [x] **Step 6: Commit.** `git commit -m "feat(native): orthogonal strategy capabilities + resolved node requirements"`

### Task 0.2b: NativeExecutionPlan compiler + public compatibility query (COMPLETE)

The architecture names `NativeExecutionPlan`; this task gives it a compiler and the public compatibility query the platform consults (replacing the drift-prone copied constant). It also attaches requirements to `WorkNode` inertly.

**Files:**
- Create: `decoy-engine/src/decoy_engine/execution/native/_plan.py`
- Modify: `decoy-engine/src/decoy_engine/execution/_runner.py` (attach `NodeRequirements` to `WorkNode`, read-only)
- Test: `decoy-engine/tests/native/test_native_plan.py`

**Interfaces:**
- Consumes: `capabilities_for`, `requirements_for`, the existing `compile_plan` + work compilation.
- Produces:
  - `compile_native_plan(config, profile, *, engine_version) -> NativeExecutionPlan` with per-node requirements, resolved input projections, per-output schema, and per-node `fallback_policy`.
  - `native_route_eligibility(config, *, table) -> NativeEligibility` (accepted | list of coded rejections) that the platform's `classify_streaming_eligibility` will call instead of hardcoding strategy sets. Total, drift-sentried, tested against live registries.
  - `WorkNode.requirements: NodeRequirements` populated at compile time; nothing routes on it yet.

- [x] **Step 1: Write failing tests**: a mask config compiles to a `NativeExecutionPlan` whose nodes carry resolved requirements; `native_route_eligibility` rejects a `formula` column with `output_type_indeterminate`; a hash-only config is accepted.
- [x] **Step 2: Run to verify it fails.**
- [x] **Step 3: Implement the compiler + eligibility query.** Attach requirements to `WorkNode` without branching on them (byte-identical behavior).
- [x] **Step 4: Run existing `_runner` tests** to prove zero behavior change. Expected: PASS, no goldens move.
- [x] **Step 5: Commit.** `git commit -m "feat(native): NativeExecutionPlan compiler + public native-route eligibility query"`

### Task 0.3: The umbrella determinism protocol (COMPLETE)

This is the blocking task. A single HMAC-counter generator CANNOT reproduce the existing sequences, but neither is per-FAMILY versioning sufficient: the SAME family used at DIFFERENT draw sites seeds distinctly and consumes draws differently (masking shuffle seeds from `derive(mask_key, namespace, column)` and calls whole-column `rng.permutation(n)` at `_strategies/_shuffle.py:49-56`; synthetic generation reseeds Faker per global row at `synthesize.py:490`; a masking Faker pool is built once under `seed_instance`). So the protocol versions semantics by `draw_site_id` WITHIN each family, and each draw site preserves its EXACT existing seed derivation, API operation, null-consumption rule, call shape, and provider version. It builds on the existing `GenDeriveContext` / `GEN_FAMILIES` model (`generators/derivation.py`), not a replacement. Task 0.1's inventory is the authority on the draw-site list and each site's required identity; this task implements one provider per catalogued draw site and the goldens gate proves each reproduces exactly.

**Global operations are explicitly non-partitionable in Phase 1.** Whole-column `rng.permutation(n)` drives shuffle. A per-group sequential stream drives `grouped_series` `monotone_walk`. Local draws or per-row substreams cannot reproduce these sequences. The protocol marks these sites `partitionable=False`. They stay on the full-frame oracle. An activated Part 2 Phase 4 slice must supply an exact external algorithm. Shuffle requires an external sort with a deterministic key. Grouped streams require a durable per-group ordinal. The admitted set for Phase 1 contains none of these sites.

**Three distinct C1 Faker draw sites, not one.**

- `gen.pool_build_faker` builds a bounded value pool once under `seed_instance`.
- `gen.pool_deterministic` selects a pool index for each row with source-keyed identity.
- `mask.faker` is the masking strategy draw site that uses the deterministic pool selection.

These are separate `draw_site_id` contracts. Part 1 Phase 3 targets their exact C1 masking use. Synthetic per-row Faker remains separate and moves to Part 2.

**Files:**
- Modify: `decoy-engine/src/decoy_engine/execution/native/_determinism_protocol.py`
- Test: `decoy-engine/tests/native/test_determinism_protocol.py`, `decoy-engine/tests/native/test_determinism_goldens.py`

**Interfaces:**
- Consumes: `decoy_engine.determinism.derive` / `DeriveContext` / `SEED_PROTOCOL_VERSION`; `generators/derivation.GenDeriveContext`, `GEN_FAMILIES`; NumPy `Generator(PCG64(...))`; Python `random.Random`; Faker `seed_instance`.
- Produces one `DrawSiteProvider` per catalogued `draw_site_id` (from Task 0.1), each a pure function of its site's declared identity tuple `(entropy_root, strategy+config fingerprint, table/group/row identity, draw ordinal, variable-draw algorithm, provider version)`, and each carrying `partitionable: bool` and its exact existing seed derivation:
  - `mask.shuffle` (family `numpy_pcg64`, `partitionable=False`): seed EXACTLY `int.from_bytes(derive(mask_key, namespace, column.encode())[:8], "big")`, then `np.random.default_rng(seed).permutation(len(non_null))`. It stays full-frame unless Part 2 activates its Phase 4 dependency slice.
  - `mask.source_keyed_hmac` (`partitionable=True`): `derive(mask_key, namespace, canonicalize(value))`, already partition-independent, reused verbatim.
  - `gen.pool_build_faker` (family `faker_seed_instance`, `partitionable=True`): builds the bounded Faker pool from its derived pool seed.
  - `gen.pool_deterministic` (family `source_keyed_hmac`, `partitionable=True`): selects from the pool by source-keyed identity.
  - `mask.faker` (family `source_keyed_hmac`, `partitionable=True`): runs deterministic masking pool selection with the mask key.
  - `gen.grouped_series_walk` (family `per_group_stream`, `partitionable=False`): per-group sequential RNG. It stays full-frame unless Part 2 Phase 4 supplies a durable per-group ordinal.
  - Synthetic per-row Faker uses its separate generation draw site and stays unchanged until Part 2.
  - `gen.statistical_per_row` (family `per_row_reseed`, `partitionable=True`): reseed per row from a stable row identity; partition-independent by construction.
  - `unit_float_from_bits53(raw_u64) -> float`: `(raw_u64 >> 11) / 2**53`, matching NumPy's own `random()` construction, always in `[0,1)` (NEVER uint64/2**64).
  - `entropy_root` is explicit per site: masking uses the resolved `mask_key`; generation uses the generation seed. Never conflated.

- [x] **Step 1: Write the range test** proving the float fix, extracting from a FULL 64-bit value (not an already-extracted 53-bit input).

```python
# tests/native/test_determinism_protocol.py
from decoy_engine.execution.native._determinism_protocol import unit_float_from_bits53

def test_unit_float_from_full_u64_never_reaches_one():
    # Extract from an all-ones 64-bit value: (2**64-1 >> 11) / 2**53 must stay < 1.0.
    assert unit_float_from_bits53((1 << 64) - 1) < 1.0
    assert unit_float_from_bits53(0) == 0.0
    # Spot-check the shift is the upper 53 bits, matching numpy random().
    assert unit_float_from_bits53(1 << 11) == (1 << 11 >> 11) / 2**53
```

- [x] **Step 2: Write per-DRAW-SITE emulation tests** asserting each site's provider reproduces the EXACT sequence the current code produces for a fixed seed at THAT site: `mask.shuffle` equals `np.random.default_rng(int.from_bytes(derive(mask_key, ns, col)[:8],"big")).permutation(n)`; `gen.pool_build_faker`, `gen.pool_deterministic`, and `mask.faker` equal their current pool-build and source-keyed selection paths; `gen.statistical_per_row` equals the current per-row reseed. Do NOT assert a global site is representable as concatenated local draws.
- [x] **Step 3: Run to verify they fail.**
- [x] **Step 4: Implement one provider per catalogued draw site**, preserving each site's exact seed derivation, API operation, null consumption, and call shape; mark global sites `partitionable=False`; cite the NumPy PCG64 / CPython MT19937 references. Do NOT implement a per-row substream for a global site.
- [x] **Step 5: Run to verify passing.**
- [x] **Step 6: Write OPERATION-SPECIFIC partition tests.** For `partitionable=True` sites only, assert a whole-column draw equals the concatenation of fresh-process partitioned draws over different batch boundaries. For `partitionable=False` sites, assert the provider REFUSES a partitioned request (raises a coded error), proving Phase 1 cannot route them.
- [x] **Step 7: Write the subprocess-stability test** (mirror `determinism/_derive.py:311`): draws in a fresh subprocess equal in-process draws.
- [x] **Step 8: GOLDEN GATE (blocks the program).** Run the existing seeded golden-vector suites for masking and generation THROUGH each draw site's provider and assert every current golden fingerprint reproduces exactly. Only when this passes is the protocol frozen and each draw site's version locked. Record the freeze + the draw-site version table in the inventory doc.
- [x] **Step 9: Commit.** `git commit -m "feat(native): per-draw-site determinism protocol (globals non-partitionable, goldens pass)"`

### Task 0.4: Keyed-crypto + FPE extension CONTRACT with pure-Python references (COMPLETE)

The contract names the resolved `mask_key` and separates it from the generation seed. It defines the complete Arrow type and encoding contract.

The FPE contract includes:

- character sets and separator preservation
- Luhn and checksum behavior
- join-group tweaks
- failure mapping and warnings
- decryption
- mixed object-column policy

Pure-Python hash and FPE references provide known-answer vectors. The contract also specifies ownership, threading, packaging, and the Arrow C Data Interface.

Part 1 Phase 2 builds `KeyedDerivationKernel`. FPE compilation moves to Part 2.

**Files:**
- Create: `decoy-engine/src/decoy_engine/execution/native/_crypto_ext.py`
- Test: `decoy-engine/tests/native/test_crypto_ext_contract.py`

**Interfaces:**
- Consumes: `decoy_engine.determinism.derive`, `kernel/_canonicalize.canonicalize_derive_source`, the current FPE strategy (`execution/_strategies/_fpe.py`: one key per `(mask_key, namespace)` = `derive(mask_key, ns, b"fpe-key/v1")`, per-column tweak = column name UTF-8, Luhn body-permute-then-append, join-group tweak, `SEED_PROTOCOL_VERSION` v6).
- Produces:
  - `KeyedDerivationKernel.derive_batch(values: pa.Array | list[Any], *, mask_key: bytes, namespace: str, truncate: int | None) -> pa.Array` (mask_key NAMED, not "seed"). The input is a real `pa.Array` for a typed column or a Python `list` for mixed-object fallback. The contract accepts the list because a mixed-object column has no single Arrow type. The compiled Part 1 extension accepts only `pa.Array` through the Arrow C Data Interface. It rejects the list with `mixed_object_not_native`. The pure-Python reference handles both forms. The contract specifies and tests null, string, bytes, and numeric encoding.
  - `FpeKernel.encrypt_batch(values, *, mask_key, namespace, tweak_column, config: FpeConfig) -> FpeBatchResult` where `FpeConfig` carries character set, separator preservation, checksum/Luhn mode, and join-group tweak, and `FpeBatchResult` carries the output array PLUS structured per-row errors and warnings. `FpeKernel.decrypt_batch(...)` for the unmask round trip.
  - `reference_keyed_derivation()` and `reference_fpe()` pure-Python kernels that reproduce the current `kernel/_scalar.hash_array` and `_strategies/_fpe` outputs exactly.
  - `CRYPTO_EXT_ABI` doc block: the compiled extension's ownership, thread model, packaging, and Arrow C Data Interface expectations, plus the fail-before-output contract (a missing/incompatible extension raises BEFORE any staging).

- [x] **Step 1: Write failing contract tests** for hash parity, FPE parity, FPE decrypt round trip, missing-key fail-closed, and null/mixed-object encoding.

```python
# tests/native/test_crypto_ext_contract.py
def test_reference_hash_matches_scalar(vals):
    got = reference_keyed_derivation().derive_batch(vals, mask_key=MK, namespace="ns", truncate=None)
    assert got.to_pylist() == hash_array(vals, seed=MK, namespace="ns").to_pylist()

def test_reference_fpe_roundtrip(pan_vals):
    kern = reference_fpe()
    enc = kern.encrypt_batch(pan_vals, mask_key=MK, namespace="ns", tweak_column="pan", config=LUHN_CFG)
    dec = kern.decrypt_batch(enc.values, mask_key=MK, namespace="ns", tweak_column="pan", config=LUHN_CFG)
    assert dec.to_pylist() == pan_vals.to_pylist()

def test_missing_key_fails_closed(vals):
    with pytest.raises(MaskKeyRequiredError):
        reference_fpe().encrypt_batch(vals, mask_key=None, namespace="ns", tweak_column="c", config=LUHN_CFG)
```

- [x] **Step 2: Run to verify they fail.**
- [x] **Step 3: Implement the Protocols + both pure-Python references + KAT vectors.** Cite the current FPE model (single key per `(mask_key, namespace)`, per-column tweak, Luhn handling) so the contract matches shipped behavior, not a new scheme.
- [x] **Step 4: Run to verify passing.**
- [x] **Step 5: Commit.** `git commit -m "feat(native): keyed-crypto + FPE extension contract, pure-Python references, KAT vectors"`

### Task 0.5: Provider execution classification (COMPLETE)

Classifies every provider as `pool_native` (bounded pool built once, selected natively), `python_only` (executes only to build a bounded pool, or a labeled Python fallback for small jobs), or `reject_large` (arbitrary custom callables that cannot be honestly native and are rejected on large jobs). This exists from Phase 0 so eligibility and fallback decisions are honest from the start.

**Files:**
- Create: `decoy-engine/src/decoy_engine/execution/native/_provider_class.py`
- Test: `decoy-engine/tests/native/test_provider_class.py`

**Interfaces:**
- Produces: `classify_provider(provider_id, provider_config) -> Literal["pool_native","python_only","reject_large"]`, total over the live provider registry. Enumerate the registry and do not hardcode a count. Unknown/custom Python callables classify `reject_large` (or `python_only` for a small-job threshold), never silently native.

- [x] **Step 1: Write failing tests**: enumerate the live default bindings and assert each classifies (poolable Faker -> `pool_native`, nonpoolable Faker -> `python_only`, an arbitrary custom callable -> `reject_large`).
- [x] **Step 2: Run to verify it fails.**
- [x] **Step 3: Implement classification** from the live registry, not a copied list.
- [x] **Step 4: Totality test** against the live provider registry.
- [x] **Step 5: Commit.** `git commit -m "feat(native): provider execution classification (pool_native | python_only | reject_large)"`

### Task 0.6: Parity harness and golden matrix fixtures (COMPLETE)

The oracle is PINNED. Logical parity is EXACT, allowing only enumerated physical differences.

**Files:**
- Create: `decoy-engine/tests/parity/native/_fixtures.py`, `test_parity_matrix.py`
- Reference (read): `decoy-engine/tests/parity/SEMANTIC_DIFFERENCES.md`

**Interfaces:**
- Produces: `run_oracle(config, sources) -> LogicalResult` (forces `substrate="pandas"`, `execution_mode="full_frame"`, `auto_chunk=False`); `assert_logical_parity(candidate, oracle, *, allowed_physical_diffs)` comparing values, row order, nulls, diagnostics (warnings + row errors), and LOGICAL schema exactly, allowing ONLY the enumerated physical differences passed in (each recorded by strategy/backend, defaulting to the specific null-typed normalization `concat_masked_chunks` already performs, NOT generic Arrow widening); `STRATEGY_MATRIX` / `PROVIDER_MATRIX` generated from the LIVE registries.

- [x] **Step 1: Write the harness self-test** asserting `assert_logical_parity` passes on an exact match and FAILS on a differing value, reordered row, null-vs-value swap, missing warning, missing row error, and an un-enumerated Arrow type difference.
- [x] **Step 2: Run to verify it fails.**
- [x] **Step 3: Implement `_fixtures.py`** with the pinned oracle and the enumerated-difference allow-list (per strategy/backend, each requiring an explicit migration decision to add).
- [x] **Step 4: Run to verify passing.**
- [x] **Step 5: Generate the matrix from LIVE registries** (one case per strategy, one per provider binding, with null/dtype/seed-mode variants). Mark not-yet-migrated cases `xfail(strict=False)` so the matrix is complete now and flips to PASS as each phase lands.
- [x] **Step 6: Commit.** `git commit -m "test(parity): pinned-oracle parity harness + live-registry golden matrix"`

### Phase 0 acceptance criteria

The Phase 0 implementation met these criteria. The final review reported no blocking or important findings.

- The RNG draw-site inventory is complete and coverage-tested against live registries.
- The umbrella determinism protocol reproduces every existing seeded golden vector (Task 0.3 Step 8) and passes partition-invariance + subprocess-stability; unit floats never reach 1.0. The protocol is frozen. **This is the program-gating criterion.**
- The native planning boundary uses orthogonal capabilities + resolved requirements, covers all node kinds, and is total against live registries; `NativeExecutionPlan` has a working compiler and public eligibility query.
- The crypto contract has pure-Python references for BOTH hash and FPE, KAT vectors, decrypt round trips, missing-key fail-closed, and a specified compiled ABI.
- Every provider classifies `pool_native | python_only | reject_large`.
- The parity harness runs the live-registry matrix against the pinned pandas oracle with an enumerated physical-difference allow-list.
- Zero existing golden fingerprints move. Both repos CI green.

---

## Part C: Phase 1 (First High-Value Slice) - Detailed Task-by-Task

**Status: active and paused after Task 1.1.** Task 1.1 is implemented at `b84054b4`. Its review is deferred to the full Phase 1 gate. Tasks 1.2 through 1.5 wait for the admission classifier to land.

**The slice, deliberately NARROW:** make large, chunk-compatible masking jobs stream by DEFAULT, including MULTIPLE independent mask tables (processed and staged one table at a time, published table-atomically with an honest manifest), keeping pandas inside each batch, FAIL-CLOSED on row errors, and EXCLUDING every job that needs semantics Phase 1 does not implement. This captures the memory win for the verified-safe surface without over-claiming atomicity, quarantine, vault, or quality parity.

### What exists today (verified anchors)

- `classify_streaming_eligibility` (`api/jobs/streams.py:454`) returns a single `StreamingRoute(table=str)` or rejection reasons; gate 2 rejects `len(tables) != 1` (`streams.py:508`). Streaming is default OFF (`api/config.py:167`). The gate already excludes bucketize, top_code, predicates, composites, formatless date_shift, transforms, relationships, validators (`streams.py:516-556`).
- `_run_v2_pipeline_streaming` (`api/jobs/v2_runner.py:299`) constructs AND commits its OWN writer (`v2_runner.py:348,363`). `FileTargetWriter.commit()` immediately `os.replace`s the canonical target (`streams.py:257`); cloud writers publish during commit. There is no externally-owned stage-only writer today.
- `run_mask_pipeline_chunked` (`execution/_chunked.py:267`) FAILS CLOSED on any per-row error (`RowErrorsFailedError`, `_chunked.py:436`). Warning row indices are chunk-local; `aggregate_chunk_warnings` (`_chunked.py:571`) dedups by equality. top_code embeds indices inside arbitrary detail keys (so warnings cannot be globalized generically).
- Full-frame quarantine removes affected rows and writes raw content to a sidecar (`quarantine.py:151`); bucketize leaves the unsafe original value on failure (`_strategies/_bucketize.py:110`).
- `VaultWriter` (`vault.py:196`) holds every DISTINCT `(namespace, masked, source)` triple in a Python `set` in memory: bounded by distinct-triple cardinality, NOT row count, so a high-cardinality vault job is unbounded.
- The streaming route skips the class-A quality decider (`v2_orchestrator.py:75`) and `build_streaming_node_runs` (`v2_node_runs.py:264`) discards per-chunk warning detail.
- The C1 recipe (the measured 12.7x case) has Faker columns WITHOUT the deterministic/namespace/pool config chunk compatibility requires. So C1 does NOT qualify for Phase 1. Its masking path qualifies in Part 1 Phase 3.

### Phase 1 eligibility matrix (BINDING)

A job streams by default in Phase 1 ONLY if ALL hold. Any miss routes to the unchanged full-frame path with reasons recorded on the manifest.

| Dimension | Phase 1 admits | Phase 1 excludes (reason) |
|---|---|---|
| Tables | N independent mask tables | any table failing a per-table gate (all-or-nothing classification) |
| Relationships | none | any `relationships` (FK streaming is deferred to Part 2 Phase 4) |
| Validators | none | any `validators` (validators can rematerialize outputs) |
| Transforms | none | any `transforms` (not per-chunk-safe) |
| Strategies | an EXPLICIT zero-error, zero-warning set: `hash`, `redact`, `truncate`, `passthrough` (each has `row_error_modes == ()` and `warning_codes == ()` and `quality_obligations == ()` in `StrategyCapabilities`) | every other strategy. Widen only through an active slice after its diagnostics contract passes parity. |
| Row errors | only strategies with `row_error_modes == ()` | any strategy with a non-empty `row_error_modes` (mechanically read from capabilities, not a hardcoded list) -> excluded (fail-closed retained; quarantine NOT implemented in Phase 1) |
| Warnings | only strategies with `warning_codes == ()` | any strategy that can emit a warning -> excluded (so Phase 1 needs NO warning globalizers at all; see Task 1.1) |
| Vault | none | any `vault: true` column (VaultWriter is in-memory, cardinality-unbounded, and deferred to Part 2) |
| Quality | only strategies with `quality_obligations == ()` | any strategy with a class-A quality obligation (mechanically read) -> excluded until a later phase preserves the decider |
| Generation | none | any `generate_columns` (streaming generation is deferred to Part 2) |
| Providers | `pool_native` providers already satisfying chunk compatibility | `python_only` / `reject_large` providers on large jobs |
| Size | at or above `streaming_min_input_mb` | below the floor (full-frame is cheaper) |

The pure predicate `phase1_eligibility(config, profile_metadata) -> StreamingPlan | list[str]` enforces this matrix. It reads `StrategyCapabilities` fields, not prose.

The fields are `row_error_modes`, `warning_codes`, `quality_obligations`, and `quarantine_required`. The explicit admitted set is `{hash, redact, truncate, passthrough}`.

An active slice can widen this set after its diagnostics contract passes parity. Every current conservative exclusion remains.

### File structure for Phase 1

- Modify `decoy-platform/api/jobs/streams.py` (multi-table `StreamingPlan`; extend classification; add vault/quality/row-error exclusions; consult the engine `native_route_eligibility` from Task 0.2b)
- Modify `decoy-platform/api/config.py` (flip default after qualification; NO new per-table budget setting)
- Create `decoy-platform/api/jobs/v2_stream_coordinator.py` (<600 LOC: multi-table coordinator with table-atomic publish + manifest)
- Modify `decoy-platform/api/jobs/streams.py` writer family (add a stage-only `prepare()/publish()/abort()` split; keep `commit()` for the single-table back-compat path)
- Modify `decoy-platform/api/jobs/v2_runner.py` (`_run_v2_pipeline_streaming` accepts an externally-owned stage-only writer + a global row offset; warning-index globalization via code-specific reducers)
- Modify `decoy-platform/api/jobs/v2_orchestrator.py` (route a `StreamingPlan` through the coordinator; admit ONCE for the max reachable route)
- Modify `decoy-engine/src/decoy_engine/execution/_chunked.py` (accept an optional `base_row_offset`; KEEP fail-closed row-error default unchanged)
- Tests: coordinator publish/abort, multi-table classification, exclusions, memory-bound (external), partition-invariance, admitted-set-is-diagnostics-free.

### Task 1.1: base_row_offset plumbing + assert the admitted set is diagnostics-free (IMPLEMENTED)

The Phase 1 admitted strategy set is zero-error and zero-warning (`{hash, redact, truncate, passthrough}`). Thus, Phase 1 needs no warning globalizers or quarantine. An admitted job produces no warnings or row errors. This task threads a `base_row_offset` and adds a defensive assertion that the admitted set stays diagnostics-free. The implementation is complete at `b84054b4`. The detailed checklist remains as the implementation record.

**Files:**
- Modify: `decoy-engine/src/decoy_engine/execution/_chunked.py:398-447`
- Test: `decoy-engine/tests/execution/test_chunked_admitted_set.py`

**Interfaces:**
- Produces:
  - `run_mask_pipeline_chunked(..., base_row_offset: int = 0)`; the running offset advances by each chunk's `num_rows`. Inert for the admitted set (no diagnostics carry indices), present for later phases.
  - The fail-closed row-error behavior is UNCHANGED (the `if result.row_errors:` raise stays; defense in depth).

- [x] **Step 1: Write failing tests**: a two-chunk `hash`/`redact`/`truncate`/`passthrough` job produces zero warnings and zero row errors across chunks; `base_row_offset` is accepted and advances correctly; and a job carrying a warning-emitting strategy is REJECTED by the Task 1.2 predicate before it reaches this runner (assert the eligibility gate, not a globalizer).
- [x] **Step 2: Run to verify it fails.**
- [x] **Step 3: Implement the offset plumbing** and the diagnostics-free assertion. No `WarningGlobalizer` in Phase 1.
- [x] **Step 4: Run to verify passing** and re-run existing `_chunked` tests (fail-closed default unchanged).
- [x] **Step 5: Commit.** `git commit -m "feat(chunked): base_row_offset plumbing + diagnostics-free admitted-set assertion"`

### Task 1.2: Pure, machine-checkable `phase1_eligibility` predicate (platform)

Implements the BINDING matrix as ONE pure function that reads `StrategyCapabilities` fields, so "error-capable" and "quality-sensitive" are field reads, not prose or a hardcoded list.

**Files:**
- Modify: `decoy-platform/api/jobs/streams.py:446-580`
- Test: `decoy-platform/tests/test_streaming_multitable.py`, `decoy-platform/tests/test_phase1_eligibility.py`

**Interfaces:**
- Consumes: the engine `native_route_eligibility` (Task 0.2b) and `capabilities_for` (Task 0.2), plus config-level checks (vault, relationships, validators, transforms, generation, size).
- Produces: `StreamingPlan(tables: tuple[str,...])`; `phase1_eligibility(config, profile_metadata) -> StreamingPlan | list[str]`, PURE and exhaustively tested against live registries. `classify_streaming_eligibility` delegates to it (keeping the single-table `StreamingRoute` return for back-compat). A table is admitted ONLY if every column's strategy has `row_error_modes == ()` AND `warning_codes == ()` AND `quality_obligations == ()` AND `quarantine_required is False`, and the table carries no `vault: true` column, no relationships/validators/transforms/generation, and is above the size floor. Any miss -> whole config rejected with coded reasons.

- [ ] **Step 1: Write failing tests** driven by capabilities, not hardcoded strategy names: two `{hash,redact,truncate,passthrough}` independent tables -> `StreamingPlan(("a","b"))`; a column whose strategy has non-empty `row_error_modes` (e.g. bucketize) -> `row_error_capable_excluded`; non-empty `warning_codes` (e.g. top_code) -> `warning_capable_excluded`; non-empty `quality_obligations` -> `quality_obligation_excluded`; `vault: true` -> `vault_not_supported_streaming`; relationships -> `relationships_not_supported`.
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement `phase1_eligibility`** reading capability fields; loop tables; aggregate coded rejections. Wire `classify_streaming_eligibility` to delegate.
- [ ] **Step 4: EXHAUSTIVE live-registry test.** Enumerate EVERY strategy in the live masking registry; assert `phase1_eligibility` admits exactly the zero-error/zero-warning/zero-quality set and rejects every other strategy with a coded reason. This proves the predicate cannot silently admit a diagnostics-capable strategy as the registry grows.
- [ ] **Step 5: Commit.** `git commit -m "feat(streams): pure capability-driven phase1_eligibility predicate (live-registry exhaustive)"`

### Task 1.3: Stage-only writer split + table-atomic multi-table coordinator (platform)

Refactors the writer to separate `write` / `prepare` / `publish` / `abort`, and the coordinator publishes each table ATOMICALLY (one `os.replace` / one object-store finalize per table). It does NOT claim cross-target all-or-nothing atomicity (repeated `os.replace` / object copies are not two-phase atomic). The failure contract is resolved to ONE behavior: on any table failure the JOB FAILS (raises), and a typed `PartialPublicationError` carries a serializable per-table status manifest that is the DURABLE carrier of what published; a happy path returns the result. Each successful table publish also writes a durable publication record (a `JobTablePublication` row) so the published set survives the exception AND the DB rollback of the job transaction.

**Files:**
- Modify: `decoy-platform/api/jobs/streams.py` (writer family: add `prepare()` returning a staged handle and `publish(handle)`; keep `commit()` delegating to prepare+publish for the single-table path)
- Create: `decoy-platform/api/jobs/v2_stream_coordinator.py`
- Create: `decoy-platform/api/jobs/_partial_publication.py` (typed `PartialPublicationError` + `PublicationManifest` serialization; kept out of the coordinator to hold the LOC cap)
- Modify: `decoy-platform/api/models.py` (add a durable `JobTablePublication` record: job_id, table, status, target metadata, committed in its OWN transaction per publish so it survives a later job rollback)
- Test: `decoy-platform/tests/test_stream_coordinator.py`, `decoy-platform/tests/test_partial_publication_durability.py`

**Interfaces:**
- Consumes: the stage-only writer, `_run_v2_pipeline_streaming` (Task 1.4), `StreamingPlan`, the admission lease + chosen route passed IN (Task 1.5), a spool-disk preflight done at claim time (Task 1.5).
- Produces:
  - `run_v2_pipeline_streaming_multitable(...) -> StreamingJobResult` (happy path only) with `{table: metadata}` for all published tables.
  - `PartialPublicationError(published_manifest: PublicationManifest)` raised on ANY table failure. `PublicationManifest` is a serializable per-table status map (`published` | `staged_not_published` | `skipped`) with published targets' metadata. The in-flight table's stage is aborted; a pre-existing target is untouched; tables already published REMAIN published (honest, not rolled back). No result-AND-raise ambiguity: failure ALWAYS raises.
  - A `JobTablePublication` durable row committed per successful publish in its own transaction, so the orchestrator's failure handler (Task 1.5) can read the published set after the job transaction rolls back.

- [ ] **Step 1: Write failing tests** for the HONEST + DURABLE semantics.

```python
# tests/test_partial_publication_durability.py
def test_failure_raises_typed_error_with_manifest(coordinator_env):
    with pytest.raises(PartialPublicationError) as ei:
        run_v2_pipeline_streaming_multitable(job, db, cfg_b_explodes, StreamingPlan(("a","b")), route, lease)
    m = ei.value.published_manifest
    assert m["a"] == "published" and m["b"] == "staged_not_published"

def test_published_set_survives_job_rollback(coordinator_env):
    with pytest.raises(PartialPublicationError):
        run_v2_pipeline_streaming_multitable(job, db, cfg_b_explodes, StreamingPlan(("a","b")), route, lease)
    db.rollback()                                  # simulate the job-txn rollback
    rows = db.query(JobTablePublication).filter_by(job_id=job.id).all()
    assert {r.table: r.status for r in rows}["a"] == "published"

def test_preexisting_target_preserved_on_failure(coordinator_env_with_existing_b):
    with pytest.raises(PartialPublicationError):
        run_v2_pipeline_streaming_multitable(job, db, cfg_b_explodes, StreamingPlan(("a","b")), route, lease)
    assert target_bytes("b") == PREEXISTING_B_BYTES
```

- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement the writer split + coordinator + durable record.** Spool cleanup in a `finally`. Table-atomic publish; commit each `JobTablePublication` in its own transaction. Raise `PartialPublicationError` on any failure. Coordinator and helpers under 600 LOC each.
- [ ] **Step 4: Run to verify passing.**
- [ ] **Step 5: Commit.** `git commit -m "feat(jobs): stage-only writer split + table-atomic coordinator + durable PartialPublicationError/JobTablePublication"`

### Task 1.4: Per-table streaming route accepts an external stage-only writer + global offset

**Files:**
- Modify: `decoy-platform/api/jobs/v2_runner.py:299-380`
- Test: `decoy-platform/tests/test_streaming_execution.py`

**Interfaces:**
- Consumes: `run_mask_pipeline_chunked(..., base_row_offset=)` (Task 1.1); an externally-owned stage-only writer (Task 1.3).
- Produces: `_run_v2_pipeline_streaming(..., writer)` no longer constructs/commits its own writer when one is passed (the coordinator owns publish); it stages via `writer.write_batch` and returns staged metadata + the running row count (the admitted set is zero-warning/zero-error, so there are no diagnostics to return). The single-table orchestrator path still passes a writer whose `commit()` publishes (back-compat).

- [ ] **Step 1: Write failing tests** asserting the route stages (does not publish) when given an external writer, and that `base_row_offset` advances across tables.
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Refactor** to accept the external writer; keep the self-owned path for the single-table back-compat case.
- [ ] **Step 4: Run to verify passing.**
- [ ] **Step 5: Commit.** `git commit -m "feat(jobs): streaming route accepts external stage-only writer + global warning offset"`

### Task 1.5: Claim-time route selection (in the admission layer), orchestration consumes route + lease, durable partial-publication handling, flip default after qualification

Route selection, the memory reservation, and the disk preflight all happen at CLAIM TIME inside the memory-safety plan's claim-time route classifier, BEFORE the single lease commits and BEFORE any cloud source is downloaded or the worker is dispatched. This is the correct lifecycle layer (the admission supervisor owns lease acquire/release). Orchestration, running inside the already-dispatched worker, must NOT acquire another lease; it receives the chosen route and the committed lease and executes them. This task is therefore split across a landing change in the admission plan and a consuming change in orchestration.

**Files:**
- Modify (LANDING DEPENDENCY, `decoy-platform/docs/plans/2026-08-26-admission-memory-safety.md`): the claim-time route classifier chooses streaming vs full-frame, computes the memory reservation for the chosen route, and does the disk preflight, all before committing the one lease. Documented here as the required extension; owned by the memory-safety plan.
- Modify: `decoy-platform/api/jobs/v2_orchestrator.py:168-270` (consume the passed-in route + lease; dispatch `StreamingPlan` to the coordinator; consume `PartialPublicationError` in the failure handler)
- Modify: `decoy-platform/api/config.py:167-169` (flip default after qualification; NO per-table budget setting)
- Test: `decoy-platform/tests/test_orchestrator_routes.py`, `decoy-platform/tests/test_claim_time_route.py`, `decoy-platform/tests/test_streaming_qualification.py`

**Interfaces:**
- Consumes: the chosen route + committed lease PASSED IN from claim time; `run_v2_pipeline_streaming_multitable`; the durable `JobTablePublication` records + `PartialPublicationError` (Task 1.3).
- Produces:
  - Orchestration dispatch on the ALREADY-CHOSEN route: `StreamingPlan` -> coordinator (no new lease acquired); full-frame otherwise. No streaming classification inside the worker beyond consuming the claim-time decision; no mid-stream fallback (a full-frame decision is made at claim time with its own pricing/re-admission, never as an in-worker fallback after streaming staging begins).
  - A disk-preflight formula (documented, applied at claim time): per FILESYSTEM, require `sum(cloud_source_staging_bytes) + max_concurrent_output_or_local_target_stage_bytes + copy_finalize_overhead + headroom`. Insufficient disk REJECTS or DEFERS the job before dispatch (never a late coordinator-level reroute after cloud download).
  - A failure handler that, on `PartialPublicationError`, reads the durable `JobTablePublication` set AFTER the job-transaction rollback and writes the per-table publication manifest into the canonical evidence manifest / job node schema (so a partially-published job is honestly recorded, not shown as producing nothing).

- [ ] **Step 1: Write failing tests**: orchestration consumes a claim-time `route=streaming` + lease and dispatches the coordinator WITHOUT acquiring a lease (assert no lease acquire call in the worker); a claim-time disk-insufficient job is rejected/deferred BEFORE dispatch (assert no cloud download occurred); on `PartialPublicationError` the evidence manifest records `a: published, b: staged_not_published` read from the durable rows after rollback.
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement** the orchestration consumer + failure handler; land the claim-time classifier extension in the admission plan (route + reservation + disk preflight before lease commit).
- [ ] **Step 4: Qualification gate (three explicit cases, tied to the phase that covers them).**
  - **Case A (hash-only single table):** exact seeded parity vs the pinned oracle + external peak-RSS within the FROZEN declared ceiling (the ceiling is fixed as a number before implementation, per T3).
  - **Case B (hash-only multi-table):** two+ independent tables, table-atomic publish + durable manifest verified, external peak-RSS within the same ceiling regardless of table count.
  - **Case C (exact measured C1):** Run the real C1 recipe. The recipe has non-conforming Faker columns. Make sure that `phase1_eligibility` excludes it. Make sure that the full-frame route records `faker_not_chunk_compatible`. Record the C1 memory work in Part 1 Phase 3. Do not claim that C1 qualifies here.
  Only after A and B pass, flip `streaming_execution_enabled` default to `True`; keep the env var as the rollback lever.
- [ ] **Step 5: Commit.** `git commit -m "feat(jobs): consume claim-time route+lease, durable partial-publication into evidence manifest; enable streaming after A/B qualification"`

### Phase 1 acceptance criteria

- A job with N independent mask tables whose every column is in the zero-error/zero-warning admitted set `{hash, redact, truncate, passthrough}` streams all N, one table resident, published table-atomically with a durable manifest.
- Every current conservative exclusion is retained; vault, class-A-quality-dependent, error-capable, and generate jobs are excluded with coded reasons.
- Exact seeded logical parity vs the pinned pandas oracle holds for every streamed table across at least three batch sizes and two table orders.
- External peak-RSS on the hash-only qualification workloads is within ONE declared ceiling at 1x/4x/16x input; row errors remain fail-closed.
- C1 is correctly EXCLUDED. Its masking path moves to Part 1 Phase 3, and the plan states this plainly.
- Admission acquires ONE lease for the max reachable route; spool disk is preflighted; no per-table budget is registered; no mid-stream fallback after staging.
- Both repos CI green; no full-frame golden fingerprints move.

---

## Part D: Part 1 Hot Path and Deferred Part 2

All later work uses the Phase 0 contracts and the Phase 1 coordinator. Part 1 has two narrow optimization slices. Part 2 has no active implementation commitment.

### Part 1 Phase 2: Native masking hot path

- Freeze a representative workload before implementation. Record its baseline, target, resource budget, warmup, repetitions, variance method, and tail-reporting method.
- Lower `passthrough`, `redact`, and `truncate` to Arrow or DuckDB expressions.
- Build the Rust `KeyedDerivationKernel` through the Arrow C Data Interface. Use it for keyed `hash`.
- Keep native FPE, `group_key`, and all other Phase 2 strategies out of this slice.
- Make sure that a missing or incompatible extension fails or reroutes before staging starts.
- Apply the Rust gate to packaging, ABI behavior, threading, KAT vectors, security review, and fail-before-output behavior.
- Require exact seeded parity for each migrated strategy. Measure end-to-end latency, strategy throughput, peak RSS, and spill use against the frozen baseline.

### Part 1 Phase 3: C1 masking vertical slice

- Freeze the exact C1 recipe and dataset tier before implementation.
- Record current end-to-end latency, hash throughput, Faker throughput, peak RSS, and spill use.
- Set a minimum speed target or an explicit non-regression limit.
- Build bounded pools only for the poolable Faker providers that C1 uses.
- Select pool values natively with the source-keyed masking identity from the determinism protocol.
- Add only the masking-pool cache and state that the C1 path uses.
- Give each state owner a fixed memory budget and a high-cardinality adversary. This includes warning state, error state, and each DuckDB-backed table.
- Exclude synthetic `generate_columns`, other `pool_native` families, vault, composite providers, custom/reference pools, and `joint_mask`.
- Enforce `reject_large` during claim-time admission. Permit the pandas oracle only below an explicit priced small-job limit.
- Require exact parity, seed stability, bounded state, and C1 fidelity within frozen tolerances.
- Run the benchmark harness and prod-sim C1 workload after this slice.
- Make sure that the C1 evidence proves execution on the intended streaming and native route.
- Treat oracle completion, admission rejection, and fallback as gate failures.

#### Phase 3 route-widening task

- Add one pure `phase3_c1_eligibility(config, profile_metadata)` layer above the Phase 1 predicate.
- Admit `mask.faker` only for the exact poolable provider IDs in the frozen C1 recipe.
- Resolve the Faker config before admission. Admit only the config shape that the C1 pool builder and native selector support.
- Consume `classify_provider` for every Faker column. Require the `pool_native` result.
- Permit exactly the named `pool_quality` obligation for an admitted C1 Faker column.
- Enforce `pool_quality` before publication with the frozen C1 fidelity tolerances.
- Reject every other quality obligation with a coded reason.
- Reject nonpoolable Faker, non-C1 providers, other provider families, `python_only`, and `reject_large` before staging.
- Run Phase 1 and Phase 3 eligibility against the unchanged frozen C1 recipe.
- Make sure that Phase 1 records `faker_not_chunk_compatible` for this recipe.
- Make sure that Phase 3 admits the same recipe before any source staging or native execution.
- Record the selected and executed Phase 3 route in the job evidence.

### Part 2 activation rule

Part 2 stays deferred. Activate only the smallest dependency-closed vertical slice after measured workload or customer evidence supports it.

Each activation record contains:

- the workload and owner decision
- the baseline and target
- the resource budget
- the expected benefit
- all safety and evidence dependencies

### Part 2: Remaining Phase 2 strategies and diagnostics

This scope includes explicit-format `date_shift`, `categorical`, `code_set`, `group_key`, FPE, `bucketize`, `top_code`, and quarantine.

### Part 2: Remaining Phase 3 state and provider work

This scope includes synthetic generation, other `pool_native` providers, vault, composite providers, custom/reference pools, and `joint_mask`.

### Part 2: Phase 4 global and relational work

This scope includes FK joins, shuffle, grouped operations, global aggregates, validators, transforms, and spill-based global algorithms.

### Held: Phase 5 hard tail

Phase 5 stays held. Formula, nested JSON, text/NER, and arbitrary Python providers remain on the pandas full-frame oracle.

The pandas oracle is a permanent route for unsupported work. This plan does not promise a full native cutover.

---

## Part E: Test and Evidence Strategy

Each active slice has a frozen correctness, performance, and resource gate. A green oracle run does not prove that the new route ran.

### T1. Exact seeded logical-value parity

Each migrated path must equal the pinned oracle (`substrate="pandas"`, `execution_mode="full_frame"`, `auto_chunk=False`). Compare values, row order, null placement, warnings, and row errors.

Byte-level file equality is not required. Permit only enumerated physical differences for a named strategy and backend. Statistical fidelity never replaces exact parity.

### T2. Partition invariance

Run fresh processes with different batch boundaries. For each migrated `partitionable=True` strategy, vary table order, route, batch size, and null handling.

Phase 1 does not include crash-resume. An interrupted run aborts its stage. A retry starts from row zero.

### T3. Frozen performance and memory gate

Before each optimization slice, record:

- the representative workload
- the correctness guard and baseline
- the minimum speed target or non-regression limit
- the memory and spill budgets
- the warmup and repetition counts
- the variance and tail-reporting methods

Run each size in a fresh process or container. Measure absolute peak RSS externally. Report latency or throughput with memory, spill, variance, and tail behavior.

### T4. C1 fidelity and route proof

Freeze the exact C1 recipe and dataset tier before Part 1 Phase 3 starts. Record current end-to-end latency, hash throughput, Faker throughput, peak RSS, and spill use.

Measure rare-category frequency, cardinality, correlation, conditional distributions, and Faker locale behavior. Keep each metric within its frozen tolerance.

After Part 1, run the parity suite, benchmark harness, and prod-sim C1 workload. The evidence must identify the intended streaming and native route.

If the oracle completes, admission rejects the job, or the engine uses a fallback, the C1 gate fails.

The route test uses one unchanged frozen C1 config. Phase 1 must exclude it with `faker_not_chunk_compatible`. Phase 3 must admit it before staging.

The route matrix covers every C1 provider and resolved config. It also covers nonpoolable Faker, non-C1 providers, `python_only`, and `reject_large`.

The test must prove that admitted C1 Faker columns carry `pool_quality`. It must also prove that publication waits for this obligation.

### T5. Bounded state

Assign a fixed memory budget to every Part 1 state owner. Include the masking-pool cache, warning state, error state, and each DuckDB-backed table.

Use high-cardinality adversaries. Make sure that no state owner falls back to an unbounded Python dictionary, set, or list.

### T6. Publication and fallback semantics

Test table-atomic publication, durable partial-publication records, pre-existing target preservation, spool cleanup, cancellation, and claim-time disk preflight.

Do not permit a mid-stream fallback after staging or native execution starts.

### T7. Part 2 gates

Each activated Part 2 slice includes all related parity, determinism, diagnostics, fidelity, bounded-state, and publication gates.

Move FK correctness and Phase 4 spill gates to Part 2. These gates include RI, orphan policy, composite FK groups, cycles, stable order, and global warnings.

### Standing CI posture

- Both repositories must pass CI. No full-frame golden fingerprint moves without a versioned determinism change and a release-notes entry.
- Run coverage and mutation gates on changed units before each merge. Test counts alone are not evidence.

---

## Part F: Sizing, Dependency Graph, Risks, Acceptance

### F.1 Sizing and commitment

The former Phase 0-through-4 total is retired as an active commitment.

- Phase 0 implementation is complete. Its remaining work is cleanup, the exact-HEAD repository gate, and merge.
- Phase 1 keeps its detailed scope. Re-estimate its remaining work after the admission classifier lands.
- The narrow Part 1 Phase 2 and Phase 3 slices need estimates before implementation.
- Each Part 2 activation needs a separate estimate for its dependency-closed slice.
- Phase 5 has no active estimate because it remains held.

Do not infer a Part 1 total from the retired program estimate.

### F.2 Biggest risks and mitigations

1. **Determinism can drift across partitions.** The completed Phase 0 protocol and goldens remain mandatory. T2 adds fresh-process partition tests for each migrated path.
2. **C1 speed can fail to improve.** Freeze the exact workload and target before implementation. Report end-to-end results, variance, tail behavior, memory, and spill.
3. **Faker pool selection can preserve parity but lose fidelity.** Apply exact parity and the frozen C1 fidelity metrics. Keep pool construction and selection as separate contracts.
4. **The Rust extension can fail through packaging, ABI, threading, or key handling.** Apply the full Rust gate before staging can start.
5. **Route evidence can hide fallback.** Record the selected and executed route. Treat oracle execution, rejection, or fallback as a failed C1 gate.
6. **High-cardinality state can remove the memory gain.** Give each Part 1 state owner a fixed budget and a cardinality adversary.
7. **Publication can overstate atomicity.** Publish each table atomically and record partial publication. Do not claim cross-target atomicity.
8. **Admission can charge memory twice.** Use one claim-time lease for the selected route. Do not register per-table budgets.
9. **Unknown providers can bypass the native boundary.** Enforce `reject_large` at claim time. Permit oracle work only below the priced small-job limit.
10. **Deferred scope can return without evidence.** Require an activation record for every Part 2 slice.

### F.3 Relationship to admission/memory-safety (explicit landing dependency, do not merge)

The memory ledger remains a landing dependency. Its claim-time classifier selects the route, calculates one reservation, and runs the disk preflight before dispatch.

Phase 1 orchestration consumes the selected route and committed lease. It does not acquire another lease or register per-table budgets.

The disk preflight is per filesystem: `sum(cloud_source_staging) + max_concurrent_output_or_local_target_stage + copy_finalize_overhead + headroom`. Insufficient disk rejects or defers before dispatch.

Part 1 also enforces `reject_large` at this boundary. The oracle threshold must be explicit and priced.

### F.4 Per-phase acceptance criteria

Phase 0 keeps its recorded acceptance criteria. Phase 1 keeps its detailed acceptance criteria.

Part 1 Phase 2 requires exact parity, Rust gate completion, and its frozen performance and resource targets.

Part 1 Phase 3 requires exact parity, bounded state, C1 fidelity, and the frozen performance targets. The intended streaming and native route must run.

The unchanged C1 recipe must move from Phase 1 exclusion to Phase 3 admission before staging. Its Faker columns must enforce `pool_quality`.

After Part 1, run the parity suite, benchmark harness, and prod-sim C1 workload. Use these results to review the 100M-row cap.

Each Part 2 activation defines its own acceptance criteria before implementation.

---

## Self-Review

- **Decision:** The plan now has an active Part 1 and an evidence-gated Part 2. Phase 5 remains held.
- **Status:** Phase 0 implementation is complete. The Opus review reported merge-ready at `13f91491`. Cleanup, the exact-HEAD gate, and merge remain open.
- **Status:** Task 1.1 is implemented at `b84054b4`. Its review waits for the full Phase 1 gate. Tasks 1.2 through 1.5 remain paused.
- **Part 1 compute:** Phase 2 includes only `passthrough`, `redact`, `truncate`, keyed `hash`, and the Rust `KeyedDerivationKernel`.
- **Part 1 state:** Phase 3 includes only the C1 masking pools, native source-keyed selection, and state that this path needs.
- **Part 1 route:** Phase 3 widens admission only for the exact C1 poolable providers and compatible `mask.faker` config.
- **Quality obligation:** The widening permits and enforces only `pool_quality`. The unchanged C1 recipe must become admitted before staging.
- **Deferred scope:** Native FPE, synthetic generation, other providers, vault, composites, custom pools, `joint_mask`, diagnostics, and Phase 4 moved to Part 2.
- **Provider policy:** Part 1 enforces `reject_large` at claim time and permits only a priced small-job oracle route.
- **Performance:** Every optimization slice freezes its workload, baseline, target, resource budget, warmup, repetitions, variance, and tail method before implementation.
- **C1 gate:** The final gate requires parity, fidelity, bounded state, benchmark evidence, prod-sim evidence, and proof that the intended route ran.
- **Sizing:** The previous program total is not an active commitment. The narrow Phase 2 and Phase 3 slices need estimates before implementation.
- **Oracle:** The pandas full-frame path remains a permanent route. This plan contains no full-cutover promise.
