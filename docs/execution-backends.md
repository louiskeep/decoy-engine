# Execution backends and route coverage

What actually computes each masking strategy, by route, and where a shape is
declined with a caller-visible reason instead of run natively. Generation is
covered at the end.

This page is a human-readable reading of the authoritative, test-enforced sources:
the per-operator compute classification in
[`records/2026-10-16-d-audit-compute-matrix.md`](records/2026-10-16-d-audit-compute-matrix.md)
(masking) and the generation compute audit
[`plans/2026-10-19-phase-f-f-audit-no-legacy.md`](plans/2026-10-19-phase-f-f-audit-no-legacy.md).
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
- **dedicated Python**: a named handler or primitive with no Rust or Arrow kernel. This
  is not the generic pandas fallback for an admitted kernel cell; it is the designed
  compute for that strategy on that route.
- **declined (caller-visible reason)**: the shape is not admitted on that route and is
  rerouted, with a coded reason, to the full-frame pandas oracle. Never a silent fallback.
- **full-frame oracle**: the pandas reference path. It is the parity oracle for every
  native route and the fallback when the native companion is absent.

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
| `categorical`, `bucket_perturb`, `date_shift` | compiled Rust | declined, reporting-only (Python handler runs) |
| `faker` | compiled index selection | declined |
| `text_redact`, `text_mask` | compiled Rust spans, Python splice | declined |
| `redact`, `truncate` | named-Arrow | named-Arrow |
| `passthrough` | no-op | no-op |
| `bucketize`, `shuffle`, `top_code`, `formula`, `derived`, `derived_aggregate`, `nested`, `joint_mask`, `geo_generalize`, `grouped_series`, `windowed_date`, `group_key` | dedicated Python (shared handler, no SEQ native admission) | dedicated Python |

A non-injective but natively-capable strategy used as an FK key is declined with a
reporting-only entry and the Python handler runs; the key surface stays correct.

### Out-of-core route (payload; large FK jobs that spill)

The FK key surface here is only `hash`, `redact`, `truncate`, `passthrough`.

| Strategy | Payload | As an FK key |
| --- | --- | --- |
| `hash` | compiled Rust | compiled Rust (needs a namespace) |
| `fpe`, `categorical`, `text_redact`, `bucket_perturb` | compiled Rust | declined |
| `redact`, `truncate` | named-Arrow | named-Arrow |
| `passthrough` | no-op | no-op |
| `text_mask` | dedicated Python (per-cell loop; backlog: route through the span kernel) | declined |
| `code_set` | dedicated Python (no kernel on any route; backlog: Arrow dict-map) | declined |
| `faker`, `date_shift`, `bucketize`, `geo_generalize`, `formula`, `derived`, `nested`, and the rest | declined (caller-visible reason) | declined |

Composite (multi-column) FK with a scalar child, and multi-parent FK, are declined on
this route today (`out_of_core_composite_fk_scalar_child_unsupported`,
`out_of_core_multi_parent_child_unsupported`); native support for those shapes is a
scheduled V1 engine track.

### Native-chunked route (single-table, chunked)

FK-participating tables on either side reroute to the oracle
(`fk_relationship_not_native_route`), so no FK cell runs here. Non-FK payload follows the
operator registry: `hash`, `categorical`, `bucket_perturb`, `date_shift`, `fpe`,
`group_key` on compiled Rust; `faker` as compiled index selection; `redact` / `truncate`
as named-Arrow; `text_redact` / `text_mask` as the compiled span kernel behind an
`arrow_python` operator; `passthrough` as a no-op.

### Dedicated-Python exceptions (the documented set)

These run their own Python handler by design, not the generic fallback:

- `code_set`: no kernel on any route (backlog: kernelize as a named-Arrow dict-map).
- out-of-core `text_mask`: the per-cell loop; the SEQ and native-chunked routes already
  use the Rust span kernel (backlog: route OOC through it too).
- the shared SEQ handlers listed in the sequential table (`bucketize`, `shuffle`,
  `top_code`, `formula`, `derived`, `derived_aggregate`, `nested`, `joint_mask`,
  `geo_generalize`, `grouped_series`, `windowed_date`, `group_key`): full-frame handler
  by design, no native admission on any route.

## Generation

Generation computes every value on Python and NumPy and never on the Rust companion.
The Rust generation-kernel-selection experiment (FR3) was shelved after it measured
slower than vectorized NumPy RNG, so this is by design, not a gap. The compiled index
kernel is reachable only from masking's deterministic pool path; no generation column
enters it. Per-kind compute: `sequence` and sequential `reference` draw by direct index
(no selection event); `categorical` and random/weighted `reference` draw via Python RNG;
pooled `faker` via NumPy selection; per-row `faker` via live provider calls;
`windowed_date` via a NumPy offset draw plus date arithmetic; `formula`, `derived`,
`derived_aggregate`, `statistical`, `grouped_series`, `group_key` run on the legacy
whole-table synthesize path. Resident and streamed materialization produce byte-identical
output.

## What this means for a caller

- If a job lands on a native route, the strategies marked compiled Rust / compiled index
  selection / named-Arrow run natively; the dedicated-Python and declined cells do not,
  and a declined cell always says why.
- A missing native companion is a loud refusal on the native routes, not a silent
  slow-path; the job runs on the full-frame oracle instead, which is byte-identical.
- Generation speed comes from vectorized NumPy, not the companion; installing the
  companion does not change generation throughput.
