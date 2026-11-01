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

The ExecutionAdapter substrate is pandas-only. `DECOY_SUBSTRATE` accepts `pandas`; other
values raise `invalid_substrate`. Execution routes are separate from this setting. Rust and
Arrow kernels can run inside those routes.

## Backends, in one line each

- **compiled Rust kernel**: the native companion owns the compute. `text_redact` is Rust
  span detection with the splice in Python. `text_mask` is Rust span detection, with native
  splice/FPE paths for admitted `text_mask` configurations and Python splice fallback.
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
- **declined (caller-visible reason)**: a route decline excludes the job from that route.
  Auto routing can choose sequential or full-frame, subject to memory admission. A forced
  incompatible route raises an error. A native-kernel decline can instead retain the current
  route and use its reference implementation. Backend evidence identifies that choice. This is
  distinct from a reporting-only key decline, which runs in place.
- **full-frame oracle**: the pandas adapter is the reference oracle for every native route,
  and the path a native cell downgrades to (with a coded reason, byte-identical) when the
  companion is absent. A few cells, such as out-of-core `bucket_perturb`, keep a dedicated
  companion-absent Python bridge instead. The full-frame route can also run the unified native
  slice.

The compiled companion is reachable only from masking. Generation never calls it (see
"Generation" below).

## Masking by route

The router selects an execution route from job shape, eligibility, and memory signals.
`execution_mode` can force a supported route. A caller does not select a masking backend
through `DECOY_SUBSTRATE`. Default byte-estimate routing can select OOC below five million
rows. The row threshold applies when byte-estimate routing is disabled. A confirmed probe can
select full-frame. The router lives in
[`_pipeline_routing.py`](../src/decoy_engine/execution/_pipeline_routing.py). Byte-parity with
the full-frame oracle is mandatory on every native route.

These cells identify available native implementations, not unconditional admission.
Configuration, source dtype, companion availability, and runtime guards still apply. A Python
fallback inside an admitted route is distinct from a route rejection. The per-strategy
qualifications are in [Native admission qualifications](#native-admission-qualifications)
below.

### Sequential route (FK, table-at-a-time)

| Strategy | Payload | As an FK key |
| --- | --- | --- |
| `hash`, `fpe` | compiled Rust | compiled Rust |
| `categorical`, `bucket_perturb`, `date_shift` | compiled Rust | dedicated Python (key declined, reporting-only) |
| `faker` | compiled index selection | dedicated Python (key declined, reporting-only) |
| `text_redact`, `text_mask` | compiled Rust spans; `text_redact` Python splice, `text_mask` native splice/FPE for admitted configs with Python splice fallback | dedicated Python (key declined, reporting-only) |
| `redact`, `truncate` | named-Arrow | named-Arrow |
| `passthrough` | no-op | no-op |
| `code_set` | dedicated Python | dedicated Python |
| `bucketize`, `shuffle`, `top_code`, `formula`, `derived`, `derived_aggregate`, `nested`, `joint_mask`, `geo_generalize`, `grouped_series`, `windowed_date`, `group_key` | dedicated Python (shared handler, no SEQ native admission) | dedicated Python |

A non-injective but natively-capable strategy used as an FK key is declined with a
reporting-only entry and the Python handler runs; the key surface stays correct.

### Out-of-core route (payload; large FK jobs that spill)

The FK key surface here is only `hash`, `redact`, `truncate`, `passthrough`.

| Strategy | Payload | Parent-key strategy |
| --- | --- | --- |
| `hash` | compiled Rust | compiled Rust (needs a namespace) |
| `fpe`, `text_redact` | compiled Rust | excluded (off the key surface) |
| `categorical` (deterministic) | compiled Rust | excluded (off the key surface) |
| `bucket_perturb` (explicit `date_format`) | compiled Rust | excluded (off the key surface) |
| `redact`, `truncate` | named-Arrow | named-Arrow |
| `passthrough` | no-op | no-op |
| `text_mask` | dedicated Python (per-cell loop; backlog: route through the span kernel) | excluded (off the key surface) |
| `code_set` (mask mode without `chapter_preserve`) | dedicated Python (no kernel on any route; backlog: Arrow dict-map) | excluded (off the key surface) |
| `faker` (deterministic REUSE, exact-string, person-name provider) | compiled index selection | excluded (off the key surface) |
| `date_shift`, `bucketize` | declined (`out_of_core_row_error_strategy_unsupported`) | excluded (off the key surface) |
| `categorical` (non-deterministic) | declined (`out_of_core_categorical_nondeterministic_unsupported`) | excluded (off the key surface) |
| `geo_generalize` | declined (`out_of_core_whole_column_aggregation_unsupported`) | excluded (off the key surface) |
| `formula`, `derived` | declined (`out_of_core_dynamic_output_type_unsupported`) | excluded (off the key surface) |
| `nested` | declined (`out_of_core_child_dispatch_unsupported`) | excluded (off the key surface) |
| `grouped_series`, `windowed_date`, `derived_aggregate`, `group_key`, `joint_mask` | declined (`out_of_core_cross_row_strategy_unsupported`) | excluded (off the key surface) |
| `shuffle`, `top_code` | declined (`out_of_core_strategy_unsupported`) | excluded (off the key surface) |

These cells describe main. A later slice (B1) changes the `date_shift`/`bucketize` cells after
its own merge. A `bucket_perturb` payload with no explicit `date_format` declines separately
with `out_of_core_bucket_perturb_autodetect_unsupported`; whole-column format auto-detection
does not chunk.

Parent-key masking and orphan remapping have separate compute paths. Native hash masks
admitted parent batches. Hash remapping currently uses the Python reference. Other payload
strategies are excluded as parent-key strategies, even when their payload implementation is
native.

On this route a composite FK whose child columns would be masked independently as scalars is
declined (`out_of_core_composite_fk_scalar_child_unsupported`); the canonical
`composite_fk_group` unit is admitted and pinned strictly against the full-frame oracle
(`tests/parity/test_phase_d_dcomp_composite_ooc*.py`; the recorded differences are in
`tests/parity/SEMANTIC_DIFFERENCES.md`). A composite chain, a composite child used as a parent
key, is declined (`out_of_core_composite_fk_chain_unsupported`) and runs on the sequential
route; a `uint64` or `decimal` key component fails closed at the joiner
(`out_of_core_fk_key_dtype_unsupported`). A multi-parent child (more than one parent targeting
the same child FK column tuple) is admitted: the parents' key relations are merged
first-hit-wins in relationship order and the child is joined once, matching the full-frame
oracle (`tests/parity/test_phase_d_dmp_multi_parent_ooc*.py`); parents whose masked key types
differ fail closed (`out_of_core_fk_key_dtype_unsupported`) and the router keeps such a job on
the sequential route. Distinct child keys referencing different parents are supported as before.
A-9 delivered the admitted composite and multi-parent FK shapes. It did not schedule OOC
`faker` or `date_shift`. Composite scalar children, composite chains, and unsupported key
dtypes retain the restrictions above.

OOC also declines when predicates, unsupported non-scalar work, self-referential relationships,
and table-level cycles. The public supported-strategy set is only a coarse payload list. It does
not establish job admission or native dtype support.

#### Why the out-of-core route declines some payload strategies (known limitations, never a silent no-op)

Every decline above is a caller-visible coded reason, and the job runs the strategy on the
full-frame (or sequential) oracle instead; the out-of-core route never silently passes a column
through or drops a strategy. Some declines are inherent to a single-pass, per-column batch
stream and are not a near-term wiring gap:

- `faker` (`out_of_core_faker_pool_unsupported`): realistic name/address/value replacement draws
  from a registry-backed value pool plus a cross-batch pool cache, which the registry-free
  streaming kernel has no channel for. So realistic replacement does **not** run at out-of-core
  scale today; this is a known limitation, not a silent no-op. A streamable value-pool design is
  a separate V2 item.
- `geo_generalize` (`out_of_core_whole_column_aggregation_unsupported`): its k-anonymity cascade
  thresholds each row's generalization level on whole-dataset counts (ZIP5/ZIP3/state or H3
  resolutions), which a single chunk cannot see.
- `group_key`, `windowed_date`, `grouped_series`, `derived_aggregate`, `joint_mask`
  (`out_of_core_cross_row_strategy_unsupported`): each needs state the per-column kernel does not
  receive -- other columns of the same row (`group_key`), a durable global row ordinal
  (`windowed_date`), a per-group sequential stream (`grouped_series`), an intra-table aggregate
  (`derived_aggregate`), or a reference-table row (`joint_mask`). Admitting them needs a bounded
  relational lowering not yet built.
- `shuffle` (`out_of_core_strategy_unsupported`): a whole-column keyed permutation is
  order-sensitive and non-partitionable by construction, so it cannot stream per batch.
- `bucketize` / `date_shift` (`out_of_core_row_error_strategy_unsupported`): row-local in value,
  but they record per-value format errors that the full-frame path removes via a quarantine
  pass; the streaming route has no row-error/quarantine channel (`date_shift` without an
  explicit `date_format` also needs whole-column format detection). `top_code` is in the same
  class and declines through the generic surface for the same missing channel.
- `formula` / `derived` (`out_of_core_dynamic_output_type_unsupported`) and `nested`
  (`out_of_core_child_dispatch_unsupported`): their output Arrow type is not knowable from the
  plan alone (or they re-enter the full pandas child-strategy dispatch), which the fixed-schema
  streaming writer cannot satisfy.

These verdicts are pinned by `tests/parity/test_out_of_core_group_b_parity.py` and
`tests/parity/test_out_of_core_group_c_parity.py` (each asserts the exact coded reason), so a
future change that silently admitted one would fail the suite rather than leak a divergence.

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

### Native admission qualifications

The route tables above identify available native implementations, not unconditional admission.
Each cell is further qualified by the guards below.

| Strategy | Exact qualification to document |
| --- | --- |
| `hash` | SEQ/OOC native hashing admits string, large_string, bool, and signed/unsigned integers. Timestamp types decline there. Chunked also admits timezone-aware timestamps through its shared predicate, subject to other guards. Namespace requirements remain enforced. |
| `fpe` | Native payload requires exact string, namespace, valid FPE configuration, and no checksum option. SEQ keys additionally admit supported integer representations. Do not imply all integer payloads are native. |
| `categorical` | SEQ/OOC native selection requires deterministic mode, namespace, exact-string source, string categories, and a buildable weighted CDF. Seeded positional categorical is separately native on chunked. OOC rejects its non-deterministic mode. |
| `bucket_perturb` | OOC requires explicit `date_format` at route admission. Native execution further requires exact string, namespace, supported bucket and format, no timezone or special pandas format, and valid preserve_values. Runtime range guards still apply. |
| `date_shift` | Native SEQ/chunked requires exact string, namespace, explicit non-special timezone-free format, no group_by, integer day bounds within the accepted range, and valid preserve_values. Main OOC rejects the entire strategy. |
| `faker` | Providers are person_first_name and person_last_name on these native paths. SEQ admits exact string with deterministic REUSE or positional REUSE. Chunked additionally admits bool/integer deterministic REUSE with its fixed-schema producer guarantee and accepted conversion metadata. Positional bool/integer/float REUSE requires explicit pool_size. Other modes and providers use their existing Python path. OOC admits the deterministic-REUSE person-name variant with an exact-string source through the compiled index selection (run-scoped pool); other faker shapes, and a non-string source, decline. |
| `text_redact` / `text_mask` | Native paths have source/configuration guards. NER remains Python. OOC text_mask currently uses its dedicated loop, including batched NER inference. |
| `code_set` | OOC admits mask mode without chapter_preserve. Gen mode and chapter_preserve remain route declines. There is no named Arrow code-set kernel on main. |
| `group_key` | SEQ has no native admission. Native chunked requires an exact string/int64/bool sibling, an unmasked non-self sibling, and valid even length in [8,64]. Unified full-frame also has a native implementation. |
| `redact` / `truncate` / `passthrough` | Keep the named-Arrow/no-op classifications, but do not promise arbitrary dtype/schema identity beyond each route's existing contract. |

Masked nullable integer columns under `truncate` or `categorical` can fail with
`null_bearing_int_unsupported`. FK children resolved through relationships are exempt.

### Dedicated-Python exceptions (the documented set)

These run their own Python handler by design, not the generic fallback:

- `code_set`: no native kernel on any route. It remaps each value through a required corpus
  state table (a keyed corpus lookup), which no per-value Arrow/Rust kernel carries today, so it
  runs its dedicated Python handler everywhere; backlog: kernelize as a named-Arrow dict-map that
  threads the corpus. Its chunked and out-of-core support is this same mask-mode handler, admitted
  for mask mode without `chapter_preserve`; `gen` mode and `chapter_preserve` decline
  (`out_of_core_code_set_shape_unsupported`, pinned by
  `tests/parity/test_out_of_core_group_c_parity.py`).
- NER (the `ner` config of `text_redact` / `text_mask`): Python by design (spaCy inference).
- `faker` masking runs as compiled index selection only on its native legs. Providers are
  person_first_name and person_last_name on these native paths. SEQ admits an exact-`string`
  source with deterministic REUSE or positional REUSE; chunked additionally admits bool/integer
  deterministic REUSE with its fixed-schema producer guarantee and accepted conversion metadata.
  It stays on its Python handler when the provider is not on the native allowlist, when a
  deterministic config is not the REUSE variant or carries no namespace (the oracle rejects it),
  when a non-deterministic mode other than REUSE is used (UNIQUE, MATCH, and SCALE need
  whole-column RNG state), or when the source type is outside the native leg's domain. A faker
  column that stays on Python is still pool-backed: the handler builds and caches one value pool
  and samples it, not per-row live-provider calls. OOC admits the
  deterministic-REUSE person-name variant with an exact-string source through the
  compiled index selection, drawing from a run-scoped pool; other faker shapes and
  a non-string source decline (`out_of_core_faker_pool_unsupported` at routing, or
  a fail-closed `out_of_core_faker_source_type_unsupported` for a non-string
  source once the schema is seen).
- out-of-core `text_mask`: the per-cell loop; the SEQ and native-chunked routes already use the
  Rust span kernel (backlog: route OOC through it too).
- the shared SEQ handlers listed in the sequential table (`bucketize`, `shuffle`, `top_code`,
  `formula`, `derived`, `derived_aggregate`, `nested`, `joint_mask`, `geo_generalize`,
  `grouped_series`, `windowed_date`, `group_key`): full-frame handler by design, with no SEQ
  native admission. `group_key` is the one of these with a native path on another route: it has
  native implementations on the native-chunked route (above) and on eligible unified full-frame
  execution.

## Generation

Generation computes every value on Python and NumPy and never on the Rust companion. The Rust
generation-kernel-selection experiment (FR3) was shelved after it measured slower than
vectorized NumPy RNG, so this is by design, not a gap. The compiled index kernel is reachable
only from masking's deterministic pool path; no generation column enters it. PoolSampler's
compiled deterministic selection branch serves masking. Generation's pool call sets
deterministic=False and REUSE. The streamed pooled selector also uses NumPy integers. Per-kind
compute:
`sequence` and sequential `reference` draw by direct index (no selection event); `categorical`
and random/weighted `reference` draw via Python RNG; pooled `faker` via NumPy selection; per-row
`faker` via live provider calls; `windowed_date` via a NumPy offset draw plus date arithmetic.
The ineligible families run on the legacy whole-table synthesize path with distinct compute:
`formula` is a formula-expression evaluation; `derived` and `derived_aggregate` select nothing
(they are computed from other columns); `statistical`, `grouped_series`, and `group_key` are
whole-table synthesize. Resident and streamed materialization produce byte-identical output for
admitted streamed generation shapes; this does not imply that every legacy whole-table
statistical family streams.

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
- A missing native companion is not a silent slow-path: a native-kernel decline retains the
  current route and uses its reference implementation, or downgrades to the byte-identical
  full-frame oracle, always with a coded reason (a few cells keep a companion-absent Python
  bridge instead).
- Generation speed comes from vectorized NumPy, not the companion; installing the companion does
  not change generation throughput.
