# TB-3 results: local cgroup-capped validation of the OOM-avoidance router

- **Status:** COMPLETE, all three proofs GREEN (2026-07-13).
- **Sprint:** TB-3 of `docs/plans/2026-07-12-track-b-completion-program.md`.
- **Design:** `docs/plans/2026-07-10-oom-avoidance-routing-redesign.md` (local
  fallback: 4-6M rows under a cgroup cap on the devbox reproduces the
  ratio/parity shape); §9 acceptance is what this validates.
- **Harness:** `scripts/tb3_cgroup_validation.py` (manual/gated, not default-CI).
- **Spend:** ZERO GCP. Entirely on the devbox under a real kernel cap.

TB-3 is the confidence gate before enabling Track B flags (TB-5) and before
paying for GCP (TB-6). It re-proves, under a **real kernel `memory.max` cgroup
v2 cap**, the three properties TB-2 proved only under the governor's in-process
RSS monitor. The distinction matters: production (k8s, cgroup-limited VMs)
enforces memory with the kernel OOM killer, not our watchdog, so the design has
to survive a real hardware ceiling.

## Mechanism (real kernel cap) and proof it bites

Each engine job runs inside a transient `sudo systemd-run` **service** under a
hard cgroup v2 cap:

```
sudo -n systemd-run --wait --pipe --collect --quiet \
  -p MemoryMax=<N>M -p MemorySwapMax=0 -p OOMPolicy=continue \
  -p User=<me> -p Group=<me> -p WorkingDirectory=<repo> \
  <.venv python> scripts/tb3_cgroup_validation.py --worker <mode> ...
```

Three mechanism decisions, each load-bearing:

- **Service, not `--scope`.** `--scope` runs synchronously in the caller's
  process context and does not cleanly host a workload that itself spawns a
  supervised child subprocess (the governor's isolated worker) -- it SIGTERMs
  the tree early (observed: governor died at ~4 s before any trip). A transient
  service reparents to the systemd manager, so the governor's
  spawn/kill/reroute lifecycle runs intact.
- **`OOMPolicy=continue`.** systemd's default `OOMPolicy=stop` tears down the
  WHOLE unit (killing the governor parent) the instant the kernel OOM-kills any
  member -- exactly the `full_frame` child the governor *wants* killed so it can
  reroute. `continue` lets the kernel kill the over-cap child (largest RSS)
  while the parent survives to reroute.
- **`MemorySwapMax=0`.** Makes the cap a true RAM ceiling with no swap escape.

The cap is a cgroup property, enforced regardless of process UID, so `User=<me>`
keeps fixtures/outputs user-owned without weakening the cap.

**Proof the cap bites (control):** a deliberately-too-big allocation (256 MB)
under a tight 64 MB cap is cgroup-OOM-killed (non-zero service exit, 255 on this
host; success marker never printed) *before* any reroute result is trusted.

## Reference host and scale

- **Host:** devbox, pve2 LXC, 4 vCPU / 8 GB RAM, cgroup v2, Linux 6.17.2-1-pve,
  Python 3.10, repo `.venv` (pyarrow 24.0.0, duckdb 1.5.4).
- **Scale chosen: 750,000 rows/table**, 2 tables (parent -> child FK,
  pure-mask hash/redact/truncate: the out-of-core-eligible shape), 6 payload
  string columns/table at ~64-byte cells (~726 MB resident input).
- **Why not the design's 4-6M rows:** on 8 GB under a cap, `full_frame`'s peak
  is ~4.4x raw input, so 4-6M rows would need a >6 GB `full_frame` peak and
  wedge the box (measured: 1.5M rows exceeded even a 6 GB headroom cap and
  thrashed ~290 s). 750,000 rows is the largest scale that cleanly demonstrates
  `full_frame`-exceeds-cap while `out_of_core`-fits without wedge risk:
  `full_frame` peaks ~3.2 GB (3.4x the cap) while `out_of_core` holds ~0.93 GB.
  The proof shape (bytes-vs-budget routing, kernel-enforced reroute, byte
  parity) is scale-invariant; the numbers are reported so a larger GCP re-run
  (TB-6) can be sized against them.
- **Cap for proof 2: 2400 MB.** Sits cleanly above `out_of_core`'s peak and the
  governor parent's resident footprint, and well below `full_frame`'s ~3.2 GB
  peak.

## The three proofs (measured, two independent runs, consistent)

### Proof 1 -- route selection by BYTES vs budget (width test, NOT row-count)

Row count held **constant at 200,000**; only bytes/row changes. Budget fixed at
500 MB (`use_byte_estimate_routing=True`).

| schema (200,000 rows) | bytes/row | routed to |
| --- | --- | --- |
| narrow (1 payload col, ~16 B cells) | small | **`full_frame`** |
| wide (10 payload cols, ~80 B cells) | large | **`out_of_core`** |

Same rows, wider schema crosses the byte budget and flips the route. The
decision keys off computed bytes-vs-budget (`_mem_estimate.fits`), not a row
threshold. **PASS.**

### Proof 2 -- reroute-to-completion under the real cap

Cap = 2400 MB (real kernel `memory.max`). Scale 750,000 rows.

| run | under cap 2400 MB | result |
| --- | --- | --- |
| `full_frame` alone (baseline free peak) | -- | peak **3177 MB** (unbudgeted, generous cap) |
| `out_of_core` alone (baseline free peak) | -- | peak **932 MB** |
| **`full_frame` under the cap** | yes | **kernel-OOM-KILLED** (exit 255, no completion envelope) |
| **`out_of_core` under the cap** | yes | **COMPLETED**, peak 925 MB, FK-consistent |
| **governor (ladder) under the cap** | yes | **trips `full_frame` -> reroutes -> COMPLETES `out_of_core`** |

Governor detail: `trip_kind=self_oom` (the **real kernel** OOM killer
terminated the over-cap `full_frame` child at ~1259 MB observed peak, not our
in-process monitor), `final_route=out_of_core`, `outcome=completed`, completed
`out_of_core` peak 746 MB, and referential integrity intact
(`fk_edge_preserved`, `masking_transformed`, `ids_distinct` all true on 750,000
rows). A job whose `full_frame` peak (3177 MB) exceeds the 2400 MB kernel cap
completes via `out_of_core` under that same cap -- no wedge, no whole-job kill.
**PASS.**

### Proof 3 -- `out_of_core` vs `full_frame` path parity (byte-identity)

For a small job (2000 rows) run through **both** routes, the masked outputs are
byte-identical: matching SHA-256 content hashes for every table (`parent`,
`child`), over the same `to_pydict()` canonical form
`tests/parity/test_out_of_core_routing_parity.py` compares. No documented
FK-typing divergence surfaced at this scale. **PASS.**

## Verdict

```
cap-bites control:            PASS
proof 1 route-by-bytes:       PASS
proof 2 reroute-to-complete:  PASS
proof 3 path parity:          PASS
```

All three §9 properties hold under a real kernel cgroup cap with zero VM spend.
TB-3 is GREEN -- the confidence gate for TB-5 (flag enablement) and TB-6 (GCP
50M/100M) is cleared on the local-fallback path.

## Guardrails honored

- **No default flag flipped.** The runtime governor / byte-estimate routing are
  enabled only within the validation run (per-call), staying default-OFF
  (flags flip at TB-5).
- **No GCP spend.**
- **Not a default-CI test.** The harness needs cgroup v2 + passwordless `sudo
  systemd-run` + minutes of runtime; it is a manual/gated validation invoked
  explicitly, not wired into the default gate. Reproduce with:
  `.venv/bin/python scripts/tb3_cgroup_validation.py`.

## Reproduce

```
.venv/bin/python scripts/tb3_cgroup_validation.py           # defaults above
# knobs: --rows --cols --width --cap-mb --headroom-mb --parity-rows \
#        --width-rows --width-budget-mb --work-dir
```

Exit 0 = all proofs PASS; exit 1 = a proof FAILED (a real Track B finding);
exit 3 = a real kernel cap is not achievable on this host (aborts rather than
silently degrading to the in-process monitor).
