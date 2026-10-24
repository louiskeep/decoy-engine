# Execution backends and route coverage

What actually computes each masking strategy, by route, and where a shape is
declined with a caller-visible reason instead of run natively. Generation is
covered at the end.

This page is a human-readable reading of the authoritative, test-enforced sources:
the per-operator compute classification in
[`records/2026-10-16-d-audit-compute-matrix.md`](records/2026-10-16-d-audit-compute-matrix.md)
(masking) and the generation compute audit (the
[F-audit plan](https://github.com/louiskeep/decoy-engine/blob/main/docs/plans/2026-10-19-phase-f-f-audit-no-legacy.md)).
The enumerated strategy/provider list is [`capability-matrix.md`](capability-matrix.md);
install and wheel/ABI facts are [`native/supported-matrix.md`](native/supported-matrix.md).
If this page and those sources ever disagree, the sources win: they are derived from
the dispatch constants and guarded by parity tests, this page is prose.

## Backends, in one line each

- **compiled Rust kernel**: the native companion owns the compute. `text_redact` and
  `text_mask` are Rust span detection with the splice in Python.
- **compiled index selection**: `faker` draws from a pool via the compiled index kernel.
- **named-Arrow**: `redact` / `truncate` run as Arrow array kernels (`redact_array` /
  `truncate_array`), no companion needed.
- **no-op**: `passthrough` carries the column through untouched.
- **dedicated Python**: a named handler or primitive with no Rust or Arrow kernel. This is
  the designed compute for that strategy on that route, not the generic pandas fallback for
  an admitted kernel cell.
- **key declined, reporting-only**: used only for an FK key. The strategy is not admitted as
  a native key, so its Python handler runs in place for the key as a reporting-only entry;
  the job is not rerouted and the key surface stays correct.
- **excluded**: not admitted on that route (an off-the-key-surface FK cell, a native-chunked
  FK cell, or the full-frame oracle itself).
- **declined (caller-visible reason)**: the shape is not admitted on that route and is
  rerouted, with a coded reason, to the full-frame pandas oracle, never silently. This is
  distinct from a reporting-only key decline, which runs in place.
- **full-frame oracle**: the pandas reference path. It is the parity oracle for every native
  route, and the path a native cell downgrades to (with a coded reason, byte-identical) when
  the companion is absent. A few cells, such as out-of-core `bucket_perturb`, keep a dedicated
  companion-absent Python bridge instead.

The compiled companion is reachable only from masking. Generation never calls it (see
"Generation" below).

## Masking by route

The route is chosen by the planner from job shape and size; a caller does not pick a
backend directly. Byte-parity with the full-frame oracle is mandatory on every native
route.

### Sequential route (FK, table-at-a-time)

| Strategy | Payload | As an FK key |
| --- | --- | --- |
| `hash`, `fpe` | compiled Rust | compiled Rust |
| `categorical`, `bucket_perturb`, `date_shift` | compiled Rust | dedicated Python (key declined, reporting-only) |
| `faker` | compiled index selection | dedicated Python (key declined, reporting-only) |
| `text_redact`, `text_mask` | compiled Rust spans, Python splice | dedicated Python (key declined, reporting-only) |
| `redact`, `truncate` | named-Arrow | named-Arrow |
| `passthrough` | no-op | no-op |
| `code_set` | dedicated Python | dedicated Python |
| `bucketize`, `shuffle`, `top_code`, `formula`, `derived`, `derived_aggregate`, `nested`, `joint_mask`, `geo_generalize`, `grouped_series`, `windowed_date`, `group_key` | dedicated Python (shared handler, no SEQ native admission) | dedicated Python |

A non-injective but natively-capable strategy used as an FK key is declined with a
reporting-only entry and the Python handler runs; the key surface stays correct.

### Out-of-core route (payload; large FK jobs that spill)

The FK key surface here is only `hash`, `redact`, `truncate`, `passthrough`.

| Strategy | Payload | As an FK key |
| --- | --- | --- |
| `hash` | compiled Rust | compiled Rust (needs a namespace) |
| `fpe`, `categorical`, `text_redact`, `bucket_perturb` | compiled Rust | excluded (off the key surface) |
| `redact`, `truncate` | named-Arrow | named-Arrow |
| `passthrough` | no-op | no-op |
| `text_mask` | dedicated Python (per-cell loop; backlog: route through the span kernel) | excluded (off the key surface) |
| `code_set` (mask mode) | dedicated Python (no kernel on any route; backlog: Arrow dict-map) | excluded (off the key surface) |
| `faker`, `date_shift`, `bucketize`, `geo_generalize`, `formula`, `derived`, `nested`, and the rest | declined (caller-visible reason) | excluded (off the key surface) |

On this route a composite FK whose child columns would be masked independently as scalars is
declined (`out_of_core_composite_fk_scalar_child_unsupported`); the canonical
`composite_fk_group` unit is admitted. Multi-parent is declined only when more than one parent
targets the same child FK column tuple (`out_of_core_multi_parent_child_unsupported`); distinct
child keys referencing different parents are supported. Native support for the declined shapes
is a scheduled V1 engine track (A-9).

### Native-chunked route (single-table, chunked)

FK-participating tables on either side reroute to the oracle
(`fk_relationship_not_native_route`), so no FK cell runs here. Non-FK payload follows the
operator registry: `hash`, `categorical`, `bucket_perturb`, `date_shift`, `fpe`,
`group_key` on compiled Rust; `faker` as compiled index selection; `redact` / `truncate`
as named-Arrow; `text_redact` / `text_mask` as the compiled span kernel behind an
`arrow_python` operator; `passthrough` as a no-op. For `text_redact` / `text_mask` the
chunked-route tests prove the `arrow_python` routing and byte-parity with the oracle, not
that the compiled span kernel is consumed; the compiled-span-consumption witness is the
sequential route's.

### Dedicated-Python exceptions (the documented set)

These run their own Python handler by design, not the generic fallback:

- `code_set`: no kernel on any route (backlog: kernelize as a named-Arrow dict-map).
- NER (the `ner` config of `text_redact` / `text_mask`): Python by design (spaCy inference).
- `faker` masking runs as compiled index selection only on its native legs (deterministic REUSE
  and positional REUSE). It stays on its Python handler when the provider is not on the native
  allowlist, when a deterministic config is not the REUSE variant or carries no namespace (the
  oracle rejects it), when a non-deterministic mode other than REUSE is used (UNIQUE, MATCH, and
  SCALE need whole-column RNG state), or when the source column is not an exact `string`. A faker
  column that stays on Python is still pool-backed: the handler builds and caches one value pool
  and samples it, not per-row live-provider calls.
- out-of-core `text_mask`: the per-cell loop; the SEQ and native-chunked routes already use the
  Rust span kernel (backlog: route OOC through it too).
- the shared SEQ handlers listed in the sequential table (`bucketize`, `shuffle`, `top_code`,
  `formula`, `derived`, `derived_aggregate`, `nested`, `joint_mask`, `geo_generalize`,
  `grouped_series`, `windowed_date`, `group_key`): full-frame handler by design, with no SEQ
  native admission. `group_key` is the one of these with a native path on another route: it
  runs compiled Rust on the native-chunked route (above).

## Generation

Generation computes every value on Python and NumPy and never on the Rust companion. The Rust
generation-kernel-selection experiment (FR3) was shelved after it measured slower than
vectorized NumPy RNG, so this is by design, not a gap. The compiled index kernel is reachable
only from masking's deterministic pool path; no generation column enters it. Per-kind compute:
`sequence` and sequential `reference` draw by direct index (no selection event); `categorical`
and random/weighted `reference` draw via Python RNG; pooled `faker` via NumPy selection; per-row
`faker` via live provider calls; `windowed_date` via a NumPy offset draw plus date arithmetic.
The ineligible families run on the legacy whole-table synthesize path with distinct compute:
`formula` is a formula-expression evaluation; `derived` and `derived_aggregate` select nothing
(they are computed from other columns); `statistical`, `grouped_series`, and `group_key` are
whole-table synthesize. Resident and streamed materialization produce byte-identical output.

## Progress events

Every route reports the same value-free event stream when the caller passes
`run_pipeline(progress=sink)` (or wraps another entry point in
`decoy_engine.progress.use_progress(sink)`): a resolve phase, one route event naming
the layer-1 route and the lane, per-table mask or generate phases with row counts, rate
samples (rows done and rows/s, time-throttled) from the chunked dispatcher, the
out-of-core batch stream and generation batches, and a terminal event. The full-frame
oracle reports its mask step once, not per table, because it has no finer boundary.
Output bytes never depend on the sink.

## What this means for a caller

- If a job lands on a native route, the strategies marked compiled Rust / compiled index
  selection / named-Arrow run natively; the dedicated-Python, reporting-only-key, excluded, and
  declined cells do not, and a declined cell always says why.
- A missing native companion is not a silent slow-path: a native cell downgrades to the
  byte-identical full-frame oracle with a coded reason (a few cells keep a companion-absent
  Python bridge instead). The CLI bundle ships the companion by default and treats a missing one
  as a loud refusal rather than running degraded.
- Generation speed comes from vectorized NumPy, not the companion; installing the companion does
  not change generation throughput.
