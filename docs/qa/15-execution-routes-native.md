# 15. Execution routes and the native companion

Status: plan

## What it covers

How a job is run: the full-frame pandas route (the reference), automatic chunking for
large single tables, the sequential route for FK jobs, the out-of-core DuckDB route for
large FK jobs, the unified native slice, and the optional Rust companion
(`decoy-engine-native`). Also capacity estimates and the `execution_mode` override.

## Expected behaviour

- The caller picks nothing by default. `execution_mode="auto"` chooses a route from the
  job's shape, size and memory signals.
- **Every route gives the same output as the full-frame route**, byte for byte, for the
  same config, key and input. This is the main promise of this section.
- Chunk size does not change the output.
- The Rust companion changes speed, not values. Without it, affected tables reroute to
  the Python path before any output is written, with a coded reason. It is never a
  silent mid-run switch.
- The companion must report ABI tag `decoy-native-abi-3` exactly and pass its
  known-answer tests, or it is treated as absent.
- Forcing a route the job cannot use raises an error. It does not quietly run another
  route.
- A route decline always carries a reason the caller can read in the result.
- Generation never uses the companion.
- A job too large for full-frame memory is rejected or routed elsewhere, not run until
  the machine dies.

## Checks

| Id | Run this | What should happen |
|---|---|---|
| ROUTE-01 | One single-table mask job (hash, fpe, redact, truncate, faker) run with `execution_mode` full-frame and with auto-chunking forced at a small `chunk_size_rows`. | Byte-identical output tables. |
| ROUTE-02 | ROUTE-01 at three different chunk sizes. | All identical. |
| ROUTE-03 | An FK job run full-frame, sequential and out-of-core. | Identical outputs, joins preserved in all three. |
| ROUTE-04 | Every job in ROUTE-01 and ROUTE-03 with the companion installed and uninstalled. | Identical outputs. The result records which backend ran each column. |
| ROUTE-05 | `native_companion_status()` with the companion present, absent, and with a build reporting a different ABI tag. | `present-ok`; `absent`; `abi-mismatch`. In the last two, masking still runs on the Python path with a coded reason. |
| ROUTE-06 | Hash a value containing a codepoint newer than the running Python's Unicode version, with and without the companion. | Record whether the tokens differ. The docs say they can, and that the companion result is canonical. |
| ROUTE-07 | `execution_mode="out_of_core"` on a job using a strategy that route does not support. | Refused with a registered reason naming why. No fallback to another route. |
| ROUTE-08 | `execution_mode="stream"` where no streaming route applies. | Refused with `runtime.streaming_unavailable` before any source is read. |
| ROUTE-09 | `explain_plan=True` on a chunked job and on a full-frame job. | The result states the route and lane chosen and the reason. |
| ROUTE-10 | `estimate_job` and `estimate_job_capacity` on a job well beyond available memory. | A verdict that the job does not fit full-frame. Running it under `auto` picks a bounded route or refuses; it does not exhaust memory. |
| ROUTE-11 | A large generate job with and without the companion. | Same output and comparable speed. |
| ROUTE-12 | A managed (bundle) CLI install with the companion removed afterwards. | Every command except `upgrade`, `--version` and `--help` refuses with exit code 3 and points at `decoy upgrade --force`. |

## Open points

- The companion README lists two kernels (keyed hash, faker index). The status probe
  also checks FPE and raw-hex kernels. Update the README so QA knows which strategies
  should show a native backend.
- The auto-chunk guarantee has listed exceptions: schema, passthrough types, nullability
  and metadata follow "guarantee 3" of the dispatcher plan, and passthrough columns are
  exact source values on the chunked route but go through pandas on full-frame. So
  "byte-identical" holds for masked values, and passthrough columns can legitimately
  differ in type. ROUTE-01 should compare masked columns strictly and record passthrough
  differences separately.
- `bucket_perturb` on the out-of-core route fails for dates within one bucket of the
  pandas timestamp bounds when the companion is present, and not when it is absent. That
  is a route-dependent outcome; confirm it is acceptable.
- The CLI bundle guards the companion; a pip install does not. A pip user gets the slow
  path with only a coded reason in the result. Check the CLI tells them.
