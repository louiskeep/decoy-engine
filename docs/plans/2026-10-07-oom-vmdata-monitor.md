Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, debugging, testing, observability-and-resilience, code-review.

# Isolated-run memory evidence for silent crashes (rev 2)

Follows `docs/plans/2026-10-07-oom-classification.md` (#219), section 6. Main CI on #220 (run 37592337361, attempt 1) failed `TestMemCapOom` with `error="child terminated abnormally (returncode=-11, signal=SIGSEGV); stderr tail: ''"`. Branch `fix/oom-vmdata-monitor` off engine main `2ed9eb4c`. Risk R1 after rev 2: no outcome changes, only added evidence.

**Rev 2 direction (Cam, 2026-10-07: "Evidence, not relabel").** Codex round 1 showed that driver-side memory sampling is a probabilistic SUSPICION, not causal attribution:
- **False negatives:** a rejected large allocation is never sampled, and growth plus death can happen between samples.
- **False positives:** a stale earlier peak followed by an unrelated crash.

So the run's OUTCOME is NOT changed. Classification stays exactly as today. The driver records memory evidence on the result, including a `suspected_memory_pressure` flag, so the platform and the logs can show it. Nothing is mislabeled or wrongly retried.

## 1. Problem

A capped child that dies by a signal other than SIGKILL or SIGABRT, with no memory marker on stderr, is classified `crashed` (`_isolated_common.classify_abnormal_exit`, ~:213-245). Native code that hits RLIMIT_DATA can get a NULL back from malloc and segfault silently. Today nothing on the result tells a silent memory crash apart from a genuine bug. As a result:
- an operator cannot tell which one happened;
- `TestMemCapOom` flakes.

## 2. Established facts (confirmed by Codex round 1)

- **What VmData measures.** `VmData` in `/proc/<pid>/status` exposes `mm->data_vm`, which the kernel checks against RLIMIT_DATA (mm/mmap.c, Linux 5.4 and 6.6; mmap enforcement since 4.7). It counts private writable non-stack mappings. This is virtual allocation, not residency.
- **The other fields.** `VmSize` is total virtual size, the quantity RLIMIT_AS bounds. `VmPeak` is the peak of `VmSize`.
- **Zombies and reaping.** A zombie still has `/proc/<pid>/status`, but without `Vm*` fields. Reaping removes the entry.
- **The cap and the driver.** The worker sets the cap in-child (`apply_mem_cap`, `_isolated_common.py:161-169`). The driver waits in `proc.communicate(timeout=...)` (`_isolated_run.py` ~:395-440). `on_spawn` receives the live pid. The timeout and callback-failure paths return before classification.
- **Kernel memory groups.** cgroup v2 `memory.events` counts kernel OOM kills against charged memory. An RLIMIT rejection increments no memcg counter.

## 3. Decisions

**3a. Outcomes unchanged.** `classify_abnormal_exit` and every envelope path behave exactly as on main, for every input.

**3b. Sampling lifecycle (Codex round 1, MEDIUM).**
- `_spawn_and_classify` receives `mem_cap_bytes` and `rlimit_kind` explicitly.
- When a cap is set, the sampler starts BEFORE `on_spawn`, as a daemon thread with a stop `Event`.
- Cleanup sits in an unconditional `finally`: set the event, then join with a bound.
- The result is published as a lock-protected snapshot.
- A thread-start failure means no evidence, and the run continues.
- The sampler owns no pipes and never waits on or reaps the child.
- Read errors, a missing or malformed field, a zombie (no `Vm*`) or a missing procfs all end sampling quietly.
- Timeout returns, callback exceptions, governor SIGKILL, envelopes and uncapped runs behave as on main. The timeout and callback paths may carry the evidence gathered so far.

**3c. What is sampled.**
- Every 50 ms, the sampler reads the field matching the cap kind (`VmData` for `"data"`, `VmSize` for `"as"`).
- For `"as"` it also reads `VmPeak` on every sample, because a read after `communicate` cannot recover it.
- It keeps the sample count, the first and last sample timestamps, the LAST sampled value, and the maximum.

**3d. Evidence on the result.** A new optional field, `IsolatedRunResult.memory_evidence`, set only for capped isolated runs. It is a small frozen record:
- `cap_mb`, `kind`;
- `last_mb`, `peak_mb`;
- `samples`, `window_ms`;
- `suspected_memory_pressure: bool`;
- `basis: str`, a short fixed phrase such as `"last sample within margin of cap"`.

The suspected flag works like this:
- **Rule:** `suspected_memory_pressure` is True only when the run ended abnormally (no envelope) AND its LAST sample was within the margin of the cap. The LAST sample is used, never the lifetime peak, so a stale earlier peak cannot set it (Codex round 1, HIGH 2).
- **Margin:** `margin = min(max(64 MiB, 0.10 * cap), 0.5 * cap)`. That stays positive and bounded for small caps, and caps below 128 MiB are documented as unsupported for the flag. It is a suspicion, recorded with its basis. It is not a verdict.
- **Error text:** the abnormal-exit error text gains `memory: last <X> MiB, peak <Y> MiB of <cap> MiB (<kind>)`. Sizes only, no data.

**3e. `TestMemCapOom` (Cam-approved change of assertion).** The test accepts:
- `oom_killed`, as before; or
- `crashed` with `memory_evidence.suspected_memory_pressure` true.

Any other result fails, including a crash far from the cap. The diagnostic message stays. This is the one assertion this slice changes. Cam approved it on 2026-10-07 as part of choosing evidence over relabeling.

**3f. Alternatives, decided.** A per-job cgroup v2 (`memory.max`, retained `memory.events.local` deltas, a supervisor outside the job) would give kernel-attributed OOM kills. It is NOT built now:
- it needs cgroup delegation on the self-hosted box and in Docker, which is unverified;
- it caps charged memory, not VmData;
- RLIMIT rejections never reach it.

It is recorded as the upgrade path if the evidence proves insufficient. Ptrace and seccomp are rejected as too intrusive or as giving no result evidence. Diagnostics inside a SIGSEGV handler are unsafe.

## 4. Acceptance tests (written first; never weakened except 3e, which Cam approved)

1. **Sampler units** (fake reader): last, max and count; field per kind; `VmPeak` read on every `"as"` sample; missing or malformed fields; zombie; read errors never raise; stop event; bounded join; a thread-start failure yields no evidence and the run is unaffected.
2. **Lifecycle:** the sampler starts before `on_spawn`. A slow `on_spawn` and a raising `on_spawn` both still clean up. Timeout, governor SIGKILL, envelope outcomes and uncapped runs: outcome, returncode and error are byte-identical to main, apart from the added evidence text where 3d says so.
3. **Flag rule:**
   - last sample within the margin gives True;
   - a high peak with a low last sample (peak then release) gives False;
   - the threshold boundary;
   - a small cap uses the bounded margin;
   - an envelope (self-reported) run gives False;
   - uncapped runs get no evidence.
4. **Deterministic integration child, kept in CI:** a handshake child allocates to a verified usage, waits for the driver to acknowledge an observed sample, then segfaults (`ctypes.string_at(0)`) with core dumps disabled.
   - Near the cap: `crashed` with the flag True.
   - Far from the cap: `crashed` with the flag False.
5. **Classifier unchanged:** every existing case in `test_isolated_run.py` and the OOM-classification tests passes unmodified, and a table test pins `classify_abnormal_exit` outputs.
6. **TestMemCapOom 3e:** the new assertion, plus a soak of 20 runs at 1536 MiB and 20 at 2280 MiB on the CI-mirror venv. Record every outcome with its evidence. Clean `/tmp/pytest-one` between batches.
7. **Overhead:** under 1% on an isolated run of about 30 s. Recorded.
8. **Sentries and mutation** on the flag rule and the sampler loop.

## 5. Failure modes

| Risk | Closed by |
|---|---|
| A real crash mislabeled OOM | Outcomes never change (3a) |
| The flag misleads | Last sample, not peak; the basis is recorded; the doc says it is a suspicion |
| The sampler hurts the run | Daemon thread, quiet errors, Event plus finally, bounded join; tests 1 and 2 |
| Lifecycle regressions | Test 2's byte-identical paths |
| Flaky integration test | Handshake child; test 4 |

Rollback: revert the merge commit.

## 6. Review log

- **Codex plan gate, round 1: REVISE** (2 HIGH, 3 MEDIUM). Cam then chose "evidence, not relabel" (2026-10-07), and rev 2 is rebuilt around it:
  - **HIGH 1 (no guarantee):** the outcome is unchanged; the flag is documented as a suspicion with its basis and sample record.
  - **HIGH 2 (stale peak, small caps, routing):** the flag uses the last sample, the margin is bounded, and there are no routing changes.
  - **MEDIUM 3 (lifecycle):** handled in 3b.
  - **MEDIUM 4 (flaky integration test):** the handshake child, plus the case list in tests 1-3.
  - **MEDIUM 5 (alternatives):** the decision is recorded in 3f.
