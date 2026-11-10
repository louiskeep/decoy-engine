# Execution backends and route coverage

What actually computes each masking strategy, by route, and where a shape is
declined with a caller-visible reason instead of run natively. Generation is
covered at the end.

This page is a human-readable reading of the authoritative, test-enforced sources:
the per-operator compute classification in
[`records/2026-10-16-d-audit-compute-matrix.md`](https://github.com/louiskeep/decoy-engine/blob/main/docs/records/2026-10-16-d-audit-compute-matrix.md)
(masking) and the generation compute audit (the
[F-audit plan](https://github.com/louiskeep/decoy-engine/blob/main/docs/plans/2026-10-19-phase-f-f-audit-no-legacy.md)).
The enumerated strategy/provider list is [`capability-matrix.md`](capability-matrix.md);
install and wheel/ABI facts are [`native/supported-matrix.md`](native/supported-matrix.md).
If this page and those sources ever disagree, the sources win: they are derived from
the dispatch constants and guarded by parity tests, this page is prose.

The ExecutionAdapter substrate is pandas-only and there is no setting for it. Execution routes
are separate from the substrate. Rust and Arrow kernels can run inside those routes.

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
`execution_mode` can force a supported route. A caller does not select a masking
backend. Default byte-estimate routing can select OOC below five million
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

## Route decision order (decide_execution_route)

This section is the route-decision narrative of
[`_pipeline_routing.py`](../src/decoy_engine/execution/_pipeline_routing.py).

### The two routing layers

Two independent routing layers compose in `_pipeline_routing.py`, in a fixed decision order
`run_pipeline` enforces by calling them in sequence:

1. S2 relationship routing + SC2 out-of-core auto-routing:
   `decide_execution_route` is the SINGLE live router for
   relationship-bearing (FK) jobs -- it runs first and early-returns
   before the layer-2 chunk classifier is ever consulted for routing.
   For a relationship-bearing PURE-MASK job (no generate tables, no
   validators, no fidelity_report, no vault_writer -- see
   `_sequential_eligible`) under `execution_mode="auto"` it picks, in
   priority order (SC2):

   - a. **out_of_core** -- the bounded-RAM DuckDB route -- when the job is
        out-of-core-compatible (`check_out_of_core_compatibility` admits
        the plan: an accepted strategy set, an acyclic table-level FK
        graph, no `when` predicate, single-parent-per-child) AND the
        largest mask table is at/above `out_of_core_threshold_rows`.
        `_pipeline_route_exec.run_out_of_core_route` executes + packages it.
        Inside that route, a sink-path table auto-selects the reorder driver
        over `_batch_join` once its largest incoming edge's deduped
        parent-key count reaches `out_of_core_reorder_threshold_rows`
        (default 2M; `0` forces every eligible table, per-run override) --
        an independent, narrower-scoped threshold from this layer's own
        `out_of_core_threshold_rows` (see `_route_policy.decide_route`).
   - b. **sequential** -- the bounded-memory (but O(cardinality), so still
        OOM-able at extreme scale) `run_sequential` path -- for a
        pure-mask FK job that is NOT out-of-core-eligible (an unsupported
        strategy, a self-referential / cyclic FK, a multi-parent child)
        or is below the out-of-core size threshold.
        `_pipeline_route_exec.run_sequential_route` executes + packages it.
   - c. **full_frame** -- the adapter-selected `run` path -- for a job with
        no relationships, and for a relationship job disqualified from
        sequential (generate+mask, validators, fidelity, vault,
        non-pandas substrate) that is small enough to be full-frame-safe.

   And it REJECTS before the read/mask step (fail-closed
   `ExecutionError` code `fk_full_frame_oom_risk_rejected`) when a
   relationship job can ONLY run full-frame (neither out-of-core- nor
   sequential-eligible) AND the largest mask table is at/above
   `full_frame_reject_rows` -- the "never a silent OOM" half of GATE-1's
   prevention posture: a too-big job that no bounded route can take is
   diverted to a clean typed error, not left to the OOM-killer.

   A generate+mask job whose static shape `_mixed_static` admits takes a fourth route under
   `auto`, `mixed`: generation stays resident and the mask tables run on the chunked lane
   (`_pipeline_mixed_route`). Every job it declines keeps the outcomes above unchanged.

   `execution_mode="full_frame"` always forces full-frame (even when
   eligible, even when large -- an explicit operator escape hatch that
   bypasses the reject: the operator owns the OOM risk they asked for);
   `execution_mode="sequential"` forces sequential and
   `execution_mode="out_of_core"` forces out-of-core, each raising
   `ConfigError` if the job is not eligible for the forced route
   (fail-closed: never silently ignore an explicit request). The
   sequential path is pandas-only by construction (`run_sequential` is
   typed to `PandasExecutionAdapter`), so it always constructs its own
   adapter regardless of the caller's `substrate` knob; the out-of-core
   route is likewise pandas-oracle-parity by construction.

   `execution_mode="stream"` means "must stream" (RUN-STREAM E1a). `run_pipeline` translates it once to
   `auto` plus an immutable `stream_required`, and `decide_route` hands that run to
   `execution/pipeline/_stream.py` instead of the two layers. A preflight over config, plan and source
   footers takes the chunked lane (one table) or the B7 split (several) when every mask table passes
   `check_chunked_compatibility`, with the size threshold dropped and the Parquet row groups kept at
   one per chunk. A job with FK edges whose tables all pass that gate runs as per-table lane units; one
   the gate rejects takes the out-of-core route only when out-of-core accepts it and the FK dtype hazard
   is clear. Anything else raises `runtime.streaming_unavailable` before the passed `sources` are read.
2. S3 (engine-efficiencies P3) auto-chunk routing: reached only when (1)
   did NOT take the sequential early return -- every path into
   `decide_chunk_route` is either explicitly forced full_frame or was
   found sequential-ineligible, relationship-bearing jobs in particular.
   `classify_job`'s own `chunked` gate independently excludes any job
   with FK edges or a `relationships` block (`_planner._chunked_rejection`),
   so a relationship-bearing job that reaches this layer (e.g.
   generate+mask FK, or an FK job disqualified from sequential by
   validators) cannot be misrouted into the chunked path either -- the
   two layers compose without overlap: (1) owns relationship routing
   (sequential vs. full_frame); (2) owns non-relationship routing, single
   table in `_pipeline_routing.py` and per table in `_pipeline_multi_table` (B7). When the job classifies `chunked`,
   `_pipeline_route_exec.run_mask_chunked` runs it in `chunk_size_rows`-row
   slices through the chunked lane (`_pipeline_auto_chunk`: B1's
   `run_mask_chunked`, or the oracle under the kill switch) instead of one
   full-frame adapter call; masked values are unchanged (the output shape
   follows guarantee 3 of docs/plans/2026-10-01-dispatcher-auto-chunk.md)
   and every eligibility miss fails CLOSED to the exact full-frame path.

Row-error honesty (D7/D8, S1): the chunked route is never eligible for
row-error quarantine -- both lanes fail closed with `RowErrorsFailedError`
(the oracle's H1 check in `_chunked.py`) the moment ANY chunk reports a row
error, so `run_pipeline` never sees row errors to quarantine on that route
(same policy the pre-existing manual chunked entrypoint already enforced).

Sprint B1b (OOM-avoidance routing redesign, docs/plans/2026-07-10-oom-avoidance-routing-redesign.md
 §13): `decide_execution_route`'s
`use_byte_estimate_routing` flag (TB-5 default ON) wires the B1a byte-level
estimator (`_mem_estimate.fits`) into the "auto" full_frame-admission
decision, additively -- see the flag's entry under `decide_execution_route`
below for the exact scope and rule. Forced OFF (the rollback path), `_pipeline_routing.py`'s
routing is BYTE-FOR-BYTE the pre-B1b row-count logic described above.

### `decide_execution_route`

`decide_execution_route` decides `(route, route_reason)` -- `"out_of_core"`, `"sequential"`,
`"mixed"`, or `"full_frame"` -- or RAISES a fail-closed reject-before-read.

`mixed_admitted` (caller-precomputed by `_mixed_static.admit_mixed_static`): a
generate+mask job whose static shape the production mixed route can run. It is
consulted only under `auto`, after the forced modes returned or raised, so
`full_frame` still runs the oracle and `out_of_core` / `sequential` still reject a
mixed job with the existing typed `ConfigError`. A mixed job it does not admit keeps
every existing outcome below, including the fail-closed reject of a large coupled job
no bounded route can take.

`use_byte_estimate_routing` (Sprint B1b; TB-5 flipped the default to
`True`, additive + forceable OFF for rollback): OFF, `decide_execution_route` is
BYTE-FOR-BYTE the row-count logic below -- `full_frame_fits_estimate` is
never read, the rollback path. ON (the default), for a job IN SCOPE
(`has_relationships and has_mask_table and not has_generate_table` --
see `byte_estimate_full_frame_fits`'s docstring for why generate+mask is
excluded), the "auto" decision is REPLACED by the §13 conservative-filter
ruling: full_frame only when `full_frame_fits_estimate is True` (the
caller precomputes this via `byte_estimate_full_frame_fits` +
`resolve_budget`) AND no bounded route applies -- a bounded-eligible
(eligible, acyclic) job takes its bounded route even when it fits, because
the full-frame oracle is the slowest route per row; otherwise -- UNPRICEABLE
(`None`) treated identically to `False`, per §13 -- the job routes to whichever BOUNDED path
eligibility admits (out_of_core when compat-admitted, else sequential),
or the existing reject when NEITHER applies (§13's one irreducible reject
class). The choice AMONG bounded routes stays eligibility-based, never an
estimate of out_of_core/sequential peak (unmeasured placeholders, §13);
`out_of_core_threshold_rows` is not consulted in this mode. Out of scope,
the flag has NO effect, even when `full_frame_fits_estimate` is set.

Sprint B2 (§3.3/§11/§13): `use_probe_routing` (TB-5 default `True`,
additive, composes with `use_byte_estimate_routing` -- it has NO effect
unless that flag is also `True`) is the fast-path RECOVERY for a job the
conservative
B1b estimate over-downgrades. `probe_recovers_full_frame` is the caller's
precomputed `_pipeline_routing_signals.resolve_probe_recovery` result:
`True` only when the two-point micro-probe (`_probe.probe_peak_bytes`)
actually MEASURED this job's real full_frame peak (at small scale,
extrapolated) and confirmed it clears the budget with margin. Checked
ONLY after `full_frame_fits_estimate is True` fails to admit the job (a
confirmed static fit never needs a probe) -- when it is `True`,
`decide_execution_route` returns `full_frame` even for a job that would otherwise be
out_of_core-eligible: a MEASURED fit beats a bounded-by-eligibility
default, because full_frame is faster and the probe's `True` means the
measurement, not a model, backs the decision. Anything other than `True`
(`False` or `None` -- an inconclusive probe, or one that was never run
because it was out of scope/unnecessary) falls straight through to the
exact same bounded-by-eligibility logic below as if the probe did not
exist -- an inconclusive probe NEVER yields full_frame, only a
CONFIRMED fit does (the load-bearing safety property: recovery only on
a measured fit).

`fk_ooc_dtype_hazard` (caller-precomputed from the source schemas by
`_pipeline_routing_fk_dtype`): out-of-core would reject a key type the oracle
completes, so a fitting job with it takes sequential rather than out-of-core.

`vault_ooc_safe` (D3b, caller-precomputed by `out_of_core._vault_collect.vault_ooc_safe`):
every vaulted column is on the reviewed allowlist, string-typed in and out, and not an
incoming FK child key. Such a job is otherwise a pure-mask job, so it takes the
out-of-core route (which collects the vault per batch) instead of full-frame-or-reject.
It never enables sequential, which has no vault collector, and the byte-estimate flag
OFF restores the old behaviour.

`validators_ooc_safe` (D3a, `out_of_core._validate_batch.validators_ooc_safe`): every
validator is a per-cell check on a route-stable string column, with no quarantine. Such a
FITTING job takes out-of-core; a probe-recovered job still routes full-frame, and sequential
stays closed to validators.

Priority (SC2), for a relationship-bearing PURE-MASK job under `auto`:
out-of-core (when compat-admitted AND large) > sequential (bounded but
O(cardinality)) > full-frame. A large relationship job that no bounded
route can take is REJECTED before read (`ExecutionError`
`fk_full_frame_oom_risk_rejected`) rather than left to OOM full-frame.

Out-of-core eligibility is a STRICT SUBSET of sequential eligibility: it
needs the pure-mask FK shape (`_sequential_eligible`), an acyclic
single-parent FK graph with only supported strategies
(`out_of_core_compatible`), AND a largest mask table at/above
`out_of_core_threshold_rows`. So a pure-mask FK job with an unsupported
strategy, a cyclic/self-referential FK, or below the size threshold keeps
taking the existing sequential route; only large, fully-supported FK jobs
divert to streaming.

`largest_table_rows` carries the largest mask table's row count from the
(SC7a bounded) profile metadata even on the lazy `source_loader` path where
no source is resident -- so the SC2 size gates fire there too, closing the
F2 `None`-on-lazy-path hole. `largest_table_rows_exact` is False when that
count is a CSV byte-size estimate (CSV has no footer); an estimated
full-frame-bound table at/above `full_frame_reject_rows` is rejected with
the distinct `fk_full_frame_oom_risk_rejected_estimated` code (convert to
Parquet or set `execution_mode`), while an estimated OOC-eligible table
still reroutes to out-of-core as usual. `largest_table_rows` is None only
when there is genuinely no size signal (no mask table); the size gates then
never fire.

A mutual cross-table FK cycle (A -> B -> A) cannot be ordered by
table_topo_order, so `auto` must not route it to sequential (it ran
fine under full_frame before this program; routing it to sequential is
a functional regression, not a leak -- see
docs/backlog/s2-fk-leak-remediation-r3-guide.md section 6). A
self-referencing table (one table, not a cross-table cycle) is NOT
flagged and still routes normally.

`resolved_substrate` disqualifies sequential eligibility when it is
not `"pandas"` (see `_sequential_eligible`'s docstring): pandas is the only
masking substrate, so any other resolved value routes to full_frame
(fail-closed) rather than the pandas-only sequential path, and a future
non-pandas substrate would need its own explicit handling first.

Threshold ordering (L2): the reject-before-read branch reads most naturally
when `out_of_core_threshold_rows <= full_frame_reject_rows`, and that is the
default (5M <= 7.5M). The two knobs are validated positive+typed at the
`run_pipeline` submit choke point but their ORDERING is deliberately NOT
force-validated, because an inverted override
(`full_frame_reject_rows < out_of_core_threshold_rows`) is a legitimate,
SAFE config -- e.g. a small-RAM host that rejects full-frame-bound FK jobs
early while leaving the out-of-core reroute at its default. The reject
branch's correctness does not actually depend on the ordering: an
out-of-core-eligible job can NEVER reach the reject branch regardless of
threshold order, because out-of-core eligibility is a strict subset of
sequential eligibility, and a sequential-eligible non-cyclic job takes the
`sequential` early return in `decide_execution_route` before the reject test is evaluated. The
reject branch is reached ONLY by full-frame-bound jobs (not
sequential-eligible, or a cyclic FK graph), which no bounded route can take
at any threshold ordering. So the ordering is an explanatory expectation,
not a load-bearing invariant, and is left to the operator rather than
rejected at submit time.

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
