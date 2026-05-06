# Decoy Engine — Capabilities Guide

> **Status:** partial — captures the surface area as of 2026-05-06. Some areas (faker provider list, FORECAST scoring math) are inferred from current code and will drift as features land. Update in the same PR that changes the surface area.
> **Last reviewed:** 2026-05-06
> **Purpose:** Single reference for "what does the engine actually do today?" — used by CLI/Platform planners and by anyone scoping a streaming/trickle (CDC) follow-on. Pairs with the streaming gap matrix at the end.

---

## How to read this

- **Capability matrix tables** are the quick scan. Each row = one user-visible knob.
- **Determinism column legend:**
  - `det/value` — same input value → same output across runs (assuming same seed).
  - `det/row` — output is a function of row index + seed.
  - `det/column` — column-level seeded RNG; ordering matters.
  - `det/run` — deterministic given full inputs + seed, but depends on full-table state (e.g. shuffle).
  - `nondet` — uses unseeded randomness or wall-clock.
- **Streaming column legend:**
  - `row` — pure per-row, safe for trickle/CDC.
  - `chunk` — works per chunk if chunks are independent.
  - `column` — needs the full column.
  - `table` — needs the full table.
  - `multi-table` — needs other tables materialized first.

---

## 1. Transforms (mask strategies)

Registry: `src/decoy_engine/transforms/registry.py`. Factory: `src/decoy_engine/transforms/factory.py:11`. Base: `src/decoy_engine/transforms/base.py:11`.

| Key | What it does | Required params | Optional params | Determinism | Null handling | Streaming |
|---|---|---|---|---|---|---|
| `passthrough` | Returns column unchanged | `column` | `seed` | det/value (identity) | preserved | row |
| `hash` | SHA256 (seeded) per value | `column` | `seed` | det/value | preserved | row |
| `redact` | Replaces non-null with constant | `column` | `redact_with` (default `"REDACTED"`), `seed` | det/value | preserved | row |
| `shuffle` | Permutes non-null values within column | `column` | `seed` | det/run | null positions held in place | **column** |
| `map` | Persistent original→replacement dict; backed by JSON on disk | `column`, `map_type` ∈ {`faker`, `hash`, `fixed`, `manual`} | `faker_type`, `fixed_prefix` (default `"MASKED"`), `mapping` (manual), `preserve_domain` (email), `seed` | det/value (cross-run, persisted) | preserved | chunk (mapping accumulates) |
| `faker` | Per-unique-value Faker replacement, in-memory mapping | `column` | `faker_type` (default `"word"`), `preserve_domain`, `seed` | det/value within run; cross-run via seed | preserved | chunk (mapping per run only) |
| `date_shift` | Shifts each date by MD5(value)%range days | `column` | `min_days` (default `-365`), `max_days` (default `365`), `date_format` (auto-detected) | det/value | preserved; invalid dates pass through with warning | row |
| `formula` | `eval` a Python expression or f-string template per cell | `column`, `formula` | `formula_type` ∈ {`basic`, `template`} | depends on formula (`value.upper()` is det/row; `randint()` is nondet) | nulls skipped | row |

**Per-transform notes**

- `passthrough` (`transforms/passthrough.py:19`) — useful for declaring intent (column is reviewed, intentionally untouched).
- `hash` (`transforms/hash.py:20`) — hex string output via `deterministic_hash(str(val), seed)` (`internal/helpers.py`). Output type is always `str`; downstream typed consumers may need a cast.
- `redact` (`transforms/redact.py:19`) — output is always the literal string. Defaults `redact_with` to `"REDACTED"` if omitted (`transforms/redact.py:66`).
- `shuffle` (`transforms/shuffle.py:32`) — full column copy: `non_na_values = column[~na_mask].values.copy()` (`transforms/shuffle.py:51`). Hard blocker for streaming.
- `map` (`transforms/map.py:33`) — JSON-backed via `MappingManager` (`internal/mappings.py:12`). Storage path: `{mappings_dir}/{column}_map.json`. Caches in `_mapping_cache` (`internal/mappings.py:38`). The four `map_type` modes:
  - `faker` — generates fake via Faker provider, seeded.
  - `hash` — deterministic hash per unique value.
  - `fixed` — `{prefix}_{index}` strings.
  - `manual` — explicit dict from rule's `mapping` key.
  - Email domain preservation (`map.py:89`) splits on `@` and keeps the original domain when `preserve_domain: true`.
- `faker` (`transforms/faker_based.py:34`) — distinct from `map` with `map_type: faker`: this one's mapping is **in-memory only**; not persisted across runs unless seeded identically.
- `date_shift` (`transforms/date_shift.py`) — auto-detects format from `_COMMON_FORMATS` (lines 9-21). MD5-based deterministic per-value shift (line 50-54).
- `formula` (`transforms/formula.py:28`) — uses `eval` with a restricted globals dict (no `__builtins__`); allows `re`, `str`, `int`, `float`, `bool`, `len`, `round`, `abs`, `min`, `max`, `randint`, `choice`. Variable `value` is the cell. **Caveat:** `eval` is not a sandbox; it's a thin allow-list. Don't run user-controlled formulas without review.

---

## 2. Faker provider coverage

Exposed via `get_faker_providers()` (`internal/helpers.py:33-111`). Faker instance is seeded per-row in generators (`generators/columns.py:144`) and per-column in transforms.

| Family | Providers |
|---|---|
| Person | `first_name`, `last_name`, `name`, `prefix`, `suffix` |
| Contact | `email`, `phone_number`, `username` |
| Address | `address`, `street_address`, `city`, `state`, `state_abbr`, `zipcode`, `country` |
| Company / job | `company`, `company_suffix`, `job` |
| Finance | `credit_card_number`, `credit_card_provider`, `currency_code`, `ssn` |
| Date / time | `date`, `date_of_birth`, `future_date`, `past_date`, `time`, `day_of_week`, `month` |
| Internet | `domain`, `url`, `ipv4`, `ipv6`, `user_agent` |
| Text | `word`, `words`, `sentence`, `paragraph`, `text` |
| Misc | `color`, `color_hex`, `file_path`, `file_name`, `mime_type`, `uuid4` |

**Caveats**

- Locale: not parameterised; uses Faker's default (en_US). No multi-locale story today.
- No custom provider registry; adding a provider means editing `helpers.py`.
- Seed is global per call; cross-run stable. No locale-aware FPE / format-preserving variants.

---

## 3. Disguises (mask bundles)

Schema: `src/decoy_engine/disguises/schema.py:15-59`. Loader: `src/decoy_engine/disguises/loader.py`.

```yaml
id: hipaa
name: HIPAA Disguise
regulation: ...
field_rules:
  - detectors: [ssn]
    mask: hash.sha256_truncated
triggers:
  required_detectors: [...]
  any_detectors: [...]
  co_occurrence: [[...]]
  min_score: 0.3
```

**Shipped today:** `default.yaml`, `hipaa.yaml`.
**Planned launch set:** PCI, GLBA, GDPR, CCPA, FERPA, SOX (per `DISGUISES_GUIDE.md`).

**Field-rule matching** (`schema.py:40`): first rule whose detector ids overlap the column's `detector_matches` wins. Order in YAML matters.

---

## 4. Generators

Entry point: `DataGenerator` (`generators/generator.py:16`). Tables processed in config order — caller is responsible for FK ordering (`generator.py:84`). Reference data accumulates in `self.reference_data: dict[table, DataFrame]` (`generator.py:69`).

### 4a. Column generation modes

| `type` | What it generates | Key params | Determinism | Streaming |
|---|---|---|---|---|
| `faker` | Faker provider per row | `faker_type`, `null_probability` | det/row (per-row seed = base seed + i) | row |
| `sequence` | Sequential ids | `start`, `step`, `prefix`, `suffix`, `pad_length` | det/row (function of row index) | row |
| `categorical` | Random choice from list | `categories`, `weights` | seeded but RNG-state dependent | column (uses column-level RNG) |
| `reference` | Draws from another table's column | `reference_table`, `reference_column`, `distribution` ∈ {`random`, `sequential`, `weighted`}, `weights` | depends on distribution | **multi-table** (target must be materialized) |
| `formula` | Eval'd Python expression | `formula_type` ∈ {`basic`, `template`, `composite`}, `formula`, `references` | per-row seed = `seed + i` for basic/template; composite is post-pass | row (basic/template); **table** (composite) |

Implementations:
- `faker` (`columns.py:115-151`)
- `sequence` (`columns.py:153-189`) — pad-then-affix
- `categorical` (`columns.py:191-212`) — `random.choices()` with seeded module RNG
- `reference` (`columns.py:214-279`) — three distribution modes, returns placeholder if target missing
- `formula` (`columns.py:281-471`) — basic and template eval'd inline; `composite` defers to post-pass

**Composite formula language** (`generator.py:399-427`): safe locals include `str`, `int`, `float`, `round`, `min`, `max`, `len`, `hash` (MD5 first 8 chars), `random`, `randint`, `choice`, `now`, `today`, `days_from_now`, `months_from_now`, `years_from_now`, `format_date`. Row index available as `i` / `index`. Referenced columns passed via context dict. Evaluated as f-string: `eval(f"f'''{formula}'''", safe_locals, context)` (`generator.py:426`). Composite columns are **post-processed** after the table is written: CSV path re-reads, computes, writes back (`generator.py:348-392`); fixed-width path slices/pads in-place (`generator.py:261-345`).

**Basic formula safe globals** (`columns.py:354-386`): includes Faker date helpers (`date_between`, `date_this_decade`, `date_of_birth`, `time`, etc.) plus arithmetic (`days_from_now`, `months_from_now`, `years_from_now`, `now`, `today`).

**Null injection** (`columns.py:89-101`): applied after base generation, with row-level seed `seed + i + hash(column_name)`.

### 4b. Relationship handlers

`generators/relationships.py:12`.

| Type | Params | What it does | Streaming |
|---|---|---|---|
| `self_reference` | `table`, `column`, `reference_column`, `levels` (default 3) | Sets a parent FK within the same table; every (levels+1)-th row is a top-level (null) | **table** (`relationships.py:96-110`) |
| `foreign_key` | `source_table`, `source_column`, `target_table`, `target_column`, `distribution` ∈ {`random`, `sequential`, `weighted`}, `weights`, `null_probability` | Fills source column with target column values; coerces dtype | **multi-table** (`relationships.py:185, 221-276`) |
| `many_to_many` | `junction_table`, `left_table`, `left_column`, `right_table`, `right_column`, `left_cardinality`, `right_cardinality`, `max_relationships` | Builds junction-table pairs with cardinality control; dedupes + shuffles | **multi-table** (`relationships.py:321-399`) |

Many-to-many cardinality (`relationships.py:350-387`): `one-to-one` = `min(L,R)` unique pairs; `one-to-many` = each L → 1-5 R; `many-to-one` = each R → 1-5 L; `many-to-many` = 20-50% of cross-product (or `max_relationships`).

---

## 5. Mappings & referential integrity

`internal/mappings.py:12-174` — `MappingManager`.

- **Per-column mapping**: `{mappings_dir}/{column_safe}_map.json` (`mappings.py:40-54`).
- **Global mapping** (cross-table referential integrity): `{mappings_dir}/global_{relationship_safe}_map.json` (`mappings.py:56-70`).
- In-memory `_mapping_cache` keyed by file path.
- Symmetric load/save APIs: `load_mapping`, `save_mapping`, `load_global_mapping`, `save_global_mapping`.

`internal/integrity.py:12` — `ReferentialIntegrityManager`. YAML config:

```yaml
referential_integrity:
  - name: customer_id
    columns:
      - { table: orders, column: customer_id }
      - { table: customers, column: id }
```

`get_referential_relationship(table, column)` returns the relationship name or None (`integrity.py:75-100`); `apply_global_mapping` (in `processor.py`) consults the shared mapping so the same source value masks identically across tables.

---

## 6. Connectors

Factory: `connectors/factory.py:11-39`. Base: `connectors/base.py:11`.

| Type | Read | Write | Streaming read | Notes |
|---|---|---|---|---|
| `csv` | `pd.read_csv` (`csv_connector.py:32`) | `df.to_csv` or fixed-width fanout (`csv_connector.py:71`) | `get_chunk_iterator(chunk_size)` (`csv_connector.py:182`) | options: `delimiter`, `encoding`, `quoting` |
| `fixed_width` | Definition file with `FIELD/START/FINISH`; line-by-line slice (`fixed_width.py:40-95`) | `_save_as_fixed_width` (`fixed_width.py:97-120`) | 10k-row chunks (`fixed_width.py:80`) | 1-based positions in definition, converted to 0-based |
| `database` | `SELECT * FROM "<table>" [WHERE …]` via SQLAlchemy (`database.py:47`) | `df.to_sql(if_exists=…)` (`database.py:57`) | **none — full materialization** | `connector_dsn`, `table`, `where`, `if_exists` |

**Large-file masking path** (`internal/large_file_processor.py:37-160`):
- Triggered when input > `large_file_threshold_gb` (default 1.0 GB) (`masker/masker.py:123`).
- `chunk_size` configurable (default 100,000) (`large_file_processor.py:86`).
- Pattern: read chunk → process → append (header on first chunk only) (`large_file_processor.py:127-138`).
- Memory monitor reports every 10 chunks (`large_file_processor.py:124`).
- Only CSV (and fixed-width via line-by-line); database has no chunked path.

---

## 7. Context & determinism propagation

`context.py:41`.

```python
ExecutionContext(logger: Logger | None, telemetry: TelemetryClient | None)
```

- `Logger` Protocol: `debug/info/warning/error` (`context.py:19-32`). CLI implements Rich, Platform implements structured.
- `TelemetryClient.emit(event, properties)` (`context.py:34-38`) — published, currently unused by engine.
- Wiring caveat: not all engine entry points accept `ExecutionContext` yet (per docstring at `context.py:11-13`).

**Determinism story by surface:**
- **Cross-run**: same seed + same config + same input → same output, modulo:
  - `faker` transform's mapping is in-memory; cross-run determinism comes from seed + column ordering, **not** persisted state.
  - `map` transform persists JSON; cross-run identical.
  - `shuffle` is deterministic only if the full column is identical (chunked input ≠ full input).
  - Composite formulas using `now()` / `today()` / `days_from_now()` are wall-clock-bound — **not** cross-run stable.
- **Per-row seeding** (generators): `local_seed = base_seed + i` (`columns.py:144, 349`); `null_probability` uses `base_seed + i + hash(column_name)` (`columns.py:96`).
- **Cross-table** consistency requires either `referential_integrity` config (mappings.py global maps) or matching seeds plus deterministic transforms.

`internal/memory.py:9-115` provides `MemoryMonitor` used at strategic points in the masker (`masker.py:52, 113, 138, 199`) and large-file processor.

---

## 8. STORM (profiler) and FORECAST (recommender)

Spec: `STORM_FORECAST_GUIDE.md`. Types: `storm/types.py:16-106`, `forecast/types.py:15-72`.

**STORM outputs** (`StormProfile`):
- per-field: `inferred_type`, null/distinct stats, value ranges, top values, regex `detector_matches`, `sentinels`, `pii_score`
- table-level: `reid_risk_score`, `quasi_identifier_groups`

**Detectors** (regex/heuristic): SSN, US phone/ZIP, email, IPv4/v6, ICD-10, NPI, MRN, account number, credit card (Luhn), IBAN, US/ISO date, person name (Faker dictionary).

**Sentinels**: future dates, numeric outliers, placeholder strings (`N/A`, `UNKNOWN`, `test@example.com`).

**FORECAST** (`forecast/recommender.py`): for each profile, evaluates Disguise triggers, ranks by `match_score = sum(matched_field_weights) * disguise.score_weight`, emits `DisguiseRecommendation`s and a `proposed_pipeline_yaml`.

---

## 9. Capability matrix (single page)

| Capability | Granularity | Determinism | Persisted state | Streaming-safe? | Notes |
|---|---|---|---|---|---|
| `passthrough` | column | det/value | none | yes | identity |
| `hash` | value | det/value | none | yes | output is hex str |
| `redact` | value | det/value | none | yes | type→str |
| `shuffle` | column | det/run | none | **no — column** | full materialization |
| `faker` (transform) | unique value | det/value (in-run) | in-mem map | partial — chunk | mapping resets per run unless seed identical |
| `map: faker` | unique value | det/value (cross-run) | JSON map file | partial — chunk | growing JSON file |
| `map: hash` | value | det/value | JSON map file | yes | |
| `map: fixed` | unique value | det/value (within run) | JSON map file | partial | index ordering matters across chunks |
| `map: manual` | value | det/value | YAML rule | yes | |
| `date_shift` | value | det/value | none | yes | MD5-based |
| `formula` (transform) | row | depends | none | depends | `randint`/`choice` is nondet |
| `gen: faker` | row | det/row | none | yes | |
| `gen: sequence` | row | det/row | none | yes | |
| `gen: categorical` | column | det/run | none | column | seeded module RNG |
| `gen: reference` | row | distribution-dep | needs target table | **no — multi-table** | target loaded from `reference_data` |
| `gen: formula basic/template` | row | depends | none | row | safe-globals only |
| `gen: formula composite` | row | wall-clock if `now()` used | none | **no — table** | post-pass over written file |
| `rel: self_reference` | table | det/run | none | **no — table** | needs all rows |
| `rel: foreign_key` | row | distribution-dep | target df | **no — multi-table** | order constraint |
| `rel: many_to_many` | table | det/run | left+right df | **no — multi-table** | dedupes + shuffles |
| `referential_integrity` | global | det/value | global JSON map | yes | grows monotonically |
| CSV connector | streamable | n/a | none | yes (chunksize) | |
| Fixed-width connector | streamable | n/a | def file | partial (line-by-line, batched parse) | |
| DB connector | full load | n/a | none | **no** | no chunked SQL path |
| STORM | full table | det if input fixed | sample profile | sample-only OK | sampled by `sample_row_cap` |
| FORECAST | profile | det | none | yes | reads STORM output, not raw |

---

## 10. Streaming / trickle delta — what's missing

Goal: support row-at-a-time or micro-batch ingest (CDC, Kafka, Kinesis, or even a continuous file tail).

### Works as-is

- `passthrough`, `hash`, `redact`, `date_shift` — pure per-value, no state.
- `formula` (deterministic body) — pure per-row.
- `map` with `map_type: hash` or `manual` — no growing state.
- `gen: faker`, `gen: sequence` — per-row functions.
- `referential_integrity` global mappings — append-only JSON works in trickle if writes are serialized; **needs a concurrency story.**
- CSV chunked I/O — already iterator-based.

### Works per-chunk but with caveats

- `map: faker` / `map: fixed` — mapping JSON grows as new uniques arrive; safe if mappings are read-modify-write under a lock. Today's `MappingManager` (`internal/mappings.py:38`) holds an in-process cache; multi-writer trickle (Platform) needs an external store (Redis / DB row).
- `faker` transform (in-mem map) — without persistence, identical inputs in two trickle batches produce different fakes. **Either promote to `map: faker` or add a backing store.**
- `gen: categorical` — per-row works if you can accept that distribution shape only converges in expectation; today the column-level RNG path requires a column.

### Hard blockers (need redesign)

| Capability | Why it blocks | Possible direction |
|---|---|---|
| `shuffle` transform | Needs full column to permute (`shuffle.py:51`) | Replace with reservoir-sampled row pairing, or restrict to "shuffle within window of N rows" |
| `gen: reference` | Reads from `reference_data[table]` materialized DataFrame (`generator.py:69, 163`) | Back with a queryable store (DuckDB, Redis, DB) and stream from it |
| `rel: foreign_key` | Same materialization assumption (`relationships.py:185`) | Reuse same store; allow "draw from current snapshot of target" semantics |
| `rel: many_to_many` | Loads both sides + builds full pair list (`relationships.py:321-399`) | Not naturally streamable; defer to a batch sub-pipeline |
| `rel: self_reference` | Levels logic walks the whole table (`relationships.py:96-110`) | Same — batch sub-pipeline only |
| `gen: formula composite` | Re-reads written file post-pass (`generator.py:348-392`) | Push composite into row-time (basic formula), or buffer last N rows |
| `database` connector read | `SELECT *` with no chunking (`database.py:47`) | Add `yield_per` / server-side cursor or a CDC source |
| Composite formulas using `now()/today()` | Wall-clock breaks cross-run determinism | Inject "logical clock" / event timestamp from `ExecutionContext` |
| `MemoryMonitor`-driven chunk path in `LargeFileProcessor` | File-size-triggered, batch-shaped | Replace with a `Source` iterator abstraction the masker drives unconditionally |
| `ExecutionContext` not threaded through generators | Can't pass a `Source`/`Sink`/clock today | Finish the wiring noted in `context.py:11-13` |

### Missing architectural pieces for trickle/CDC

1. **Source abstraction**. Today the masker assumes "a connector that yields chunks of a single table." Trickle wants "a stream of `(table, row)` events."
2. **Sink abstraction**. CSV append exists; nothing for Kafka, JDBC sink, S3 multipart, etc.
3. **Watermark / commit semantics**. No "commit offset on chunk success" hook in `LargeFileProcessor`.
4. **Shared mapping store with concurrent writers**. `MappingManager` is single-process file-locked-by-convention.
5. **Stateful operator window**. No primitive for "shuffle within last N rows" or "categorical draws targeting a moving distribution."
6. **Out-of-order / late event handling**. No event-time vs processing-time distinction; composite formulas using `now()` would drift.
7. **Schema evolution**. `set_column_configurations` (`connectors/base.py`) is set once at handler init — no way to absorb a new column mid-stream.
8. **Backpressure / async**. All connectors are sync; there's an `await pipeline.run_async(...)` pattern referenced in `SHARED_ENGINE_ARCHITECTURE.md` but no `run_async` exists in code today.

### Suggested phasing for a streaming module

1. **Phase 0 — wire `ExecutionContext` through generators + masker.** Pre-req for injecting a `Source`/`Sink`/clock.
2. **Phase 1 — `Source`/`Sink` interfaces + a "row at a time" masker path** that only allows the `Works as-is` set above. Block other transforms with a clear "not streaming-safe" error.
3. **Phase 2 — externalize `MappingManager`** behind an interface; provide file-backed (current) and Redis/DB implementations.
4. **Phase 3 — windowed variants** of `shuffle` and `categorical`.
5. **Phase 4 — backed `reference` / `foreign_key`** via DuckDB or DB-of-record.
6. **Phase 5 — async sinks** (Kafka, S3 multipart) and watermarking.

Many-to-many, self-reference, and composite-with-references stay batch-only for the foreseeable future; document them as such.

---

## See also

- `SHARED_ENGINE_ARCHITECTURE.md` — engine/CLI/Platform split.
- `STORM_FORECAST_GUIDE.md` — profiler + recommender contract.
- `DISGUISES_GUIDE.md` — Disguise YAML schema and launch set.
- `CLAUDE.md` — repo orientation and doc taxonomy.
