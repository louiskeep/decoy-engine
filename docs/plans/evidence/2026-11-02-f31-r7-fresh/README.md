# F31 revision 7: fresh anchors, attribute allowlist, sink sizing

Rules consulted: 00-universal, development-loop, risk-and-exceptions, testing (same set as the plan).

Heads: decoy-platform `origin/main` `0638a97d` (descends from `27304856`), decoy-engine
`origin/main` `492f35ac`. Probes are stdlib `ast`, run with the container Python 3.11.2, NOT the
`uv.lock` interpreter, over a `git archive` of each head; nothing is imported from the targets.
No test suite was run. Revision 6 numbers were at platform `d8908f42`; the same probes were re-run
there for the diffs below.

## Files

| File | Probe | Result |
|---|---|---|
| `logassign-0638a97d.txt` | `../2026-11-01-f31-r3-census/logassign.py api` | 28 `.log` assignments in 17 files (unchanged count; five lines moved) |
| `httpexc_kinds-0638a97d.txt` | `../2026-11-01-f31-r4-anchors/httpexc_kinds.py api` | 66 sites: text 60, attr 5, classname 1 (was 60: 56, 3, 1) |
| `census-0638a97d.txt` | `../2026-11-02-f31-r6-callee/excbound.py <root>` (revision 6 list) | pass 1 56 rows in 28 files, pass 2 31 rows (was 56 in 29 files, 30) |
| `census-allow-0638a97d.txt` | `excbound_allow.py <root>` (allowlist) | pass 1 56, pass 2 54 rows: 110 total |
| `excattrs-0638a97d.txt` | `excattrs.py api` | every attribute read on a caught exception, with counts; input to the allowlist |
| `httpexc_in_except-0638a97d.txt` | `httpexc_in_except.py api` | 123 `HTTPException` calls inside `except`; 66 read the bound name, 57 do not |
| `selftest_allow-r6-probe.txt` | `selftest_allow.py` against the revision 6 probe | 3 MISS, exit 1 (red) |
| `selftest_allow-r7-probe.txt` | `selftest_allow.py` against `excbound_allow.py` | all ok, exit 0 (green) |

The revision 6 `selftest.py` passes against both probes.

## What changed since `d8908f42`, and why

- `HTTPException` text sites +4, all from #143 (the 0.17.0 repin): `jobs/_service.py:237`,
  `jobs/routes_preview.py:163`, `pipelines/router.py:1384`, `unmask/router.py:133`. Each copies
  `MaskSecretConfigError.message`, a constant per code (`api/keys/mask_secret_errors.py:15-45`).
  Attr-only reads +2 (`nested_preview/router.py:110`, `unmask/router.py:171`, `.code`). Admin rows
  unchanged at 10.
- `exc-copy`, revision 6 probe: `jobs/v2_vault.py:91` left (#152 raises a fixed
  `VaultPersistError`); `pipelines/v2_validation.py:290` (pass 1, #143) and the store
  `jobs/v2_config.py:282` (pass 2, #150 F21 slice 1) arrived. 87 rows at 76 sites.
- `add_error` sites in `pipelines/v2_validation.py`: 8 to 10 (`:290` #143, `:298` #150).

## Allowlist (Dennis, class 8)

Revision 6 treated a fixed list of attribute names as text (`args`, `message`, `reason`, `detail`,
`msg`, `error`, `summary`, `description`, `stderr`, `stdout`); any other attribute escaped.
`excbound_allow.py` inverts it: any attribute read in a copy position is a text read unless it is
in `ALLOW = {code, errno, status_code, transient, cap}`, each value-free by its producers' types at
`0638a97d`. It reuses the revision 6 probe source and swaps only the attribute test.

The 23 pass-2 rows the allowlist adds, classified by reading the code at `0638a97d`:

| Site (api/...) | Attribute or callee | Decision |
|---|---|---|
| admin/sso_router.py:171 | `exc.response.status_code` (int) | none: `require_admin` route, int only |
| jobs/claim_source_identity.py:70, :145; jobs/claim_source_metadata.py:220, :231, :388 | `CloudClientError.cause` (code), `exc_type` (type name), `transient` | none: closed cause plus a type name into `Unavailable` and a debug log |
| jobs/iteration_dispatch.py:388 | `exc.kind` | none: rendered from two literals |
| jobs/preflight_cloud.py:219, :220 | botocore `exc.response` HTTP status and error code | none expected: int status, AWS code token and `logical_name` (an owner name) into `PreflightRejectedError`; P4d's build re-checks the producer |
| jobs/v2_config.py:281 | `exc.field` (a closed descriptor key) | none; the text then reaches the validation sink, closed by P7 |
| launcher/launch.py:185, :227 | `labels`, `container_id`, `image_ref` | V2-LOG [1]: launcher RPC, internal |
| scheduler/txn.py:170 | `exc.result` (a work result) | none: not text |
| environments/bindings.py:286 (two chains) | `_validation_summary(exc)`: `loc` and pydantic `type` only | none: owner names and closed tokens |
| generation/route_args.py:126 | `error.code`, `error.retry_after` (int) | none |
| jobs/_partial_publication.py:108 | `exc.published_manifest.metadata_for(table)` | none: owner result (publication metadata the success path also records) |
| jobs/_partial_publication.py:188, :204 | `__cause__` walk; `failed_table` (owner name) into `where` | F29 P1 (walk kept byte for byte) |
| jobs/v2_lifecycle.py:73 | `exc.ended_status` (a status enum) | none |
| jobs/worker_tmpdir.py:97 (three chains) | `detail.startswith(...)` | none: a predicate |

None needs V1 work. The sentry's baseline at P4d's base is these 110 rows less the rows P4d and
P4e rewrite.

## Sink sizing (Dennis re-frame)

`httpexc_in_except.py`: a global handler that withholds every in-`except` detail not built by a
closed constructor would also hit the 57 constant details raised inside `except` blocks, and any
`HTTPException` raised while another exception is being handled, which a lexical count cannot
bound. Revision 7 adopts sink closure where one sink exists (the validation response, the two
refusal mappers, F29's error-node reader, `check_database`) and keeps per-site rewrites for
`HTTPException` details.
