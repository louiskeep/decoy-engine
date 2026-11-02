# F31 revision 6: exc-copy census, pass 2 (callee, attribute, store) and corrections

Probe: `excbound.py` (stdlib `ast`, imports nothing), run with the container Python 3.13.18,
NOT the `uv.lock` interpreter, over decoy-platform `origin/main` `d8908f42`. Raw output:
`census-d8908f42.txt`. Pass 1 is revision 5's probe unchanged and reproduces its 56 rows
byte for byte (`../2026-11-02-f31-r5-o3/census-d8908f42.txt`). Pass 2 adds 30 rows at 19 sites.
`selftest.py` seeds each pass-2 shape plus two negative controls: red on the revision 5 probe
(5 MISS), green on this one; three mutations (no callee resolution, no text attributes, no
store shapes) each turn it red. No test suite was run.

Why a second pass: revision 5 excluded an exception passed to a callee and read only
`.args`/`.message`. The Codex gate of `ac8e1cf3` found the lane chains that this hid
(`supervision/startup.py:370`, `:390`, `:406` store `str(exc)` as the lane disable reason;
`LaneDisabledError.reason` is copied by `jobs/preview_slot.py:121` and
`retrieval/retrieval_slot.py:133`). Both are pass-2 rows; the rows are found where the text is
READ from a caught exception, so the store into the registry dict needs no tracing.

## Corrections to the revision 5 classification

| Site (api/...) | Revision 5 | Revision 6 | Why |
|---|---|---|---|
| pipelines/router.py:478, :483 | V2-LOG | **P4d** | The error branches return `values=[]`; `IterationConfigError` interpolates the stored `mode` (`jobs/iteration.py:115-116`) and loop bounds (`:161-166`) into `IterationPreviewOut.error`. "The response already carries the values" was false, and owner-only is still non-admin. |
| health_check.py:66 (copy at :72) | V2-LOG | **P4d** | `str(SQLAlchemyError)[:200]` served unauthenticated by `/healthz/db` (`main.py:223-234`) and to `release_manager` by `/server-logs/services` (`server_logs/router.py:87-104`); a connect error names host, user, database or socket path. |
| supervision/startup.py:370, :390, :406 | V2-LOG, "not traced past the raise" | internal; closed by **P4e** | The stored reason is read only by the reaper CAS (`supervision/reaper.py:469`) and, as `LaneDisabledError.reason`, by the two lane mappers P4e rewrites. |
| rows ending at a raise (lines 40, 41 of the revision 5 README) | "not traced past the raise" | covered | A re-wrapped text becomes a pass-1 or pass-2 row where it is caught and copied, whatever raised it. |

Classification rule from revision 6 (the V1-1 lesson): a row whose sink is a non-admin response is
V1 unless every producer of the text is cited and builds it from codes, counts, caps or type
names. "Owner-only" and "the response already carries the value" are not reasons.

## Pass 2 rows (19 sites)

| Site (api/...) | Kind | Where it lands | Decision |
|---|---|---|---|
| jobs/preview_slot.py:121 | callee | `PreviewUnavailableError.message` <- `LaneDisabledError.reason` | **P4e** |
| retrieval/retrieval_slot.py:133 | callee | `RetrievalUnavailableError.message` <- `LaneDisabledError.reason` | **P4e** |
| jobs/preview_http.py:52 | callee | 503 `detail.message` of job preview (`jobs/routes_preview.py:160`) and node preview (`pipelines/router.py:1374`); also carries `preview_parent.py:837`, whose `preview_sample.py:403-405` message holds a server path | **P4e** |
| retrieval/retrieval_http.py:103 | callee | 503/429 `detail.message` of `GET /jobs/{id}/outputs/{target}/page` before authentication (`retrieval/router.py:261-269`) | **P4e** |
| retrieval/_outcome.py:116 | callee | `RetrievalResult.message`, served by `retrieval_http.py:120` | closed at sink by **P4e** |
| retrieval/decode_child.py:2632, :2639, :2646 | attr | child envelope `detail`, served via `_outcome.py:225-235` and `retrieval_http.py:135-137`; `:1940`, `:2137`, `:2176` put pyarrow `str(exc)` in it | closed at sink by **P4e** |
| jobs/v2_orchestrator.py:287 | unres | `FailureDetails.report(job.id, exc)` | closed by **P8** (part (e)) |
| retrieval/retrieval_http.py:102 | callee | `exc.reason`, a closed code, selects the status | none |
| files/pg_pull_router.py:193 | attr | reads `exc.detail["code"]`, a code | none |
| jobs/run_job_start_recovery.py:158 | callee | `_land_refusal_failed(..., exc.detail)` ignores `detail`; fixed text at `:190-192` | none |
| admin/storage_walk.py:93, :98, :117, :124 | unres | `note_error` reads only `errno` (`:55-58`) | none |
| launcher/docker_runtime.py:102 | callee | `RuntimeUnavailableError(str(exc))` inside launcherd | V2-LOG [1]: launcher RPC; no Platform catch copies `.detail` (it would be an attr row) |
| launcher/protocol.py:469 | callee | launcher RPC error reply | V2-LOG [1]: same |
| scheduler/source_worker.py:86 | unres | exception object handed back to the calling thread and re-raised | none: its catcher is a row |

Totals: 86 rows (56 pass 1, 30 pass 2) at 75 sites. Rewritten: P4d 6 sites, P4e 4, P7 1, P8 1; 73 rows at 63 sites remain.
Closed at the sink: P2a 6, P8 2, P4e 4. The `exc-copy` baseline keys rows by file, enclosing
function and kind with a count, so line drift does not fail it.
