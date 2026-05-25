# V2.1 Legacy Feature Coverage Audit

**Purpose:** Week 1 gate for V2.1 Legacy Removal sprint. Enumerate every
behavior `Masker`, `DataGenerator`, and the legacy `IOHandler` connector
provide. Assign a decision to each. No code is deleted until this table
has Dennis's sign-off.

**Produced:** 2026-05-25 Session 14  
**Status:** DRAFT — awaiting Dennis sign-off before any deletion PR opens  
**Decision abbreviations:** DROP = hard-delete; PORT = must port to graph
before deleting; COVERED = graph already handles this; KEEP = not legacy;
VERIFY = needs parity test before deciding.

---

## 1 · Masker (`masker/masker.py` + `masker/processor.py`)

| # | Feature | Where it lives | Graph runner covers? | Decision |
|---|---|---|---|---|
| M1 | YAML masker config format (`masking_rules`, `global_settings`) | `masker/masker.py:__init__` | Yes — graph YAML replaces it | **DROP.** CLI builds a graph internally for one-file workflows. |
| M2 | Single-file CSV/parquet mask (load → mask → save) | `masker/masker.py:mask()` + `_process_standard_file()` | Yes — `source.file → mask → target.file` | **COVERED.** Graph handles this end-to-end. |
| M3 | Large-file chunked processing (>1 GB threshold, configurable via `large_file_threshold_gb`) | `masker/masker.py:_process_large_file()`, `internal/large_file_processor.py` | Partial — DuckDB source.file streams; pandas path is in-memory | **VERIFY.** Run the parity test suite on a 1.5 GB fixture with the graph source.file (DuckDB) path before deleting LargeFileProcessor. If graph throughput is acceptable, drop. If not, port streaming to a graph file-source option. |
| M4 | Row-level conditional masking (`conditions` block with AND/OR logic, 11 operators: eq, ne, gt, gte, lt, lte, in, not\_in, contains, not\_contains, is\_null, is\_not\_null) | `masker/processor.py:_evaluate_conditions()` | **No.** Graph `mask` op applies strategies unconditionally to all rows. | **PORT.** Add optional `conditions` block to graph `mask` op config. Validator enforces structure; apply-side filters rows before applying strategy. Alternatively, the `if_router` op can route rows to conditional mask paths — audit whether existing graph ops compose to cover this. Decision requires Dennis. |
| M5 | Format preservation after masking (`preserve_format: true` flag) | `masker/processor.py:apply_masking_rules()` calling `apply_format_preservation()` | Yes — `apply_format_preservation()` is already imported and called in `graph/ops/mask_op.py` | **COVERED.** |
| M6 | Referential integrity (consistent masking across FK columns across tables) | `internal/integrity.py:ReferentialIntegrityManager` (shared) | Yes — graph handles FK consistency via `column_relationships` config | **KEEP.** Not legacy; shared subsystem. Not deleted. |
| M7 | Structured lineage emission (`emit_lineage`, `emit_step`) | `masker/masker.py:mask()` via `context.py` | Yes — graph runner emits the same lineage events via `ctx` | **COVERED.** |
| M8 | Context-injected keyed determinism (`derive_key` from ExecutionContext) | `masker/masker.py:__init__`, `StrategyManager` | Yes — graph ctx threads `derive_key` to StrategyManager | **COVERED.** |

### Masker summary

One genuine gap: **M4 (row-level conditional masking)**. All other features are
either covered by the graph runner or shared infrastructure not being deleted.

M4 must be resolved before the Masker delete PR opens. Two options:
- **Option A (Port to mask op):** Add `conditions` block to `graph/ops/mask_op.py`.
  ~80-120 LOC; validator + apply-side changes. Creates parity with the legacy
  path. Preferred if the feature is used in any current demo/customer config.
- **Option B (Compose with if\_router):** `if_router` already routes rows by
  condition. A conditional mask pattern is:
  `source → if_router (condition) → [mask_branch, passthrough_branch] → join`.
  More verbose in YAML but no new mask op code. Preferred if M4 is rarely used
  and ops-compose is acceptable.

**Action:** Dennis decides between Option A and B. If any existing demo or
platform-generated config uses `conditions:` in a masker YAML, Option A is
mandatory.

---

## 2 · DataGenerator (`generators/generator.py`, `generators/columns.py`, `generators/relationships.py`)

| # | Feature | Where it lives | Graph runner covers? | Decision |
|---|---|---|---|---|
| G1 | YAML generator config format (`tables`, `generator_settings`) | `generators/generator.py:__init__` | Yes — graph YAML replaces it | **DROP.** CLI builds a graph internally. |
| G2 | `faker` column type (all Faker providers, locale, seeded) | `generators/columns.py:_generate_faker_column()` | Yes — generate\_op delegates to ColumnGenerator | **COVERED.** |
| G3 | `sequence` column type (sequential integers with start/step) | `generators/columns.py:_generate_sequence_column()` | Yes — generate\_op delegates to ColumnGenerator | **COVERED.** |
| G4 | `categorical` column type (values list + cardinality bounds) | `generators/columns.py:_generate_categorical_column()` | Yes — generate\_op delegates to ColumnGenerator | **COVERED.** |
| G5 | `formula` column type (inline Python expression, per-row deterministic seeding) | `generators/columns.py:_generate_formula_column()` → `_eval_formula_inline()` | Yes — generate\_op delegates to ColumnGenerator | **COVERED.** |
| G6 | `formula` column with **cross-column references** (`references: [col_a, col_b]`) — post-pass fill after all other columns generated | `generators/columns.py:_generate_formula_column()` returns Nones + `generators/generator.py:_process_referenced_formulas()` fills them | **No.** `generate_op.py` calls `gen.generate_column()` which returns None placeholders but has no post-pass to fill them. **Silent bug: referenced formula columns produce all-None output in the graph runner.** | **PORT.** Add a post-pass to `generate_op.apply()`: after pass 1 + pass 2, collect columns where `references` is non-empty; re-evaluate them with sibling values in scope. ~60-80 LOC. This is a correctness gap, not just a feature gap. |
| G7 | `reference` column type (FK pool sampling from another table) | `generators/columns.py:_generate_reference_column()` | Yes — generate\_op materializes parent pools via pool\_resolver and coerces FK columns to `reference` type | **COVERED.** |
| G8 | Cross-table FK with distribution control (random / skewed / uniform, min/max per parent) | `generators/columns.py:_generate_reference_column()` + generate\_op FK coercion | Yes — generate\_op threads distribution/weights/min\_per\_parent/max\_per\_parent from column\_relationships | **COVERED.** |
| G9 | Self-referential FK within same node | `generators/generator.py:RelationshipHandler.process_self_references()` | Yes — generate\_op handles self\_ref\_targets in pass 2 | **COVERED.** |
| G10 | Many-to-many junction tables (cartesian / sampled / weighted pool strategies) | `generators/generator.py:RelationshipHandler.process_many_to_many_relationship()` | Yes — generate\_op handles m2m\_specs via `_emit_m2m_junction()` | **COVERED.** |
| G11 | Multi-parent FK (composite key from multiple parents, joined with `\|`) | generate\_op multi\_parent\_targets | Yes — generate\_op handles multi\_parent\_targets | **COVERED.** |
| G12 | Custom Faker provider pool (list-backed, platform-synced) | generate\_op custom\_provider FK path | Yes — generate\_op handles `custom_provider` FK source | **COVERED.** |
| G13 | PK uniqueness check with `DECOY_PK_LENIENT` escape hatch | generate\_op pk\_metrics + PKDuplicatesError | Yes — generate\_op has full PK uniqueness check | **COVERED.** |
| G14 | Fixed-width file output (`output_type: fixed_width`, `_parse_fixed_width_definition`) | `generators/generator.py:_generate_table()` + `_parse_fixed_width_definition()` | **No.** `target.file` supports CSV/parquet only. | **AUDIT USAGE.** Search all demo configs, platform fixture YAML, and test fixtures for `output_type: fixed_width` in generator configs. If no hits: drop. If hits: evaluate whether to add fixed-width format to `target.file` or declare as dropped. |
| G15 | Fixed-width post-pass for referenced formulas | `generators/generator.py:_process_referenced_formulas()` (fixed\_width branch) | **No.** Blocked by G14 gap. | **Follows G14 decision.** |
| G16 | Pipeline-bound keyed determinism (`pipeline_derive_key` from ctx) | `generators/generator.py:DataGenerator.__init__` | Yes — generate\_op reads `pipeline_derive_key` from ctx | **COVERED.** |
| G17 | Instance-default locale for Faker (`instance_default_locale` from ctx) | `generators/generator.py:DataGenerator.__init__` | Yes — generate\_op reads `instance_default_locale` from ctx | **COVERED.** |
| G18 | Configuration pre-flight (validates fixed-width definition files exist before run starts) | `generators/generator.py:_preprocess_configuration()` | Partial — graph validator runs structural checks before execution; fixed-width definition file existence not checked | **Follows G14 decision.** If fixed-width output is dropped, pre-flight is moot. |

### DataGenerator summary

Two genuine gaps:

1. **G6 (cross-column formula references)** — this is a correctness bug in the
   current graph runner, not merely a missing feature. Any graph config that uses
   `references: [...]` on a formula column silently gets all-None output. Must
   port the post-pass before V2.1 deletion. ~60-80 LOC in generate\_op.py.

2. **G14/G15 (fixed-width output)** — usage audit needed before deciding.
   Fixed-width SOURCE is already supported in source.file (`format: fixed_width`).
   Only the OUTPUT side is a gap.

All other DataGenerator capabilities are already covered by the graph generate op.

---

## 3 · IOHandler Connector (`connectors/base.py`, `connectors/factory.py`, `connectors/fixed_width.py`)

| # | Feature | Where it lives | Graph runner covers? | Decision |
|---|---|---|---|---|
| C1 | CSV load (`load_data()` → pandas `read_csv`) | `connectors/base.py` subclasses | Yes — `source.file format:csv` | **COVERED.** |
| C2 | Parquet load | `connectors/base.py` subclasses | Yes — `source.file format:parquet` | **COVERED.** |
| C3 | SQLite load | `connectors/base.py` + factory routing | Yes — `source.db` via DuckDB sqlite\_scanner | **COVERED.** (Note: sqlite\_scanner requires outbound internet in CI; tag tests with `@pytest.mark.requires_infra`.) |
| C4 | CSV save | `connectors/base.py` subclasses | Yes — `target.file format:csv` | **COVERED.** |
| C5 | Parquet save | `connectors/base.py` subclasses | Yes — `target.file format:parquet` | **COVERED.** |
| C6 | SQLite save | `connectors/base.py` + factory routing | Yes — `target.db` | **COVERED.** |
| C7 | Fixed-width load (positional column parsing from definition file) | `connectors/fixed_width.py:FixedWidthHandler.load_data()` | Yes — `source.file format:fixed_width fw_columns:[...]` | **COVERED.** Graph source.file has native fixed-width read support. |
| C8 | Fixed-width save (write padded fixed-width from DataFrame + definition file) | `connectors/fixed_width.py:FixedWidthHandler.save_data()` | **No.** `target.file` supports CSV/parquet only. | **AUDIT USAGE.** Same as G14: search for any config that saves to fixed-width. If no hits, drop FixedWidthHandler.save\_data and the entire connector. |
| C9 | Chunked DataFrame splitting (`chunk_dataframe()`) | `connectors/base.py:IOHandler.chunk_dataframe()` | Partial — DuckDB source.file streams; pandas mask path is in-memory | **Follows M3 (LargeFileProcessor) decision.** |
| C10 | File size info logging (`get_file_size_info()`) | `connectors/base.py:IOHandler.get_file_size_info()` | Partial — graph logs I/O events via ctx logger | **DROP.** Diagnostic utility; not a user-facing feature. |
| C11 | Column configuration injection (`set_column_configurations()`) | `connectors/base.py:IOHandler.set_column_configurations()` | Not needed — graph ops carry their own column config | **DROP.** |

### IOHandler summary

The IOHandler abstraction is entirely replaceable by graph ops. The single
open question is fixed-width SAVE (C8), which is the same question as G14.

`create_io_handler()` and `connectors/factory.py` can be deleted as soon as
`Masker` and `DataGenerator` are deleted (nothing else calls them).

---

## 4 · LargeFileProcessor (`internal/large_file_processor.py`)

| # | Feature | Where it lives | Graph runner covers? | Decision |
|---|---|---|---|---|
| L1 | Chunked CSV/parquet masking for large files (splits at configurable byte threshold, processes chunk-by-chunk, concatenates) | `internal/large_file_processor.py:LargeFileProcessor.process_large_dataset()` | Partial — DuckDB `source.file` path streams by batches; pandas `source.file` path is fully in-memory | **VERIFY before delete (same as M3).** If a 1.5 GB CSV masked via the graph runner (DuckDB engine) completes without OOM, the LargeFileProcessor's use case is covered. Specific acceptance: `run_graph` on a 1.5 GB CSV using a mask node produces correct output within 2x the time of the legacy path. |

---

## 5 · Shared infrastructure — NOT legacy, NOT deleted

These modules are used by the graph runner directly and must not be deleted:

| Module | Used by graph runner? | Decision |
|---|---|---|
| `internal/integrity.py:ReferentialIntegrityManager` | Yes — graph ops use FK consistency checks | **KEEP.** |
| `generators/columns.py:ColumnGenerator` | Yes — `generate_op.py` delegates to it directly | **KEEP.** |
| `generators/derivation.py` | Yes — seeded key derivation used across engine | **KEEP.** |
| `transforms/` (hash, faker\_based, date\_shift, etc.) | Yes — `mask_op.py` delegates to these | **KEEP.** |
| `expressions.py` | Yes — `generate_op.py` formula evaluation | **KEEP.** |

---

## 6 · Pre-deletion gate checklist

Before any V2.1 deletion PR opens:

- [ ] **Dennis sign-off** on this audit table (week 1 deliverable).
- [ ] **M4 resolution**: Dennis decides Option A (port conditions to mask op) or Option B (compose via if\_router). If Option A, the port PR lands before the Masker delete PR.
- [ ] **G6 fix**: Port cross-column formula post-pass to generate\_op.py. This is a correctness bug; must be fixed regardless of V2.1 timing. Suggested: land as a standalone bug-fix PR now, not gated on V2.1.
- [ ] **G14/C8 usage audit**: `grep -r "output_type.*fixed_width\|fixed.width" decoy-platform/api decoy/src docs/` — if any hit, evaluate port-vs-drop; if no hit, safe to drop.
- [ ] **M3/L1 large-file verification**: Run a 1.5 GB fixture through `run_graph` (DuckDB path). Measure memory ceiling and runtime vs legacy. Document result.
- [ ] **Platform PR first**: `decoy-platform/api/jobs/runner.py` and any other platform callers of `Masker`, `DataGenerator`, `create_io_handler` must be updated to graph-only APIs before the engine delete PR lands.
- [ ] **CLI PR first**: CLI commands that call `Masker()` or `DataGenerator()` must be updated to call `run_graph` internally before the engine delete PR lands.
- [ ] **Parity tests**: Run `tests/parity/` for 7 consecutive days post-delete as the backstop gate.

---

## 7 · Feature count summary (for sprint re-scope gate)

Per the sprint plan: "if audit finds more than 3 features requiring porting
(excluding Masker and DataGenerator themselves), sprint pauses for re-scope."

Features requiring porting/verification:
1. **M4** (conditional masking) — PORT or COMPOSE decision needed
2. **G6** (cross-column formula references) — PORT required (correctness bug)
3. **M3/L1** (large-file chunked processing) — VERIFY before delete
4. **G14/C8** (fixed-width output) — AUDIT then DROP or PORT

Count: **4 items** — one over the "more than 3" re-scope threshold per the sprint plan. Dennis should review whether G6 (a pre-existing correctness bug, already present) counts toward the threshold or whether it's a separate bug fix that doesn't pause the sprint, reducing the pause-worthy items to 3 (M4, M3/L1, G14/C8).

Dennis's call: proceed at the current scope, or add a scoping buffer.
