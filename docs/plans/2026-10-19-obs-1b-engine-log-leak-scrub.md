Status: plan (Observability OBS-1b slice: scrub the remaining raw-exception/value engine log + error sites, and close the leak-sentry blind spots. HOLD build until Codex plan-gate GO. Program plan: `decoy-platform/docs/plans/2026-10-19-observability-program.md` §2 OBS-1b. Base: engine `origin/main` @ 1e0ea3d6. Merge authority: Phase-F bar extended to this runway (Cam 2026-10-19) - merge on dennis GO + Codex final GO + local-CI green, flag Slack before merge. Grounded in the 2026-10-19 error-message survey.)

Rules consulted: dev-rules `00-universal.md`, `development-loop.md` (incl. the OBS-0 "logging wired" done-state), `observability-and-resilience.md` (log location + exception TYPE, never a value/expression/raw sub-exception; an automated check is required), `risk-and-exceptions.md`; engine `CLAUDE.md` (explain-why; module caps; sentry allowlist discipline). Design spec: `docs/guides/logging.md`; engine `docs/security/error-reporting-and-data-exposure.md`.

# OBS-1b: engine log-leak scrub + sentry blind-spot closure

## 0. Why

The error-message survey confirmed raw-value/exception leaks still live in the engine, including ONE user-facing path, and two leak-sentry blind spots that let them through. OBS-1b fixes the sites to log location + exception TYPE only, and extends the sentry so the class cannot silently reappear. No masking/generation behavior changes.

## 1. What exists

- Leak sentry `tests/sentry/test_log_interpolation.py`: AST ratchet over `logger.*` calls, name-based via `RISKY_NAMES` (:23-48), with an `ALLOWLIST` (:54-72). Two structural limits: (a) name-based, so a data value in a domain-named variable slips through (e.g. geo `z3`); (b) only inspects logger calls, so returned `error=` strings and `emit_step(error_msg=...)` sinks are invisible.
- Four ALLOWLIST entries marked "Observability follow-up" (:68-71): `internal/faker_setup.py::_load_txt` + `_load_json`, `storm/profiler.py::_run_storm_inner`, `transforms/base.py::BaseMaskingStrategy._log_stats` - known, deferred, NOT fixed.
- Scrub helpers `execution/_isolated_common.py`: `scrub_error_text` (:191, only the Arrow `Wrapping <value> failed` pattern), `exception_error_text` (:209, type name + scrubbed msg).
- The sites (survey, @origin/main):
  - `storm/profiler.py:613` `logger.error(f"... {type(exc).__name__}: {exc}")` (raw exc; allowlisted) + an ADJACENT `emit_step(..., error_msg=str(exc))` (2nd leak into the structured step event; sentry-invisible).
  - `transforms/base.py:92` `self.logger.debug(f"... '{column_name}': {exc}")` (raw exc at DEBUG; allowlisted).
  - `internal/faker_setup.py:~263,~281` `_log.warning("custom_providers: failed ... %s: %s", path.name, exc)` (raw exc; both allowlisted).
  - `transforms/geo_generalize.py:398` `_LOG.debug("... zip3 prefix %r is HHS-restricted ...", z3)` (leaks the zip3 VALUE at DEBUG; NOT allowlisted, NOT caught - `z3` not in RISKY_NAMES).
  - `execution/_isolated_run.py:497->512` `stderr_tail = stderr...[-1][:300]` (+ `stdout_tail` :502) interpolated raw into `IsolatedRunResult.error`, which on `crashed` reaches the job's USER-VISIBLE error. The worst: user-facing, crash path (stderr can hold a traceback frame with real row data), unscrubbed, sentry-invisible (returned string).

## 2. The change (scrub the sites; close the sentry blind spots; NO behavior change)

- **(A) Scrub the four allowlisted log sites to type-only, then REMOVE their allowlist entries.** Each logs the location + `type(exc).__name__` (and the already-safe config identifier like `path.name`/`column_name`), never the exception message. After the fix, DELETE the four "Observability follow-up" ALLOWLIST entries so the sentry proves the fix (a still-leaking site would now FAIL, not be suppressed). `storm/profiler.py`: fix BOTH the `logger.error` and the adjacent `emit_step(error_msg=...)` (type-only).
- **(B) Fix the geo_generalize zip3 DEBUG leak + the name blind spot.** `geo_generalize.py:398`: log the location/that a restricted prefix was encountered, NOT the `z3` value (the restriction is structural, the value adds nothing safe). AND add `z3`, `z5`, `zip`, `zip3`, `zip5`, `prefix` (the geo value-bearing names) to `RISKY_NAMES` so this class is caught going forward - the sentry must FAIL on the pre-fix geo line (prove the name addition catches it).
- **(C) Fix the user-facing crash leak + extend the sentry beyond logger calls.** `execution/_isolated_run.py`: the stderr/stdout tail must NOT be interpolated raw into `IsolatedRunResult.error`. Replace with a safe summary (exit signal/return code + "see server logs", or a type/location marker), so no child output text reaches the user-facing job error. Extend the sentry to inspect the non-logger sinks that carry user/data text: `emit_step(error_msg=...)` and the returned `error=`/`IsolatedRunResult.error` construction - at minimum flag a RISKY_NAMES value flowing into those sinks (mirroring the logger-call check). If full AST coverage of returned strings is too broad, scope the extension to the named sinks (`emit_step` error_msg kwarg; the `IsolatedRunResult.error` assignment) and assert the pre-fix `_isolated_run` line would fail.
- **Do NOT** change masking/generation compute, determinism, or accepted-job output. The `_isolated_run` change alters only the human-readable `error` SUMMARY string on the crash path (not a data path, not output); a test pins that the new summary carries no child output text.

## 3. Risks

- Over-scrubbing: keep the SAFE config identifiers (path.name, column_name, the fact of a restricted prefix) - they are actionable and not data; only the exception message / data value is removed.
- Sentry false-negatives: the extension must actually FAIL on each pre-fix site (geo name, emit_step, _isolated_run) - prove it by running the sentry against the pre-fix lines (or a fixture) and seeing red.
- Sentry false-positives: adding `zip`/`prefix` to RISKY_NAMES could flag benign non-data uses elsewhere; sweep the engine for new violations and allowlist ONLY genuinely-safe ones with a stated reason (prefer fixing).
- `_isolated_run` crash-path: the error summary is what operators/users see on a crash - keep it DIAGNOSTIC (exit/signal, a stable marker) so debuggability is preserved via the server log, not the user error.
- Module caps + physical-seam sentry unaffected (test-side + log-string edits).

## 4. Acceptance

- All four previously-allowlisted sites log type-only; their ALLOWLIST entries are DELETED and the sentry is green without them (proves fixed, not suppressed).
- geo_generalize logs no zip3 value; `z3`/`z5`/`zip*`/`prefix` are in RISKY_NAMES; the sentry FAILS on the pre-fix geo line and passes on the fix.
- `_isolated_run` error carries NO child stderr/stdout text on the crash path (a test constructs a crash with a sentinel in child stderr and asserts the sentinel is absent from `IsolatedRunResult.error`); the sentry (extended) FAILS on the pre-fix interpolation.
- The sentry now inspects `emit_step(error_msg=...)` and the `IsolatedRunResult.error` sink; storm/profiler's emit_step is type-only.
- No regression: full engine suite green (abi-3 companion venv for native/parity; ci-mirror for pure-python/sentry); no accepted-job output change; determinism untouched; module census does not grow.

## 5. Verification + gates

- ci-mirror venv `~/.cache/decoy-ci-mirror-venv/bin/python` for the sentry + pure-python; abi-3 `~/.cache/decoy-d1a-abi3-venv/bin/python` for anything touching native/isolated-run tests. ALWAYS `PYTHONPATH="$(pwd)/src:$(pwd)" ~/bin/pytest-one <python> <paths> -q`, one at a time. Run the leak sentry, the geo/storm/faker/base transform tests, and the isolated-run tests.
- Gate chain: Opus authors + VERIFY -> dennis -> Codex final -> merge on full green + local CI (Phase-F bar), flag Slack before merge (pre-merge explainer: what it is/does + example). Build on `feat/obs-1b-leak-scrub` (off engine origin/main 1e0ea3d6).
- DOCUMENT: CHANGELOG (engine); after merge: platform RECENTLY-SHIPPED note (OBS-1b); section Slack; the engine `docs/security/error-reporting-and-data-exposure.md` updated if its promises change.

## 6. Done when

- [ ] Four allowlisted sites type-only; allowlist entries removed; sentry green without them.
- [ ] geo zip3 value no longer logged; RISKY_NAMES extended (z3/z5/zip*/prefix); sentry fails pre-fix, passes post-fix; no new false-positive violations (or justified allowlist).
- [ ] `_isolated_run` crash error carries no child output text (sentinel test); sentry extended to emit_step + the error sink and fails on the pre-fix lines.
- [ ] Full engine suite green; no accepted-job output change; determinism + module census unchanged. dennis GO + Codex final GO; merged on the Phase-F bar; CHANGELOG + post-merge docs + section Slack done.

cam
