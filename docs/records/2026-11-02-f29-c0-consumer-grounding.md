---
orphan: true
---
# F29 C0: consumer grounding record, failure-tail census

Status: record

Slice C0 of plan `docs/plans/2026-11-02-f29-errors-progress-remainder.md` (branch `plan/f29-errors-progress-remainder`, head `a26994bf`, revision 5). Docs only. Every `file:line` resolves on the section 1 merge bases (section 7). Drift against the plan: (a) the vault no longer writes an error node (section 2, row V); (b) both consumers pin `0.18.0` (section 6); (c) `JobDetail.tsx` renders the raw string, not `error.message` (section 5).

## 1. Merge bases

| Repo | Census merge base (`origin/main`) | Handoff base | Moved |
|---|---|---|---|
| Platform | `0638a97d1d98a50630ecc694f49c5a45062261ab` | `5e610156` | descends; 15 `api/jobs/` files changed; all citations re-anchored here |
| CLI | `257706265498140466142477d2249eee0957763f` | `004afda9` | descends; `src/decoy/cli/run.py` identical; `pyproject.toml` repinned |
| Engine | `9eec6123114e0395eafe46d2bae395c6ebccf74b` | `f5b46a2c` (`v0.18.0`) | descends; registry `1.3.0` (#365); #366 touches `subset/` only |

## 2. Platform failure-tail census

Tail: inner `try` at `api/jobs/v2_orchestrator.py:245`, `except Exception as exc` at `api/jobs/v2_orchestrator.py:289`, `details = _failure_details(exc)` at `api/jobs/v2_orchestrator.py:301`, one error node at `api/jobs/v2_orchestrator.py:329`. "Node writes" names each `JobNodeRun(...)` with `error=`.

| Path | Entry `file:line` | Reaches `:289` | Node writes with `error=` |
|---|---|---|---|
| K `_key_setup_error` | held `api/jobs/v2_orchestrator.py:173`, raised in the try at `api/jobs/v2_orchestrator.py:254` | yes | `_error_node_run` `api/jobs/v2_node_runs.py:426` (`error=` at `api/jobs/v2_node_runs.py:434`) |
| S `_run_v2_pipeline_streaming` | `api/jobs/v2_runner.py:269`; re-raise `api/jobs/v2_runner.py:426`; wrapped `PartialPublicationError` `api/jobs/v2_stream_coordinator.py:237`; route `api/jobs/route_dispatch_exec.py:46` called at `api/jobs/v2_orchestrator.py:278` | yes | the same single write |
| Q `v2_sequential.py` | `api/jobs/v2_sequential.py:486` via `api/jobs/route_dispatch_exec.py:150`, called at `api/jobs/v2_orchestrator.py:280`; vault persist `api/jobs/v2_sequential.py:460`; no status write in the file | yes | the same single write |
| F `v2_full_frame.py` | `api/jobs/v2_full_frame.py:70` via `api/jobs/route_dispatch_exec.py:108`, called at `api/jobs/v2_orchestrator.py:282`; `TokenVaultDurabilityError` `api/jobs/v2_full_frame.py:63`; vault persist `api/jobs/v2_full_frame.py:175` | yes | the same single write |
| V `_persist_job_vault` | raises `VaultPersistError()` `api/jobs/v2_vault.py:279` (fixed message `api/jobs/v2_vault.py:91`), from rows Q and F | yes | none; its ok node `api/jobs/v2_vault.py:280` has no `error=` |
| H1 run-owner refusal | `api/jobs/queue_worker.py:413` | no (named limit) | none |
| H2 price reject | `api/jobs/queue_worker.py:419` | no (named limit) | none |
| H3 disk reject | `api/jobs/queue_worker.py:430` | no (named limit) | none |
| H4 ledger reject | `api/jobs/queue_worker.py:434` | no (named limit) | none |
| H5 flag-on claim reject | `api/jobs/scheduler_claim_loop.py:173` | no (named limit) | none |
| X failed-status writers not in the plan list | `api/jobs/scheduler_claim_loop.py:89`, `api/jobs/queue_worker.py:604`, `api/jobs/reaper.py:219`, `api/jobs/job_terminal_cas.py:247`, `api/jobs/iteration.py:499` | no (named limit) | none |

H1 to H5 call `_fail_host_safety` (`api/jobs/queue_worker.py:378`), which appends `Job.log` (`api/jobs/queue_worker.py:385`) and writes no node. Row V: at `67e8cbba` the vault node stored `str(exc)` (`api/jobs/v2_vault.py:98` at `67e8cbba`); Platform #152 (`d058f361`) removed it. On the merge base the only `error=` node write in `api/` is `api/jobs/v2_node_runs.py:434`, so after P1 every failed-job node goes through `node_error` at one call site; P1's `v2_vault.py` edit is void and P-T7's vault failure lands on that write with code `vault_write_failed`.

## 3. Row readers

| Reader | `file:line` | Builds `error` from rows | P1 covers |
|---|---|---|---|
| `/node-runs` | `api/jobs/routes_runtime_facts.py:70` to `api/jobs/routes_runtime_facts.py:115`, row at `api/jobs/routes_runtime_facts.py:99` (`error: str | None`, `api/jobs/schemas.py:430`) | yes, as stored | yes |
| `update_finished_manifest` | `api/evidence/assembly.py:2334`, `_hydrate_nodes` at `api/evidence/assembly.py:2408` (def `api/evidence/assembly.py:1888`, raw copy `api/evidence/assembly.py:1984`) | yes | yes |
| Legacy synthesis | `api/jobs/routes_evidence.py:190` (`_hydrate_nodes`) | yes | yes |
| Stored manifest | `api/jobs/routes_evidence.py:164` to `api/jobs/routes_evidence.py:166` | served as stored | no, named limit |

The other grep hits select `exports`, `warnings`, `kind` or `status` only (`admin/storage_inventory.py`, `evidence/assembly.py` output helpers, `files/router.py`, `jobs/execution_summary.py`, `jobs/routes_evidence.py` outputs query, `jobs/routes_outputs.py`, `reporting/parsing.py`, `unmask/router.py`); a grep for `.error` / `"error"` / `error=` in each finds no node-error read. No unlisted reader, so no stop; P1 not widened. `NodeError` is `api/evidence/schema.py:328`.

<!-- raw -->
```
$ git grep -n "JobNodeRun" api/    # Platform 0638a97d, full output
api/admin/storage_inventory.py:57:    JobNodeRun,
api/admin/storage_inventory.py:151:        db.query(JobNodeRun.job_id, JobNodeRun.exports, Job.status)
api/admin/storage_inventory.py:152:        .join(Job, JobNodeRun.job_id == Job.id)
api/admin/storage_inventory.py:153:        .filter(JobNodeRun.kind == "target.file", JobNodeRun.exports.is_not(None))
api/admin/storage_inventory.py:154:        .order_by(JobNodeRun.id.desc())
api/evidence/assembly.py:17:  off the job row, hydrates the terminal sections from JobNodeRun
api/evidence/assembly.py:1647:    matching child node's JobNodeRun.exports payload (the engine emits
api/evidence/assembly.py:1655:      - JobNodeRun for the child node has no exports column or no
api/evidence/assembly.py:1665:    from api.models import JobNodeRun
api/evidence/assembly.py:1674:    # Index JobNodeRun exports by node_id for one-pass lookup.
api/evidence/assembly.py:1675:    runs = db.query(JobNodeRun).filter_by(job_id=job.id).all()
api/evidence/assembly.py:1763:    Walks every JobNodeRun's exports payload, extracts the engine's
api/evidence/assembly.py:1771:      - All JobNodeRun rows lack exports JSON (legacy / pre-R3.2 jobs).
api/evidence/assembly.py:1784:    from api.models import JobNodeRun
api/evidence/assembly.py:1786:    runs = db.query(JobNodeRun).filter_by(job_id=job.id).all()
api/evidence/assembly.py:1889:    """Build the manifest's ``nodes`` array from JobNodeRun rows.
api/evidence/assembly.py:1898:    rows_out + elapsed_ms (already on JobNodeRun from R1).
api/evidence/assembly.py:1908:    from api.models import JobNodeRun, JobStep
api/evidence/assembly.py:1910:    rows = db.query(JobNodeRun).filter_by(job_id=job.id).order_by(JobNodeRun.id).all()
api/evidence/assembly.py:1914:    # steps so the join key matches JobNodeRun.node_id. Non-graph jobs
api/evidence/assembly.py:1993:        # JobNodeRun.row_count we already had.
api/files/router.py:598:    each one before filtering by owner. It now starts from ``JobNodeRun``
api/files/router.py:605:    from api.models import Folder, Job, JobNodeRun, Pipeline
api/files/router.py:608:        db.query(JobNodeRun, Job, Pipeline)
api/files/router.py:609:        .join(Job, JobNodeRun.job_id == Job.id)
api/files/router.py:612:            JobNodeRun.kind == "target.file",
api/files/router.py:613:            JobNodeRun.status == "ok",
api/files/router.py:614:            JobNodeRun.exports.is_not(None),
api/files/router.py:627:    q = q.order_by(Job.created_at.desc(), Job.id.desc(), JobNodeRun.id.asc()).limit(
api/jobs/execution_summary.py:82:    from api.models import JobNodeRun
api/jobs/execution_summary.py:86:        db.query(JobNodeRun)
api/jobs/execution_summary.py:87:        .filter(JobNodeRun.job_id == job.id, JobNodeRun.kind == "mask")
api/jobs/execution_summary.py:88:        .order_by(JobNodeRun.id)
api/jobs/phase_timing.py:29:  value already summed into ``JobNodeRun.elapsed_ms`` in
api/jobs/routes_evidence.py:36:from api.models import Job, JobNodeRun, User
api/jobs/routes_evidence.py:187:    # persisted on JobNodeRun even when no creation-time manifest
api/jobs/routes_evidence.py:356:        db.query(JobNodeRun)
api/jobs/routes_evidence.py:358:            JobNodeRun.job_id == job_id,
api/jobs/routes_evidence.py:359:            JobNodeRun.kind == "target.file",
api/jobs/routes_evidence.py:360:            JobNodeRun.status == "ok",
api/jobs/routes_evidence.py:362:        .order_by(JobNodeRun.created_at, JobNodeRun.id)
api/jobs/routes_outputs.py:45:from api.models import Job, JobNodeRun, User
api/jobs/routes_outputs.py:82:        db.query(JobNodeRun, Job)
api/jobs/routes_outputs.py:83:        .join(Job, JobNodeRun.job_id == Job.id)
api/jobs/routes_outputs.py:85:            JobNodeRun.kind == "target.file",
api/jobs/routes_outputs.py:86:            JobNodeRun.status == "ok",
api/jobs/routes_outputs.py:87:            JobNodeRun.exports.is_not(None),
api/jobs/routes_outputs.py:92:    q = q.order_by(Job.created_at.desc(), JobNodeRun.id.asc())
api/jobs/routes_outputs.py:153:        db.query(JobNodeRun)
api/jobs/routes_outputs.py:155:            JobNodeRun.job_id == job_id,
api/jobs/routes_outputs.py:156:            JobNodeRun.kind == "target.file",
api/jobs/routes_outputs.py:157:            JobNodeRun.status == "ok",
api/jobs/routes_outputs.py:159:        .order_by(JobNodeRun.created_at, JobNodeRun.id)
api/jobs/routes_outputs.py:195:        db.query(JobNodeRun)
api/jobs/routes_outputs.py:197:            JobNodeRun.job_id == job_id,
api/jobs/routes_outputs.py:198:            JobNodeRun.node_id == node_id,
api/jobs/routes_outputs.py:199:            JobNodeRun.kind == "target.file",
api/jobs/routes_outputs.py:200:            JobNodeRun.status == "ok",
api/jobs/routes_runtime_facts.py:27:    JobNodeRunOut,
api/jobs/routes_runtime_facts.py:35:    JobNodeRun,
api/jobs/routes_runtime_facts.py:70:@router.get("/{job_id}/node-runs", response_model=list[JobNodeRunOut])
api/jobs/routes_runtime_facts.py:78:    Returns each JobNodeRun the graph runner wrote for this job, ordered
api/jobs/routes_runtime_facts.py:91:        db.query(JobNodeRun)
api/jobs/routes_runtime_facts.py:92:        .filter(JobNodeRun.job_id == job_id)
api/jobs/routes_runtime_facts.py:93:        .order_by(JobNodeRun.created_at, JobNodeRun.id)
api/jobs/routes_runtime_facts.py:97:    out: list[JobNodeRunOut] = []
api/jobs/routes_runtime_facts.py:99:        item = JobNodeRunOut.model_validate(r)
api/jobs/runner.py:306:    function, so the hooks cannot diverge. The hooks read ``JobNodeRun`` rows by
api/jobs/runner.py:579:                # The hooks read JobNodeRun rows by `kind`; the new worker
api/jobs/schemas.py:410:class JobNodeRunOut(BaseModel):
api/jobs/v2_cloud_materialize.py:544:    dict the caller persists into JobNodeRun.exports.
api/jobs/v2_cloud_materialize.py:558:    Caller's JobNodeRun.exports is the persistence boundary.
api/jobs/v2_full_frame.py:6:outputs, persist per-table-kind ``JobNodeRun`` telemetry, and set success
api/jobs/v2_full_frame.py:114:    # JobNodeRun.elapsed_ms (build_node_runs, below) -- traceable to an
api/jobs/v2_node_runs.py:1:"""V2 JobNodeRun assembly (evidence/manifest node construction).
api/jobs/v2_node_runs.py:6:raw per-table counters) into the ``JobNodeRun`` rows the platform persists
api/jobs/v2_node_runs.py:140:    """Build one JobNodeRun per masked output table from an ExecutionResult.
api/jobs/v2_node_runs.py:142:    Mirrors the V1 JobNodeRun creation (api/jobs/runner.py). `elapsed_ms` is the
api/jobs/v2_node_runs.py:150:    stashed in `JobNodeRun.exports`; quality warnings are serialized to
api/jobs/v2_node_runs.py:151:    `JobNodeRun.warnings` via `_warning_dict` (the canonical manifest shape, H2).
api/jobs/v2_node_runs.py:153:    the same warnings list as evidence-only observations. Returns unsaved JobNodeRun
api/jobs/v2_node_runs.py:172:    from api.models import JobNodeRun
api/jobs/v2_node_runs.py:220:            JobNodeRun(
api/jobs/v2_node_runs.py:238:    """Kind + exports payload for one ``target.<kind>`` JobNodeRun (S15 shape).
api/jobs/v2_node_runs.py:278:    """Build one ``target.<kind>`` JobNodeRun per written output table.
api/jobs/v2_node_runs.py:293:    from api.models import JobNodeRun
api/jobs/v2_node_runs.py:305:            JobNodeRun(
api/jobs/v2_node_runs.py:351:    from api.models import JobNodeRun
api/jobs/v2_node_runs.py:374:    mask_run = JobNodeRun(
api/jobs/v2_node_runs.py:386:    target_run = JobNodeRun(
api/jobs/v2_node_runs.py:407:    """Build one non-ok JobNodeRun for a failed V2 job (S3/RT-B). Carries the
api/jobs/v2_node_runs.py:419:    from api.models import JobNodeRun
api/jobs/v2_node_runs.py:426:    return JobNodeRun(
api/jobs/v2_node_runs.py:440:    """Build one JobNodeRun per generated output table from an ExecutionResult
api/jobs/v2_node_runs.py:449:    from api.models import JobNodeRun
api/jobs/v2_node_runs.py:454:            JobNodeRun(
api/jobs/v2_orchestrator.py:7:outputs, persists ``JobNodeRun`` telemetry per-table-kind, finishes the
api/jobs/v2_orchestrator.py:101:    persists JobNodeRun telemetry per-table-kind, finishes the manifest,
api/jobs/v2_runner.py:31:- ``v2_node_runs.py``: JobNodeRun assembly (mask/generate/target/streaming/
api/jobs/v2_vault.py:8:``target.vault`` JobNodeRun (``_persist_job_vault``). Every route persists the
api/jobs/v2_vault.py:193:    its `target.vault` JobNodeRun. Callers run this BEFORE the first target
api/jobs/v2_vault.py:227:    from api.models import JobNodeRun
api/jobs/v2_vault.py:280:    return JobNodeRun(
api/mcp_server.py:118:    # by construction -- the output endpoints filter JobNodeRun.kind=="target.file"
api/models.py:766:    node_runs: Mapped[list["JobNodeRun"]] = relationship(
api/models.py:767:        "JobNodeRun", back_populates="job", cascade="all, delete-orphan"
api/models.py:1308:class JobNodeRun(Base):
api/reporting/parsing.py:4:legacy rows), JobNodeRun warnings, schedules, uploaded-file rows and the
api/reporting/parsing.py:172:    """Load JobNodeRun.warnings rows for a job, grouped by node_id.
api/reporting/parsing.py:183:    from api.models import JobNodeRun as _JobNodeRun
api/reporting/parsing.py:185:    rows = db.query(_JobNodeRun).filter(_JobNodeRun.job_id == job_id).all()
api/reporting/schemas.py:61:    Populated from JobNodeRun.warnings entries the engine / runner
api/unmask/router.py:37:from api.models import JobNodeRun, User
api/unmask/router.py:89:        db.query(JobNodeRun)
api/unmask/router.py:91:            JobNodeRun.job_id == vault_job_id,
api/unmask/router.py:92:            JobNodeRun.kind == "target.vault",
api/unmask/router.py:93:            JobNodeRun.status == "ok",
```
<!-- /raw -->

## 4. CLI emit site and exit-1 tuple

`run.py` is identical at `b4f19ba`, `004afda9` and the merge base. One run-time failure emit: `src/decoy/cli/run.py:1056` (comment `src/decoy/cli/run.py:1033`); `src/decoy/cli/run.py:1167` is the success emit. The pre-run `--notify` usage refusal at `src/decoy/cli/run.py:455` also emits JSON before the engine is entered: a plan section 3 named limit, not a second failure site.

Tuple `src/decoy/cli/run.py:944` to `src/decoy/cli/run.py:993` (first member `src/decoy/cli/run.py:945`, last `src/decoy/cli/run.py:992`), in order.
Members: `PlanCompileError`, `PipelineValidationError`, `ConfigError`, `_ChunkedGenerateError`, `_ChunkedFixedWidthError`, `_ChunkedReportFlagError`, `_ChunkedVaultError`, `_VaultUsageError`, `_MaskSecretUsageError`, `DuckDBSourceError`, `MissingExtraError`, `UnsupportedCloudEndpointError`, `*_ner_unavailable_types`, `_native_gate.NativeGateError`, `_ConfigValidationError`, `*_mask_secret_error_types`

Starred groups: `_ner_unavailable_types` is `(_NerUnavailableError,)` or `()` (`src/decoy/cli/run.py:896`); `_mask_secret_error_types` is `(_MaskSecretError,)` or `()` (`src/decoy/cli/run.py:908`). Count: 16. Equal to plan C-T8, member for member and in order; no drift.

## 5. Frontend read

Raw string, not parsed: `web/src/pages/JobDetail.tsx:750` is `<td className="py-1.5 font-mono text-red-600 break-all">{n.error ?? ''}</td>` (`n` is a `/node-runs` row, `web/src/pages/JobDetail.tsx:698`, typed `error: string | null` at `web/src/pipelines/api.ts:906`). React renders it as escaped text. For P1 (not a stop): the Nodes tab shows the whole JSON string, `reason` payload included. Plan 1.5 (frontend untouched) holds; the plan's C0 line ("reads `error.message` as text") does not.

## 6. Consumer pins and registry versions

| Consumer | Pin on merge base | Pin at plan base | Registry of the pin |
|---|---|---|---|
| Platform | `pyproject.toml:43` `==0.18.0` | `pyproject.toml:40` `==0.17.0` at `5e610156` | `1.2.0` |
| CLI | `pyproject.toml:107` `==0.18.0` | `pyproject.toml:103` `==0.17.0` at `004afda9` | `1.2.0` |

`src/decoy_engine/reasons/_registry.py:24`: `1.1.0` at `v0.17.0`, `1.2.0` at `v0.18.0`, `1.3.0` on engine main. The repin branches merged (Platform `a5cd954f` #149, CLI `2577062` #79); `git ls-remote origin refs/heads/chore/repin-engine-0.18.0` prints nothing in both repos. C1 and P1 assert `registry_version` as `<pinned>` (now `1.2.0`), never a literal. `reason_adapter.py` changed between `v0.17.0` and `v0.18.0`; the plan read it at `v0.17.0`.

## 7. Citation self-check script and output

Extract the block below to `c0_check.py`, then from the engine root: `P_DIR=<platform> P_SHA=0638a97d1d98a50630ecc694f49c5a45062261ab C_DIR=<cli> C_SHA=257706265498140466142477d2249eee0957763f E_DIR=. E_SHA=9eec6123114e0395eafe46d2bae395c6ebccf74b python3 -I c0_check.py <record>`. It prints each `CITE` line it read (MISMATCH when the expected text is absent), flags a body `file:line` with no `CITE` (UNCHECKED; script and raw blocks excluded), and runs A1 to A3.

<!-- script -->
```python
import os, re, subprocess, sys
R = {k: (os.environ[k + "_DIR"], os.environ[k + "_SHA"]) for k in "PCE"}
text = open(sys.argv[1], encoding="utf-8").read()
body = re.sub(r"<!-- (script|raw) -->.*?<!-- /\1 -->", "", text, flags=re.S)
bad, seen = 0, set()
for repo, rev, path, cites in re.findall(r"^CITE\|(\w)@?([\w.]*)\|([^|]+)\|(.+)$", text, re.M):
    src = subprocess.run(["git", "-C", R[repo][0], "show", f"{rev or R[repo][1]}:{path}"],
                         capture_output=True, text=True).stdout.splitlines()
    for n, want in (c.split("=", 1) for c in cites.split(" ;; ")):
        got = src[int(n) - 1].strip() if 0 < int(n) <= len(src) else ""
        bad += want not in got or not got
        seen.add((path.split("/")[-1], int(n)))
        print("OK      " if got and want in got else "MISMATCH", f"{repo} {path}:{n}: {got}")
for m in re.finditer(r"([\w/.-]+\.(?:py|tsx|ts|toml)):(\d+)", body):
    if (m.group(1).split("/")[-1], int(m.group(2))) not in seen:
        bad += 1; print("UNCHECKED", m.group(0))
sec = lambda n: (re.search(rf"^## {n}\b.*?(?=^## |\Z)", body, re.S | re.M) or [""])[0]
for name, need, s in (("A1", ["v2_runner.py:269", "v2_sequential.py:", "v2_full_frame.py:", "queue_worker.py:419",
        "queue_worker.py:434", "scheduler_claim_loop.py:173", "_key_setup_error"], sec(2)),
        ("A2", ["routes_runtime_facts.py:70", "assembly.py:2408", "routes_evidence.py:190", "routes_evidence.py:164"], sec(3))):
    rows = [r for r in s.splitlines() if r.startswith("| ") and re.search(r"\.py:\d+", r)]
    miss = [k for k in need if not any(k in r for r in rows)]
    bad += bool(miss); print(name, "OK" if not miss else "MISSING " + ", ".join(miss))
m = re.search(r"^Members: (.+)$", sec(4), re.M); c = re.search(r"Count: (\d+)", sec(4))
k = len(re.findall(r"`[^`]+`", m.group(1))) if m else 0
bad += not (k == 16 and c and int(c.group(1)) == k); print("A3", k, "listed, count line", c and c.group(1))
print("RESULT:", bad, "mismatches"); sys.exit(bad > 0)
```
<!-- /script -->

```
CITE|P|api/jobs/v2_orchestrator.py|173=_key_setup_error = _key_exc ;; 245=try: ;; 251=if _budget_error is not None ;; 252=raise _budget_error ;; 254=raise _key_setup_error ;; 255=if _token_vault_error ;; 256=raise _token_vault_error ;; 278=execute_streaming(ctx ;; 280=dispatch_relationship_route(ctx ;; 282=execute_full_frame(ctx ;; 289=except Exception as exc: ;; 301=details = _failure_details(exc) ;; 329=_error_node_run(
CITE|P|api/jobs/v2_node_runs.py|426=return JobNodeRun( ;; 434=error=json.dumps({"code": code, "message": message
CITE|P|api/jobs/v2_runner.py|269=def _run_v2_pipeline_streaming( ;; 426=raise
CITE|P|api/jobs/v2_stream_coordinator.py|237=raise PartialPublicationError(
CITE|P|api/jobs/route_dispatch_exec.py|46=def execute_streaming( ;; 108=run_full_frame_branch( ;; 150=run_sequential_relationship_job(
CITE|P|api/jobs/v2_sequential.py|460=_persist_job_vault(vault_writer ;; 486=def run_sequential_relationship_job(
CITE|P|api/jobs/v2_full_frame.py|63=raise TokenVaultDurabilityError( ;; 70=def run_full_frame_branch( ;; 175=_persist_job_vault(vault_writer
CITE|P|api/jobs/v2_vault.py|91=super().__init__(self.message) ;; 279=raise VaultPersistError() from exc ;; 280=return JobNodeRun(
CITE|P@67e8cbba|api/jobs/v2_vault.py|98="message": str(exc)
CITE|P|api/jobs/queue_worker.py|378=def _fail_host_safety( ;; 384=full = f"host-safety: {reason} ;; 385=[host-safety] rejected before read ;; 413=_fail_host_safety(db, job, str(exc)) ;; 417=reservation = price_job_at_claim(job) ;; 419=could not price fail-closed ;; 430=claim_route.disk_reason ;; 434=_fail_host_safety(db, job, decision.reason) ;; 604=JobStatus.failed ;; 607=job.log = (job.log or "") + (
CITE|P|api/jobs/scheduler_claim_loop.py|89=def _fail_admission( ;; 173=_fail_host_safety(db, job, outcome.reason)
CITE|P|api/jobs/reaper.py|219="status": JobStatus.failed
CITE|P|api/jobs/job_terminal_cas.py|247="status": JobStatus.failed
CITE|P|api/jobs/iteration.py|499=JobStatus.failed ;; 532=JobStatus.failed ;; 625=JobStatus.failed if any_failed ;; 651=JobStatus.failed
CITE|P|api/jobs/routes_runtime_facts.py|70=@router.get("/{job_id}/node-runs" ;; 99=JobNodeRunOut.model_validate(r) ;; 115=return out
CITE|P|api/jobs/worker_budget.py|393=except Exception as exc ;; 402=could not be parsed fail-closed: {exc} ;; 590=if reservation.rejected ;; 595=could not price job fail-closed: {reservation.reason}
CITE|P|api/jobs/_shared.py|203=if include_log and job.log is not None ;; 211=log_payload = job.log ;; 241=admission_reason=getattr(job, "admission_reason"
CITE|P|api/jobs/admission.py|992=except (UnsizeablePhaseError, ParquetFooterUnreadableError) ;; 998=admission phase pricing rejected (fail-closed): {exc}
CITE|P|api/jobs/_create_phases.py|554=admission_reason=admission.reason
CITE|P|api/jobs/iteration_dispatch.py|145=def _mark_child_failed( ;; 156=JobStatus.failed
CITE|P|api/jobs/job_principal.py|76=def land_owner_refused( ;; 89=JobStatus.failed
CITE|P|api/jobs/schemas.py|430=error: str | None
CITE|P|api/evidence/assembly.py|1888=def _hydrate_nodes( ;; 1984="message": row.error ;; 2334=def update_finished_manifest( ;; 2408=_hydrate_nodes(job, db)
CITE|P|api/jobs/routes_evidence.py|164=manifest = job.evidence_manifest ;; 166=return manifest ;; 190=_hydrate_nodes(job, db)
CITE|P|api/evidence/schema.py|328=class NodeError(
CITE|P|web/src/pages/JobDetail.tsx|698=nodeRuns.map(n ;; 750={n.error ?? ''}
CITE|P|web/src/pages/reporting/JobDetailPanel.tsx|792=function NodeRunsCard( ;; 831={n.error}</div>
CITE|P|web/src/pipelines/api.ts|906=error: string | null
CITE|P|pyproject.toml|43="decoy-engine==0.18.0"
CITE|P@5e610156|pyproject.toml|40="decoy-engine==0.17.0"
CITE|C|src/decoy/cli/run.py|455=emit_json( ;; 896=_ner_unavailable_types: tuple = (_NerUnavailableError,) ;; 908=_mask_secret_error_types: tuple = (_MaskSecretError,) ;; 944=( ;; 945=PlanCompileError, ;; 992=*_mask_secret_error_types, ;; 993=), ;; 1033=one emit_json call site for a run-time failure ;; 1056=emit_json(state, payload) ;; 1167=emit_json(state, payload)
CITE|C|pyproject.toml|107="decoy-engine==0.18.0"
CITE|C@004afda9|pyproject.toml|103="decoy-engine==0.17.0"
CITE|E@v0.17.0|src/decoy_engine/reasons/_registry.py|24=REGISTRY_VERSION = "1.1.0"
CITE|E@v0.18.0|src/decoy_engine/reasons/_registry.py|24=REGISTRY_VERSION = "1.2.0"
CITE|E|src/decoy_engine/reasons/_registry.py|24=REGISTRY_VERSION = "1.3.0"
```

Output on the merge bases: one `OK` line per `CITE` anchor (96 at this head; the original 71 were checked as 69 until the tag regex learned dotted tags such as `v0.17.0`), each printing the line it read (elided here for the 300-line cap; rerun to see them), then `A1 OK`, `A2 OK`, `A3 16 listed, count line 16`, `RESULT: 0 mismatches`, exit 0. Red on the empty record first (A1, A2, A3 missing, exit 1). Mutations, each on a copy, each exit 1: delete row F, `A1 MISSING v2_full_frame.py:` (C0-A1); strip row H4's `file:line`, `A1 MISSING queue_worker.py:434`; delete the legacy-synthesis row, `A2 MISSING routes_evidence.py:190` (C0-A2); drop `_ChunkedVaultError`, `A3 15 listed, count line 16` (C0-A3); CITE `289` to `290`, `MISMATCH` on the shifted line plus `UNCHECKED` for the old one; body `:289` to `:290`, `UNCHECKED` on it (C0-A4). A reviewer holding the plan's C0 list finds a deleted row F missing by name.

## 8. Same-class greps

<!-- raw -->
```
$ git -C decoy-platform grep -n "error=json.dumps\|error=str(" api/     # 0638a97d
api/jobs/v2_node_runs.py:434:        error=json.dumps({"code": code, "message": message, "where": where}),
api/pipelines/router.py:479:        return IterationPreviewOut(count=0, values=[], error=str(exc))
api/pipelines/router.py:484:        return IterationPreviewOut(count=0, values=[], error=str(exc))
$ git -C decoy-platform grep -n "_fail_host_safety" api/
api/jobs/queue_worker.py:378:def _fail_host_safety(db: Any, job: Job, reason: str) -> None:
api/jobs/queue_worker.py:413:        _fail_host_safety(db, job, str(exc))
api/jobs/queue_worker.py:419:        _fail_host_safety(db, job, f"could not price fail-closed: {reservation.reason}")
api/jobs/queue_worker.py:430:        _fail_host_safety(db, job, claim_route.disk_reason)
api/jobs/queue_worker.py:434:        _fail_host_safety(db, job, decision.reason)
api/jobs/scheduler_claim_loop.py:171:        from api.jobs.queue_worker import _fail_host_safety
api/jobs/scheduler_claim_loop.py:173:        _fail_host_safety(db, job, outcome.reason)
api/observability/metrics.py:39:rejection (``queue_worker._fail_host_safety``), admission rejection
$ git -C decoy grep -n "emit_json(state, payload)" src/decoy/cli/run.py     # 25770626
src/decoy/cli/run.py:1056:            emit_json(state, payload)
src/decoy/cli/run.py:1167:        emit_json(state, payload)
```
<!-- /raw -->

The one node-error write is census row K's; the two `pipelines/router.py` hits are `IterationPreviewOut`, not `JobNodeRun`. Every host-safety tail is rows H1 to H5. The emit hits are the failure and success sites of section 4.

## 9. V1 leaks found, not fixed here

- Owned by F31 slice P2a. `worker_budget.py:393` to `worker_budget.py:402` interpolates `str(exc)` (YAML parse text) into `reservation.reason`; `queue_worker.py:417` to `queue_worker.py:419` forwards it to `_fail_host_safety`, which appends it to `Job.log` (`queue_worker.py:384`, `queue_worker.py:385`); `_shared.py:203` to `_shared.py:211` returns `Job.log` to the job owner. Second route: `worker_budget.py:590` to `worker_budget.py:595` wraps it into `LedgerDecision.reason`, passed at `queue_worker.py:434`. Class 1; V1 rule 3 counts `str(exc)`. `queue_worker.py:385` and `queue_worker.py:607` are P2a terminal writers; the fix point is `_fail_host_safety` taking a closed reason (F31-FILE-MAP.md, errata E-X1).
- Owned by the F31 platform slice. `admission.py:992` to `admission.py:998` puts `{exc}` of `UnsizeablePhaseError` or `ParquetFooterUnreadableError` into `PhaseReservation.reason`; `_create_phases.py:554` stores it as `Job.admission_reason`; `_shared.py:241` returns it in the job response (F31-FILE-MAP.md, orchestrator addition 2026-11-03, L-X2).
## 10. V2-LOG

- Census limits (V2-LOG, class 4). The guarded tail at `v2_orchestrator.py:251` to `v2_orchestrator.py:256` raises `_budget_error`, `_key_setup_error` and `_token_vault_error`; row K names only the second. Failed-status writers missing from row X, none writing a node or reason: `iteration_dispatch.py:145` to `iteration_dispatch.py:156`, `job_principal.py:76` to `job_principal.py:89`, `iteration.py:532`, `iteration.py:625`, `iteration.py:651`.
- Second raw node-error renderer: `web/src/pages/reporting/JobDetailPanel.tsx:831` (`NodeRunsCard`, `JobDetailPanel.tsx:792`) prints `{n.error}` like `JobDetail.tsx:750`; both consume `/node-runs`. The other `.error` renderers in `web/src` are non-node (mirror runs, uploads, previews).
- `JobDetail.tsx` will show P1's `reason` JSON inline in the Nodes tab (section 5). Class 10 (wording, no value).

## 11. Not verified

- No Platform, CLI or engine test run against these anchors; all read from source at the merge bases. Failures raised before the orchestrator's `try` (outside rows K to F) are not enumerated ("every entry point" is V2).
- Whether the plan's P-T and C-T tests still hold at engine `0.18.0` (`reason_adapter.py` and three registry modules changed since `v0.17.0`), and whether each `reservation.reason`, `disk_reason` and `decision.reason` string is value-free.
