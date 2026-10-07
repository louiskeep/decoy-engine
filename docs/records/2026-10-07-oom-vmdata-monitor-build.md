Status: record

# Isolated-run memory evidence: build record

Contract: `docs/plans/2026-10-07-oom-vmdata-monitor.md` rev 4. Branch `fix/oom-vmdata-monitor` off engine main `2ed9eb4c`. Risk R2: no outcome changes, one new public result field, a sampler thread, and one appended error-text suffix.

## What changed

- New `execution/_isolated_memwatch.py`. A daemon thread reads `/proc/<pid>/status` every 50 ms: `VmData` for `rlimit_kind="data"`, `VmSize` plus `VmPeak` for `"as"`. It publishes one immutable `MemorySnapshot` (count, first and last sample time, last and max value, stop reason). The reader runs without the publication lock, and `stop()` closes publication, so a read still in flight after the bounded join cannot change the snapshot.
- `build_evidence` turns a snapshot into `MemoryEvidence`. `suspected_pressure` is the only place the flag is decided: abnormal exit, last sample at or above `cap - margin`, sample age at most 250 ms at the moment `communicate` returned. Margin is `min(max(64 MiB, 10% of cap), 50% of cap)`.
- `_isolated_run._spawn_and_classify` starts the sampler before `on_spawn`, takes the termination clock and stops the sampler straight after `communicate` or timeout cleanup, then does envelope work. Envelope and timeout results carry the evidence with the flag off. The abnormal-exit result carries it with the flag rule applied and gains `; memory: last X MiB, peak Y MiB of Z MiB (kind)` after the existing text (sizes only, omitted with no samples).
- `IsolatedRunResult.memory_evidence` is appended last with a `None` default. `MemoryEvidence.to_dict()` returns JSON primitives only.
- `classify_abnormal_exit` and every outcome path are untouched.
- Tests: `test_isolated_memwatch.py` (sampler, flag rule, margin, wire format, classifier table), `test_isolated_memwatch_run.py` (lifecycle and the real-child handshake test). `TestMemCapOom` accepts `oom_killed`, or `crashed` with `suspected_memory_pressure`, as the plan approves.
- Sentries: a census entry for `_isolated_run.py` at 611 lines, and a permitted-module entry for `_isolated_memwatch.py` in the physical-seam sentry.

## Judgment calls

- The test observer is a module attribute, `_isolated_memwatch.live_observer`, read when the sampler is created. The integration test sets it with `monkeypatch`; production leaves it `None`.
- The real-child test needs `MAP_PRIVATE` for its allocation. Python's default anonymous `mmap` is shared, and `VmData` does not count shared mappings (first version of the test never reached its target for `"data"`).
- `FieldMissing` derives from `DecoyError` and the observer failure log prints only the exception type, because the exception-hierarchy and log-interpolation sentries flagged the first version.
- `basis` has four fixed phrases beyond "no sample": near cap, below margin, too old, run did not end abnormally.
- The in-flight-read stall test fixes the cleanup rule: after a stalled `stop()` the late read is discarded, never merged.

## Verification

- Hand mutation of the flag rule and sampler loop: freshness `>` to `>=`, threshold `>=` to `>`, abnormal check dropped, flag from peak instead of last, margin `min` to `max`, freshness check removed, age clamp removed, closed-publication guard removed, first-sample time overwritten, max replaced by last, `VmPeak` not kept, loop ignoring the step result, loop ignoring the stop event, and the pacing wait removed. Two mutants survived the first pass (loop ignoring step result, pacing wait removed); a process-exit liveness assertion and a pacing test were added, and a redundant loop condition was removed so the loop has one check per concern. All are killed now.
- Overhead: one `procfs` read costs about 38 microseconds, 20 per second, on the driver side. Four alternating pairs of a capped completing run (about 6 s each, 4M rows by 8 columns, 1536 MiB `data` cap) took 5.62 to 6.97 s with the sampler and 5.59 to 6.65 s without (means 6.03 s and 6.11 s). No difference is measurable. A 30 s run was not timed; the cost is per sample, not per job length, so it scales to about 0.08% of one core.
- Soak (test 6, Python 3.10 CI-mirror venv, `TestMemCapOom` job, assertion as changed): 20 runs at 1536 MiB and 20 at 2280 MiB, in batches of 10. All 40 passed.

| Cap | Runs | `oom_killed` via envelope (flag off) | `oom_killed` via SIGABRT `std::bad_alloc` (flag on) | `crashed` | Failures |
|---|---|---|---|---|---|
| 1536 MiB | 20 | 20 | 0 | 0 | 0 |
| 2280 MiB | 20 | 11 | 9 | 0 | 0 |

  The nine SIGABRT runs had last samples near the cap (2262.5 MiB in the example) and ages of 143 to 193 ms against the 250 ms window, so the window has little headroom on this box. No SIGSEGV occurred in 40 runs, so the original flake was not reproduced here and the new `crashed` branch of the assertion was not exercised by the soak. Deterministic coverage of that branch is the real-child integration test.

## Not covered

- cgroup v2 attribution stays out of scope (plan 3f).
- The flag is a suspicion. A rejected large allocation with a low last sample, or growth between two samples, still produces `crashed` with the flag off, and `TestMemCapOom` would then fail. It is recorded as an unresolved acceptance failure for a decision, never fixed by widening the margin, the window or the assertion.
- Caps below 128 MiB are unsupported for the flag (the margin becomes half the cap).

## dennis gate and remediation

dennis GO (0 BLOCKER, 0 HIGH, 1 MEDIUM, 3 LOW). All seven plan pins hold. dennis's governor A/B against main used a real child and a 2 GiB cap: the outcomes were identical, and only the evidence field differed.

- **MEDIUM:** the freshness clock now stops at the sampler's own observation of the child's exit, so kernel teardown time no longer ages the sample. See the plan's review-log amendment to 3d. Tests cover four cases: slow teardown stays fresh, `field_missing` also stops the clock, `read_error` keeps the `communicate` reference, and an exit seen late can never exceed the `communicate` time.
- **LOW 1:** fixed the double fd close in the handshake test.
- **LOW 2:** `start_sampler` now sits inside the `try`. A test checks that a sampler construction error still kills and reaps the child, and that `_FIELDS` keys equal `RLIMIT_KINDS` keys.
- **LOW 3:** the real capped-governor test is tracked as a follow-up. dennis's manual A/B is the evidence for now.

After remediation: the memwatch, isolated-run and OOM-classification tests pass (165 passed).

