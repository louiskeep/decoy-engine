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
| `bucketize`, `top_code` | dedicated Python (per-batch oracle-handler bridge; row-error channel, fails closed on an uncoercible cell) | excluded (off the key surface) |
| `windowed_date` | dedicated Python (per-batch oracle-handler bridge; row-offset channel, keyed on the durable global row ordinal; needs an unmasked/passthrough anchor that is not FK-rewritten) | excluded (off the key surface) |
| `faker`, `date_shift`, `geo_generalize`, `formula`, `derived`, `nested`, and the rest | declined (caller-visible reason) | excluded (off the key surface) |

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
Native support for the declined shapes is a scheduled V1 engine track (A-9).

#### Why the out-of-core route declines some payload strategies (known limitations, never a silent no-op)

Every decline below is a caller-visible coded reason, and the job runs the strategy on the
full-frame (or sequential) oracle instead; the out-of-core route never silently passes a column
through or drops a strategy.

Two channels admit strategies that were previously declined, each running the SAME full-frame
handler on a bounded batch (byte-identical to the oracle), with the runner failing closed on a
row error rather than quarantining (the chunked route's contract; neither non-full-frame lane
quarantines):

- **Row-error channel** (`_row_error_bridge.py`): `bucketize` and `top_code`. Row-local in
  value, they emit per-value `format_error` RowErrors; the runner collects them with an absolute
  (whole-source) row index and fails the job closed via `RowErrorsFailedError` on the first batch
  that reports one. Admitted for a column ONLY when its per-batch Arrow-to-pandas conversion is
  provably identical to the oracle's whole-frame conversion for every batch split
  (`row_error_bridge_batch_stable`): a null-stable plain numpy numeric or string column. A column
  whose pandas dtype depends on null presence or batch content -- a pandas nullable extension
  dtype (`boolean`, `Int64`, `Float64`, ...), a dictionary/categorical type, a null-unstable plain
  numpy bool/int that widens to object/float under nulls, or a decimal/large-offset type -- is
  declined (`out_of_core_row_error_dtype_unsupported`) and runs on the full-frame oracle, which
  converts the whole column at once and is correct. The decline is conservative by construction:
  when per-batch parity cannot be proven, the job never runs on a bare per-batch conversion that
  could emit a value differing from the oracle (if such a job is also too large to fit in memory,
  the standard reject-before-read applies, which is correct -- the alternative is a wrong value).
- **Row-offset channel** (`_offset_bridge.py`): `windowed_date`. Position-keyed; the runner
  threads a durable global row ordinal (the cumulative Phase-1 source-order row count, equal to
  the row's whole-source position on both drivers) so each row keys identically regardless of the
  batch boundary. Admitted only with an unmasked or passthrough anchor
  (`out_of_core_windowed_date_masked_anchor_unsupported` otherwise) that is not itself a child FK
  column: an FK-rewritten anchor is read post-resolution by the oracle but pre-resolution by the
  bridge, a wrong-value divergence, so it declines
  (`out_of_core_windowed_date_fk_anchor_unsupported`).

Some declines remain inherent to a single-pass, per-column batch stream and are not a near-term
wiring gap:

- `faker` (`out_of_core_faker_pool_unsupported`): realistic name/address/value replacement draws
  from a registry-backed value pool plus a cross-batch pool cache, which the registry-free
  streaming kernel has no channel for. So realistic replacement does **not** run at out-of-core
  scale today; this is a known limitation, not a silent no-op. A streamable value-pool design is
  a separate V2 item.
- `geo_generalize` (`out_of_core_whole_column_aggregation_unsupported`): its k-anonymity cascade
  thresholds each row's generalization level on whole-dataset counts (ZIP5/ZIP3/state or H3
  resolutions), which a single chunk cannot see.
- `group_key`, `grouped_series`, `derived_aggregate`, `joint_mask`
  (`out_of_core_cross_row_strategy_unsupported`): each needs state the per-column kernel does not
  receive -- other columns of the same row (`group_key`), a per-group sequential stream
  (`grouped_series`), an intra-table aggregate (`derived_aggregate`), or a reference-table row
  (`joint_mask`). `group_key` is a near-term extension of the row-error bridge (a sibling-column
  read restricted to an unmasked/passthrough group_by); the rest need a bounded relational
  lowering not yet built.
- `date_shift` (`out_of_core_row_error_strategy_unsupported`): row-local in value and a row-error
  strategy like bucketize, but it also needs whole-column format detection without an explicit
  `date_format`, and its `group_by` mode anchors on a whole-table snapshot; admitting it with an
  explicit format and no group_by on the row-error bridge is a near-term extension.
- `shuffle` (`out_of_core_strategy_unsupported`): a whole-column keyed permutation is
  order-sensitive and non-partitionable by construction, so it cannot stream per batch.
- `formula` / `derived` (`out_of_core_dynamic_output_type_unsupported`) and `nested`
  (`out_of_core_child_dispatch_unsupported`): their output Arrow type is not knowable from the
  plan alone (or they re-enter the full pandas child-strategy dispatch), which the fixed-schema
  streaming writer cannot satisfy.

These verdicts are pinned by `tests/parity/test_out_of_core_group_b_parity.py` and
`tests/parity/test_out_of_core_group_c_parity.py` (each asserts the exact coded reason), so a
future change that silently admitted one would fail the suite rather than leak a divergence. The
admitted bridges are pinned by `tests/parity/test_out_of_core_row_error_bridge.py` (byte-parity +
fail-closed + the plain-numeric column stays admitted and the extension/null-unstable columns
decline with `out_of_core_row_error_dtype_unsupported`) and
`tests/parity/test_out_of_core_offset_bridge.py` (byte-parity + batch-size invariance + the
masked-anchor decline).

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

- `code_set`: no native kernel on any route. It remaps each value through a required corpus
  state table (a keyed corpus lookup), which no per-value Arrow/Rust kernel carries today, so it
  runs its dedicated Python handler everywhere; backlog: kernelize as a named-Arrow dict-map that
  threads the corpus. Its chunked and out-of-core support is this same mask-mode handler, admitted
  for mask mode without `chapter_preserve`; `gen` mode and `chapter_preserve` decline
  (`out_of_core_code_set_shape_unsupported`, pinned by
  `tests/parity/test_out_of_core_group_c_parity.py`).
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
