# F31 revision 5: O3 classification of Platform except-bound text copies

Superseded in part by `../2026-11-02-f31-r6-callee/README.md` (revision 6): three rows corrected below, and a
second probe pass for callee, attribute and store flows. Totals below are revision 5's.

Probe: `excbound.py` (stdlib `ast`, imports nothing), run with the container Python 3.13.18,
NOT the `uv.lock` interpreter, over decoy-platform `origin/main` `d8908f42`. Raw output:
`census-d8908f42.txt` (56 sites in 29 files: 23 `return`, 12 `assign`, 21 call statements).

Method: inside `except ... as NAME`, a `return`, an assignment or a call statement whose value
reads NAME in an R7 text shape (`str(NAME)`, `repr(NAME)`, `str(f(NAME))`, `NAME.args`,
`NAME.message`, NAME in an f-string, `%` or `.format`), excluding reads inside `HTTPException`
(part (b)), `raise`, logger calls and `write_audit*`. Dennis's review counted 43 by a different
probe that was not provided; this probe's return-plus-assign subset is 35 and it adds 21 call
statements, so it is a superset of the shapes he named (all three of his concrete sites are rows).
Passing NAME to a callee is not a row; the callee's own copy is (the V1-A path is such a callee
and is closed by part (e), not by this census).

Classes: `response` = reaches a non-admin response or stored user-visible field outside the four
choke points; `choke` = reaches a user only through a choke point another part closes (named);
`admin` = stderr, server log, audit or admin CLI; `internal` = IPC or in-process state; a row
ends at its first consumer that is a choke point, an admin surface or a raise.

| Site (api/...) | Class | Where it lands | Decision |
|---|---|---|---|
| pipelines/router.py:717 | response | `ValidateYamlOut.messages[].message`, pipeline validate (owner) | **P4d** |
| workflows/runner.py:225 | response | `WorkflowRun.summary`, served by `GET /workflows/runs` (owner, admin) | **P4d** |
| workflows/runner.py:238 | response | same; PyYAML marked errors quote the config line | **P4d** |
| pipelines/router.py:478, :483 | response | `IterationPreviewOut.error`, owner-only GET; the error branches return `values=[]` and the text interpolates the stored `mode` and loop bounds (`jobs/iteration.py:115-116`, `:161-166`) | **P4d** (revision 6; revision 5 said V2-LOG on a false claim) |
| subset/router.py:73 | response | `SubsetPreviewOut.warnings`; `SubsetBudgetExceededError` message is built from typed fields only (scope, table, cap, counts, edge id; engine `subset/_policy.py:87`, `:111`) | V2-LOG [1] |
| environments/bindings.py:321 | response | binding health `detail`; `BindingResolutionError` raise sites interpolate names, ids and pydantic type codes only (`bindings.py:177`, `:222`, `:349`, `:360`, `:379`; `_validation_summary` drops `input` and `msg`, `:276-288`) | V2-LOG [1] |
| pipelines/preflight.py:170 | response | validate `messages[].message`; `UnresolvedVariable` raise sites interpolate scope, key and the above names only (`jobs/variables.py:157`, `:160`, `:426`, `:440`, `:446`, `:462`) | V2-LOG [1] |
| retrieval/reader.py:361 | response | `generation_stale` detail; `_LocatorStaleError` messages are constants (`retrieval/_bindings.py:206`) | none |
| pipelines/v2_validation.py:154 | response | `ValidationMessage` | already V1: P7 (part (c)) |
| jobs/v2_vault.py:91 | choke | vault `JobNodeRun.error` | V1: P8 (part (e)) |
| jobs/fixed_width_output.py:225 | choke | coded error `.message`, read by `_failure_details` into the error node | V1: P8 (part (e)) |
| jobs/queue_worker.py:413; jobs/scheduler_claim.py:384, :412; jobs/admission.py:993, :1231; jobs/worker_budget.py:397 | choke | a reason string that `_fail_host_safety` (`queue_worker.py:378-386`, `scheduler_claim_loop.py:173`) appends to `Job.log` | V1: P2a (part (a)); the rewritten writer drops `reason` |
| storm/router.py:1236, :1265, :1356 | internal | `JobLogger.step(error_msg=...)`; never written (`persistence/logger.py:206-207`) | none |
| health_check.py:66 | response (unauthenticated) | `/healthz/db` and `/server-logs/services`: 200 chars of DB driver error text (host, user, database, socket path) | **P4d** (revision 6; revision 5 said V2-LOG) |
| jobs/cgroup_supervisor.py:265 | admin | audit detail | V2-LOG [1] |
| decoy_admin.py:47, :79, :211, :214, :252; preflight.py:107; jobs/preflight_worker.py:400; retrieval/decode_child.py:2576, :2587 | admin | process stderr (admin CLI, startup, child stdio) | V2-LOG [1] |
| retrieval/_outcome.py:261, :521, :593; retrieval/reader.py:328, :364, :457 | admin | `log_detail` to the server log; the served body is generic (`retrieval/retrieval_http.py:119-130`) | none |
| jobs/cgroup_supervisor_preflight_handoff.py:198; jobs/cgroup_supervisor_rpc_handlers.py:289, :299, :316, :471; jobs/cgroup_supervisor_transport.py:428, :506; jobs/preflight_dispatch.py:362; jobs/preflight_worker.py:381, :387 | internal | supervisor IPC; ends in a raised `PreflightError` (`jobs/preflight_backend.py:354`, `jobs/preflight_dispatch.py:392`) and the server log (`preflight_backend.py:513`) | V2-LOG [1]; a re-wrapped text is a row where it is caught and copied (revision 6 pass 2) |
| launcher/ack.py:216; launcher/service.py:282; scheduler/launch_handoff.py:278, :280; supervision/startup.py:370, :390, :406 | internal | launcher RPC replies, scheduler handoff, lane registry reason (`supervision/registry.py:213-219`, raised as `LaneDisabledError`) | V2-LOG [1]; `startup.py` reasons reach responses via `LaneDisabledError.reason`: closed by P4e (revision 6) |
| pipelines/preflight.py:168 | internal | substring test (`"unknown scope" in str(exc)`), not a copy | none |

Totals: 3 rows planned as P4d; 1 rewritten by P8 (vault) and 1 by P7; 7 keep their copy but its only
sink is closed downstream (6 by P2a's `_fail_host_safety` rewrite, 1 by P8's error-node reader);
44 need no V1 work (V2-LOG or no copy). The P4d ratchet `exc-copy` takes the rows still present at
P4d's base as its baseline; a slice that rewrites a row removes it, so 51 rows remain after P4d,
P7 and P8. A new row fails until it is classified here.
