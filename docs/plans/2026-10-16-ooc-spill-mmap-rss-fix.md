Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, bugfix, performance, testing, verification

# Out-of-core spill readers: stop memory-mapping (unbounded file-backed RSS)

## 1. Problem (root cause, measured)

The out-of-core FK reorder lane's peak RSS scales with the largest reorder-lane
table's payload size, defeating the engine memory budget and any RSS/ceiling bar.
At 100M it peaked ~29-30 GiB regardless of an 8 GiB budget (Phase E runs).

Root cause (profiled 2026-10-16): three reorder-lane spill readers open their
Arrow IPC files with `pa.ipc.open_stream(str(path))`, which pyarrow
MEMORY-MAPS. As phase 3 reads the file, its touched pages become resident as
file-backed RSS (RssFile) and stay counted until the reader closes at end of
table. Nothing in the budget ledger accounts for mapped-file pages, so the
budget cannot bound it. This is the SAME failure the sorter already fixed and
documented in `_external_sort_run.py:18-40` (`_RunHead`: "Opened with `pa.OSFile`
... NOT `pa.memory_map`: a subprocess RSS measurement showed `memory_map` leaves
the touched pages of the WHOLE mapped file resident ... `OSFile` reads copy each
batch through the tracked pool instead"). The payload/spill readers were never
converted.

Evidence: at 10M (width 13, force-OOC, reorder), peak RSS was ~4.4 GiB with
~3.8 GiB in mapped `payload.arrow`/`child_keys.arrow` pages; anonymous RSS and
`pa.total_allocated_bytes` stayed flat (~500 MB / ~30 MB). A proof monkeypatch
wrapping the string path in `pa.OSFile` dropped the same run to ~1.8 GiB peak
(RssFile ~90 MB). Payload ~680 B/row fits the GCP numbers: uncapped grandchild
(45M) on reorder -> ~30 GB mapped (saw 30.4 GiB); capped child (35M) on reorder
-> ~24 GB mapped + ~4 GB anon (saw 28.9 GiB). The consumer is reorder-lane only;
the batch lane has no payload store (~1.4 GiB at 10M).

Note: these are clean, file-backed pages the kernel can reclaim under pressure,
so this is more a measurement / cgroup-accounting + budget-violation defect than
a proven hard OOM (not tested under real pressure). It still fails every
RSS-based bar and defeats the stated budget, so it blocks the bounded-100M claim.

## 2. Fix

Convert the three spill readers to read through `pa.OSFile(str(path), "rb")`
(buffered, pool-tracked, bounded) instead of a mapped string path, matching the
sorter's `_RunHead`. Sites in
`src/decoy_engine/execution/out_of_core/_payload_store.py`:
- `:113` `SpillPayloadStore.iter_batches` - `with pa.ipc.open_stream(str(self._path))`.
- `:164` `RawParentKeySpill.__iter__` - `with pa.ipc.open_stream(str(self._path))`.
- `:233` `SpillChildKeys.open_reader` - `return pa.ipc.open_stream(str(self._path))`
  (RETURNS the reader; caller owns it).

For the two `with` sites: open the OSFile in the same `with` so both close
together, e.g. `with pa.OSFile(str(self._path), "rb") as src, pa.ipc.open_stream(src) as reader:`.

For the returned-reader site (`:233`): the OSFile must stay alive as long as the
caller uses the reader and close WITH it. The caller is `StreamFkJoiner` (it
registers/unregisters the reader with its DuckDB connection). The build must
ensure the OSFile's lifetime is tied to the reader's (options, builder picks the
cleanest: return a tiny wrapper that owns both and closes both; or attach the
OSFile to the reader object so it is not GC-closed early; or convert the caller
to a `with` over an owner object). Do NOT leave the OSFile to be GC'd
non-deterministically.

Keep `open_stream` (not `open_file`) - the payload/key spills are written as
streams; only the source handle changes. Output stays byte-identical (same IPC
bytes, read through a file handle instead of a mapping).

## 3. Acceptance tests

- Byte-identical output: the existing out-of-core FK parity suite
  (`tests/parity/test_out_of_core_fk_parity.py` and siblings) stays green,
  companion present (`~/.cache/decoy-d1a-abi3-venv/bin/python` via
  `~/bin/pytest-one`). No output change on any route.
- NEW regression test: on a multi-hundred-MB reorder-lane payload (a fixture big
  enough that a mapped read would show it), assert the spill read stays bounded -
  peak RssFile (or peak RSS minus a baseline) stays well under the payload size,
  measured out-of-process (subprocess + `ru_maxrss`, or `/proc/self/smaps`
  RssFile for the payload file), mirroring the sorter's own RSS reasoning. The
  test must FAIL against the pre-fix mapped reader (prove non-vacuous) and pass
  after. Keep it fast/bounded enough for CI (a few hundred MB, not GBs) and guard
  on the companion if it needs the reorder lane.
- No regression in the sorter's own bounded behavior; `tests/sentry` physical
  seam + module-size unaffected (change is within `out_of_core/`, under
  `execution/`, not `execution/physical/`).
- ci-mirror clean (ruff + mypy on `src/decoy_engine`).

## 4. Risk

Low. Byte-identical output (same bytes, different read handle); the sorter
already proves `OSFile` is correct + bounded for the same file type. The only
real care is the `:233` returned-reader lifetime (do not close the OSFile early;
do not leak it) - called out above and covered by the parity suite (early close
would break the join read) + a no-leak check.

## 5. Verify after merge

The fix is proven locally to 10M. A single 100M GCP re-run (same config as the
Phase E capped run, 8 GiB budget) would CONFIRM the reorder lane now tracks
anonymous + budgeted-DuckDB + sorter memory (~a couple GiB) instead of ~29 GiB.
That re-run is owner-approved GCP spend but should be flagged before launching
(it is the bounded-100M proof the Phase D/E milestone wants). Hold it for Cam.

## 6. Gate chain

Codex plan-gate (this doc) -> build (convert 3 readers + regression test) ->
ci-mirror + parity + companion suite -> dennis -> Codex final -> merge on green +
Slack. DOCUMENT: run-state ledger; note the bounded-100M confirmation re-run as
the follow-up. This closes the Phase E bounded-memory finding (pending the
confirming re-run).

cam
