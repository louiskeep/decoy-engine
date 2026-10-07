Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, debugging, testing, observability-and-resilience, code-review.

# Isolated-run OOM classification by measured memory (the deferred VmData monitor)

Follows `docs/plans/2026-10-07-oom-classification.md` (#219), whose section 6 deferred this fix "until the diagnostic shows a CI failure of that shape". It did: main CI on #220 (run 37592337361, attempt 1) failed `TestMemCapOom` with `error="child terminated abnormally (returncode=-11, signal=SIGSEGV); stderr tail: ''"`. Cam approved building it (2026-10-07). Branch `fix/oom-vmdata-monitor` off engine main `2ed9eb4c`. Risk R2: a change to how capped runs are classified.

## 1. Problem

When a capped child process dies by a signal other than SIGKILL or SIGABRT, and stderr shows no memory marker, the run is classified `crashed` (`_isolated_common.classify_abnormal_exit`, ~:213-245). Native code that hits RLIMIT_DATA can get a NULL back from malloc and segfault without printing anything. The run truly exhausted its cap, but it reports as an opaque crash. That breaks the isolated-run guarantee ("a running job that exhausts its cap is `oom_killed`, never `crashed`"), and it is the remaining cause of the `TestMemCapOom` flake.

Message wording cannot fix this, because there is no message. The evidence has to come from the process itself: how much of its capped memory it was using.

## 2. Established facts

- **Where the cap is set.** The worker sets it in-child with `resource.setrlimit(RLIMIT_KINDS[rlimit_kind], (cap, cap))` (`_isolated_common.apply_mem_cap`, :161-169). `RLIMIT_KINDS = {"as": RLIMIT_AS, "data": RLIMIT_DATA}` (:86).
- **What the driver knows.** `mem_cap_bytes` and `rlimit_kind` are passed through `_run_isolated` (`_isolated_run.py` :315-345).
- **How the driver waits.** It runs the child with `subprocess.Popen` and blocks in `proc.communicate(timeout=timeout_s)` (`_isolated_run.py` ~:395-440). The `on_spawn` hook gets the live pid.
- **Where the classifier runs.** When no envelope arrives, `classify_abnormal_exit(returncode, stderr)` decides (`_isolated_run.py` ~:470-500), and the result records `returncode`, `signal_number` and an error tail.
- **What `/proc/<pid>/status` reports (Linux).**
  - `VmData` is the data-segment size the kernel charges against `RLIMIT_DATA` (private writable mappings plus heap).
  - `VmSize` is the total virtual size that `RLIMIT_AS` bounds.
  - `VmPeak` is the peak of `VmSize`. There is no peak counterpart for `VmData`.
  - Once the child exits and is reaped, its `/proc` entry is gone. A zombie's status carries no `Vm*` lines.

## 3. Decisions

**3a. Sample while waiting.**
- When `mem_cap_bytes` is set, the driver runs a small daemon sampler thread while it waits in `communicate`.
- Every `_SAMPLE_INTERVAL_S` (50 ms) the thread reads `/proc/<pid>/status` and keeps the running maximum of the field that matches the cap kind: `VmData` for `"data"` and `VmSize` for `"as"`.
- It also reads `VmPeak` once at the end, if it is still readable, for the `"as"` kind.
- It stops when the process exits, or the `/proc` entry or field disappears.
- Any read error ends sampling quietly. It must never fail or slow the run.
- The thread is joined, with a short timeout, before classification.

**3b. Classify by measured headroom.** In the abnormal-exit branch only, after the existing marker and signal rules have returned `crashed`, a capped run is reclassified `oom_killed` if its sampled peak was at or above `cap - margin`.
- **Margin:** `margin = max(_MIN_MARGIN_BYTES, _MARGIN_FRACTION * cap)`, with `_MIN_MARGIN_BYTES = 256 MiB` and `_MARGIN_FRACTION = 0.20`.
- **Why that margin:** the failing allocation is not visible to a sampler. The investigation saw single Arrow allocations of 64 to 256 MiB fail (`malloc of size 268435456`), so the process can die with its last sample well below the cap. That is why the margin is generous.
- **Ordering:** the existing `oom_killed` rules (markers, SIGKILL, SIGABRT) are unchanged and checked first.
- **Not affected:** the envelope paths (the self-reported outcomes) and uncapped runs.
- **Margin rationale on record:** the margin is chosen and recorded from the soak (test 6), not tuned silently.

**3c. Evidence on the result.**
- The error text gains `"memory peak <X> MiB of <cap> MiB (<kind>)"` whenever a capped run dies abnormally, whatever the outcome. A crash far from the cap stays `crashed` and shows how far it was.
- `IsolatedRunResult.peak_rss_mb` stays `None` on this branch, since it means RSS. A new field `peak_capped_mb: float | None` carries the sampled peak.
- No data values are recorded; these are only sizes.

**3d. What a false positive costs.** A genuine non-memory segfault in a job already running within the margin of its cap is now named `oom_killed`. The platform then reroutes it to a bounded route instead of surfacing a crash. That trade is accepted on purpose: such a job was about to hit its cap regardless, and the recorded peak makes the call auditable. A crash far from the cap is still `crashed`.

**3e. Portability.** On a system without `/proc/<pid>/status` (non-Linux), sampling yields no peak and classification is exactly as today. A test pins this.

## 4. Acceptance tests (written first; never weakened)

1. **Sampler unit tests** (a fake status reader):
   - takes the maximum across samples;
   - picks the right field per kind;
   - stops cleanly when the entry disappears;
   - read errors never raise;
   - the thread is joined.
2. **Classifier table:**
   - SIGSEGV with an empty stderr and a peak inside the margin gives `oom_killed`;
   - the same peak outside the margin gives `crashed`;
   - an uncapped run gives `crashed`;
   - SIGKILL and SIGABRT stay `oom_killed`, and marker rules are unchanged;
   - a positive returncode with no envelope follows the same headroom rule;
   - the error text carries the peak and the cap.
3. **Real child, kept OUT of the default CI run if it is flaky** (marker `isolated_soak`, documented):
   - a tiny C-level segfault after allocating close to the cap gives `oom_killed` (for example, a child that grows a bytearray to near the cap and then calls `ctypes.string_at(0)`);
   - the same segfault at low usage gives `crashed`.
4. **`TestMemCapOom` unchanged:** its assertion and diagnostic stay. It must now pass whichever failure shape occurs.
5. **No regressions:** every existing `test_isolated_run.py` and OOM-classification test passes unmodified.
6. **Soak, local, recorded:** the flaking test 30 times at 1536 MiB and 30 times at 2280 MiB on the CI-mirror venv. Zero `crashed`. Record the sampled peaks against the caps to justify the margin. Mind the disk: each soak item leaves basetemp, so clean `/tmp/pytest-one` between batches.
7. **Overhead:** the sampler adds less than 1% to an isolated run of about 30 s. Recorded.
8. **Sentries** (log interpolation, module size, physical seam) and mutation on the headroom rule and sampler loop.

## 5. Failure modes

| Risk | Closed by |
|---|---|
| A real segfault is mislabeled OOM | Only within the margin of the cap; the peak is recorded; 3d accepts this on purpose |
| The sampler misses the final spike | A generous margin (3b); the soak records real peaks |
| The sampler hurts or fails the run | A daemon thread, errors swallowed, a bounded join; test 1 |
| Non-Linux hosts | No peak means today's behavior; 3e test |
| The test flake continues | Soak, test 6 |

Rollback: revert the merge commit.
