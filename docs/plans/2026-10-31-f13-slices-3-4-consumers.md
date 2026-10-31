Status: handoff, plan only (revision 1, Opus planner). No implementation, release or deployment approval is granted by this document.

Summary: the platform sources the engine mask secret from its existing instance master key (HKDF with its own domain label, wrapped in an engine `SecretKeyProvider`) and hands that one provider to every engine entry it calls; the CLI resolves the secret from `--mask-secret` or `global_settings.mask_secret_ref` through one shared helper used by `run`, `tokenize`, `unmask`, `report vault` and `decoy.mask()`. Neither consumer ever falls back to the job seed. Platform job configs may not name a secret reference at all.

Baselines: engine `origin/main` `545d913` (contains #339, `6ce435c`); platform `origin/main` `a4f5857`; CLI `origin/main` `c459758`. Consumer pins as read: platform `decoy-engine==0.16.0` (`decoy-platform/pyproject.toml:36`), CLI `decoy-engine==0.15.1` (`decoy/pyproject.toml:99`). Slice 0 vault floor plan: `origin/plan/slice0` (`docs/plans/2026-10-31-slice0-vault-key-floor-and-simpleeval.md`). Nothing in this plan was executed; every claim is from reading code at these SHAs.

Rules consulted: dev-rules `00-universal`, `risk-and-exceptions` (R2/R3 table), `config-secrets-and-supply-chain` (injection mechanism, rotation, no unsafe default), `security`, `api-and-compatibility`, `testing`; `DEFECT-CATALOG.md`, `OWNER-DECISIONS-2026-10-30.md` (V1 threat model, 2026-10-31 pre-release ruling) and `HOSTILE-PLAN-REVIEW.md` (decoy-platform `handoffs/queue`); engine, platform and CLI `CLAUDE.md`; `docs/security/secret-providers.md`; `docs/security/key-derivation.md`.

Risk: R3 for every slice that changes which bytes key a mask or a vault (platform 3a, 3b; CLI 4b). Why R3 and not R2: the rulebook R3 trigger is "credential rotation / irreversible production mutation" (`risk-and-exceptions.md:24`). Activating these slices on any instance that already holds masked data or vaults is a key rotation in effect: every keyed output changes identity and every existing vault becomes unreadable (section 7). The code merge is reversible; the activation on a live instance is not, so the activation step needs Cam's explicit authorization immediately before it, and the gate is Codex plus a Dennis pass that did not author this plan. Test-only, docs-only and the dedup refactor (4a) are R2. If Cam confirms no instance outside disposable dev exists (the pre-release ruling suggests so, the repos cannot prove it), 3a/3b/4b may be re-tiered to R2 by the owner, not by the worker.

Threat model: V1 (self-hosted single-org, trusted engine process). In scope: an accidental leak of a secret, a derived key, a reference or a path into a log, error, report, manifest or response; a tenant-authored job config choosing a host secret source; cache or key mix-up between two steps of one job under a realistic interleaving. Out of scope (V2 note): a hostile in-process object, a hostile `KeyProvider`, `BaseException(secret)` from a provider (`OWNER-DECISIONS` 2026-10-30).

# F13 slices 3-4: platform provider injection and CLI secret resolution, PLAN

## 1. Problem, as verified (and where the handoff text drifted)

- F13 is NOT in either consumer pin. At `v0.16.0` (`5da40c2`) `git merge-base --is-ancestor 6ce435c 5da40c2` is false and `secret_providers/__init__.py` does not exist (`git show 5da40c2:src/decoy_engine/secret_providers/__init__.py` fails). So today both consumers run a pre-F13 engine and work; the break happens only at the pin bump.
- The break is wider than vaults. Engine main treats every column in `ALWAYS_KEYED_STRATEGIES` (`secret_providers/_requirements.py:151-166`: `fpe, hash, date_shift, shuffle, bucket_perturb, text_mask, code_set, grouped_series, windowed_date, group_key, joint_mask, tokenize`), every `deterministic: true` column, every `vault: true` column and every composite-FK group (`_requirements.py:209-235`) as keyed. A keyed config with no admitted secret raises `KeyedStrategyRequiresSecret` (`_preflight.py:305-318`; direct entries via `require_mask_key`, `keyprovider.py:209-227`, called at `execution/_sequential.py:332` and `execution/native/_chunked_entry.py:260`). The platform passes no `key_provider` anywhere (grep of `api/` for `key_provider` finds only `api/engine_a8_guard.py:121`, a probe), so after a naive pin bump every keyed platform job and every `POST /unmask` fails closed, vault or not.
- Handoff citation drift (checked): CLI vault creation is `decoy/src/decoy/cli/run.py:789-791` (`vault_writer_for_config(config_dict)`) and `run_pipeline` is `:821`; the handoff's `:831-835` points into the `run_pipeline` argument list. Platform `v2_runner.py:225-232` is the tail of the call that starts at `:226`. `report.py:2290-2305` and `v2_vault.py:23-40` are exact.
- The CLI inspector is already wrong on the pinned 0.15.1 for any secret-keyed vault: `vault_writer_for_config` at `v0.15.1` resolves `global_settings.mask_secret_ref` into the writer key (`git show v0.15.1:src/decoy_engine/vault.py`, function `vault_writer_for_config`), while `report vault` opens with `job_seed_for_config(raw)` (`report.py:2295`, `:2305`). By reading, `decoy report vault` cannot open a vault that `decoy run --vault --mask-secret ...` wrote. Not run (see section 15).
- The CLI has no `--mask-secret` on `unmask` or `report vault` (grep of `src/decoy/cli/unmask.py` and `report.py`: none); only `run.py:237`, `tokenize.py:124` and `api.py:326`. `decoy unmask` can only use a YAML `mask_secret_ref`. Its docstring still says "The config carries the seed: treat it as a key" (`cli/unmask.py:48-49`), which F13 made false.
- Three copies of the reference predicate exist and accept only `env:`/`file:` prefixes: `run.py:165`, `tokenize.py:98`, imported by `api.py:37`; the flag-injection logic is duplicated in `run.py:605-665` and `api.py:115-146`. The CLI therefore cannot name a `vault://` ref today even if the engine could resolve it.
- Stale engine text to fix in slice E0: `config/_global_settings.py:84-95` still says "pre-GA the keyed surface falls back to `job_seed`".

## 2. Established solutions (cited, from knowledge; not re-fetched this session)

- Vault Agent (HashiCorp): the agent authenticates (auto-auth), caches nothing for the app, and renders secrets to a file through a `template` stanza or a token to a file sink; the application reads a local file and never holds Vault credentials. Applied here: for the CLI the sanctioned external-store path is a `file:` reference fed by Vault Agent, Kubernetes Secret volume or systemd `LoadCredential` (`$CREDENTIALS_DIRECTORY`), with zero new Decoy code. The engine's native `vault://` adapter stays opt-in and is not wired into the CLI in this plan (secret-providers.md: the caller authenticates first).
- Docker/Compose and Kubernetes secrets guidance: prefer a mounted file (`/run/secrets/NAME`) over an environment variable, because env is inherited by child processes and visible in `/proc/<pid>/environ` to the same user and in crash dumps. Applied: `file:` is documented first, `env:` second (CI), no implicit default.
- The Twelve-Factor "config in the environment" convention: the ref names an env var, the value is not in the config file. Applied: `env:NAME` stays; the config never holds a secret.
- Key hierarchy by HKDF with a distinct `info` per purpose (RFC 5869 section 3.2, NIST SP 800-108 domain separation): the platform already does this (`api/keys/derive.py:28-35`, `:68-86`); the new secret is one more labeled child of the master. Cited in the new function's docstring (project rule: cite the source pattern).
- Rotation by versioned reference (Vault KV v2 `version=`): already the engine design (`secret-providers.md` "Rotation"); this plan adds only the honest consumer-side statement (section 7).
- SSH and GnuPG refuse or warn on group/world-readable private key files; Vault Agent file sinks default to 0640. Not adopted in this plan (owner question Q5), noted so the choice is explicit.

## 3. Decisions

### 3.1 Config surface

| Consumer | Who names the secret | Surface | Not allowed |
|---|---|---|---|
| Platform | The deployment, never the job author | Existing master key sources only: `DECOY_MASTER_KEY` (env, base64 32 bytes), `{DECOY_KEY_DIR}/.decoy_master_key` (file, 0600, auto-generated), `DECOY_MASTER_KEY_PROVIDER=kms_aws\|kms_gcp` (`api/keys/master.py:1-12`, `:144-176`, `api/keys/kms.py:179`). No new setting. The engine secret is `HKDF-SHA256(master, info="v2-mask-secret:instance")`, 32 bytes, wrapped as `SecretKeyProvider(secret, key_version="v1")` and passed as `key_provider=` at every engine entry. | `global_settings.mask_secret_ref` in a job config: rejected at config validation with code `mask_secret_ref_not_allowed` (present-but-null allowed, present-and-anything-else rejected, including `""`). A second platform secret (`DECOY_MASK_SECRET`). |
| CLI | The operator, per run | `--mask-secret REF` or `global_settings.mask_secret_ref` (exactly one place, existing rule `run.py:617-628`), now also on `decoy unmask` and `decoy report vault`. Grammar is the engine's `parse_ref` after the 4b pin bump (so `?encoding=` and `?key_version=` work); `env:` and `file:` resolve out of the box. `vault://` parses but yields `secret_provider_unknown` (fixed code) because the CLI registers no provider; opt-in Vault registration is a separate demand-gated slice (Q3). | Any implicit source: no `DECOY_MASK_SECRET` auto-read, no default key file, no CLI-generated key. The flag stays env-free (`run.py:617` comment). |

Why the platform derives from the master key instead of a new variable or a ref: the master key already has backup reminders, a fingerprint, restore flow and KMS wrapping (`api/keys/key_restore.py`, `backup_reminder.py`, `kms.py`, tests `test_r4_5_key_backup_restore.py`, `test_master_key_kms.py`). A second secret outside that machinery could be lost or leaked outside it. A ref in the job config would let a job author point the server at `env:` or `file:` sources (a read oracle on host secrets), which is the tenant-isolation class this threat model keeps in scope.

Why HKDF domain separation matters here and is tested: if the new `info` string equalled `v2-job-seed:mask:instance` (`derive.py:~70`), the public 8-byte job seed (stored in Seed rows and plans) would equal the first 8 bytes of the engine secret (`derive_mask_job_seed_int` takes `[:8]` of the same HKDF output). The label `v2-mask-secret:instance` is distinct, and the test asserts no 8-byte prefix relation and no equality with any `derive_pipeline_key` output.

### 3.2 Key sourcing per environment (no weak default)

| Environment | Sanctioned source | Notes |
|---|---|---|
| Platform dev, no external store | The auto-generated `.decoy_master_key` (random 32 bytes, 0600, `master.py:117-143`) | Existing behavior, now also keys masking. No fixed key anywhere in `api/` except the A-8 probe (`engine_a8_guard.py:121`, `bytes(range(32))`), which is a test-only self-check and gets a sentry entry so it cannot be imported by job code. |
| Platform prod | `DECOY_MASTER_KEY` or KMS-wrapped provider | Unchanged. |
| CLI dev, no external store | `file:` to a 0600 file made with `umask 077; openssl rand -hex 32 > ~/.config/decoy/mask.key`, or `export DECOY_MASK_SECRET=$(openssl rand -hex 32)` with `env:DECOY_MASK_SECRET` | Docs-only recipe in `decoy explain keys`. Matches the engine's own guidance string (`keyprovider.py:201-206`). Trailing newline is stripped by the engine decoder (`_requirements.py:106-125`, `"".join(text.split())`). |
| CLI CI | `env:NAME` from the CI secret store | |
| CLI/any with Vault, KMS, k8s | Vault Agent template, k8s Secret volume or systemd credential rendered to a file, referenced as `file:` | No Decoy code. |

The engine cannot certify strength: it enforces length only (`MIN_SECRET_BYTES=32`, `keyprovider.py:54`; `secret-providers.md`: "Pattern and entropy scans never certify key strength"), so 64 zero hex digits pass. "No weak default" is therefore a rule about where keys come from: nothing in either consumer supplies, generates or falls back to key bytes except a CSPRNG (`secrets`, as `master.py:135` does). A CLI `decoy keys init` generator is optional (Q4); the plan recommends docs-only.

### 3.3 One resolution path (creation, run and inspection)

- Platform: `api/jobs/engine_key.py` (new, under 60 LOC) owns `instance_key_provider()`. It is called ONCE per job at `v2_orchestrator.py:152` (next to `resolve_job_seed`) and the same object is threaded to the vault writer, the run entry, and (for unmask) a fresh call per request. It is not cached at module level, so `api.keys.master.reset_cache()` and the restore flow (`key_restore.py`) cannot leave a stale provider.
- CLI after 4b: one `AdmissionScope` per command (`secret_providers.AdmissionScope`, `_preflight.py:284`). `run` calls `admit_mask_key(config_dict)` once and passes `admitted.key_provider` to BOTH `vault_writer_for_config(config_dict, key_provider=...)` and `run_pipeline(..., key_provider=...)` / `run_mask_pipeline_chunked`. `report vault` and `unmask` build the key the same way (`admit_mask_key` then `provider.mask_key()` into `load_vault`; `unmask_pipeline` admits internally at `unmask.py:247-257`). Before 4b (pin 0.15.1) the same helper uses `decoy_engine.keyprovider.key_provider_from_ref`, which exists on both pins (`keyprovider.py:178`; `v0.15.1` `vault.py` imports it).

## 4. Guarantee (invariants as properties)

- **P1 (no seed key).** No engine entry called from `api/` or `src/decoy/` runs with a key derived from `job_seed`. Every keyed run, vault write, vault read and unmask in both consumers uses a provider whose `mask_key()` is at least 32 bytes.
- **P2 (one key per job/command).** Within one platform job, every table, route, chunk and the vault writer use the same provider object. Within one CLI command, the run and the vault use one admitted key.
- **P3 (no tenant-chosen source).** A platform job config can never cause the server to read an env var, file or provider named by the job author.
- **P4 (value-free).** No secret, derived secret, derived mask key, reference text or secret-bearing path appears in any consumer output surface (section 7.5), for success and every failure code.
- **P5 (fail closed with a registered code).** Every secret failure surfaces as an engine-registered code (`reasons.runtime`, `reasons/runtime/_part2.py:358` and `_part3.py:255` and the seven F13 codes) or the single platform code in 3.1, never a 500 or an unclassified exception.
- **P6 (rotation is explicit).** A key change never silently re-keys or invalidates a vault: the failure is `vault_key_mismatch` and the docs say so (section 8).

## 5. Class table

| # | Class | Closing structure | Proving test | Mutation that must turn it red |
|---|---|---|---|---|
| 1 | Value-bearing output | Fixed engine text for every secret failure (`_errors.py`, no chained cause); platform and CLI add no message that formats a ref, path or key; one helper builds the CLI usage errors; the platform `unmask` router maps `MaskSecretError` to `{code, message}` from `exc.code` and the engine constant | T6 seeded-marker corpus: a marker secret in `DECOY_MASTER_KEY` / key file / `--mask-secret` raw paste / `env:` value / `file:` path / `mask_secret_ref` name, swept over every surface in 7.5 for success and each failure, each with a non-empty positive control (the derived key IS produced) | Format `repr(provider.secret)` into the `vault_write_failed` message (`v2_vault.py:98` pattern), or echo the ref in `_MaskSecretUsageError`: T6 fails |
| 2 | Cleanup missing on an exit path | CLI: `AdmissionScope` as a context manager around run, report and unmask (closes the `SecretBuffer` on every exit). Platform: the provider holds an immutable `bytes` derived per job and is dropped with the job context; vault file written once by `_persist_job_vault`, unchanged | T5 exit-path matrix in 7.1: admission failure, run exception, vault write failure, cancel, success; assert no vault file is left after a refusal and the scope is closed (spy on `AdmittedKey.close`) | Remove the `with AdmissionScope()` in `report vault`: T5 close-spy fails; write the vault before admission in `run`: T5 partial-file cell fails |
| 3 | State machine and concurrency | Key identity fixed at job start; engine caches stay off (`SecretCachePolicy` default, `_types.py:308`); no consumer sets a TTL. Master-key swap mid-job cannot change the provider because it is resolved once | T7 barrier test: build the writer, swap the master key (`reset_cache` + new env) before `run_v2_pipeline`, assert the run still uses the first provider or fails closed via `assert_vault_writer_keyed` (`_pipeline_context.py:325-327`), never writes a vault under key A with output under key B; second test: two concurrent jobs with different masters in one process (test only) do not mix providers | Derive the provider per call instead of once per job: T7 produces a vault/output key split and fails |
| 4 | Route parity and silent fallback | AST sentry: every call to a keyed engine entry under `api/` must pass an explicit `key_provider=` keyword and must not use `**` spread (unresolvable calls fail closed, as the CLI import-surface sentry does); the same for `src/decoy/` after 4b. Seven platform sites in 7.2 | T8 route matrix: for each of full_frame, streaming (dispatcher on and off), sequential, out_of_core, preview and unmask, run a keyed fixture and assert the engine received the instance provider (spy on `require_mask_key` input) and no `SeedKeyProvider` | Delete `key_provider=` at `v2_sequential.py:201`: sentry T8a and route cell T8b fail (against 0.16.0 and against engine main) |
| 5 | Referential integrity | One provider per job means parent and child tables share one mask root on every route, including children in a later chunk batch; composite-FK groups are keyed (`_requirements.py:225-228`) so they need the same key | T9 relational test per route per FK shape in 7.3: masked child FK equals masked parent key under the injected provider | Give the child route a second provider instance with a different `key_version`: T9 FK-equality fails |
| 6 | Untrusted producer | Platform: job config is untrusted, `mask_secret_ref` rejected at validation (P3). CLI: YAML/flag value goes through engine `parse_ref` (exact `str` type check, `types.py:101`), never through Pydantic first (keeps the existing raw pre-check `run.py:513-535`) | T10 field walk over `global_settings.mask_secret_ref` with `None, "", " ", 0, [], {}, b"x", "env:X", "file:/x", "vault://m/p?version=1&field=f&encoding=raw", 2 KB string, NUL byte, a str subclass`; platform rejects all but `None`; CLI reports fixed codes and no value | Drop the `is not None` presence check and use truthiness: the `""` cell fails; remove the raw pre-check before `model_validate`: the list/dict cells echo the value and fail |
| 7 | Check then use on a path | CLI today reads the secret twice per run (`vault_writer_for_config` then `run_pipeline`), a window for an `env:`/`file:` value to change between reads. 4b admits once and passes the same provider to both. No permission pre-check is added (Q5), so no new check-then-use | T11: monkeypatch the `file:` source to return key A on first read and key B on second; on 4b assert one read total and a consistent vault; on 4a (old pin) assert the run fails closed with the engine vault-key guard and writes no vault | Re-admit in `run_pipeline` while also admitting for the writer: T11 read-count assertion fails |
| 8 | Weak tests and overclaimed evidence | Red records captured on the unmodified base before any fix; baselines from `scripts/dev/gates.sh --baseline` per repo; golden/fixture regeneration is a reviewed, named step with a diff table (key change is expected there), never a silent re-record; no skips; `no_default_secret` style markers (engine `tests/_keyed_secret.py`) are not copied | T12: each new test is run against base and recorded red in `docs/plans/evidence/` of the owning repo; platform regenerated fixtures listed with old and new hashes | Revert 3a code: the recorded-red tests are red again; widen a golden tolerance: reviewer rule, forbidden |
| 9 | Behavior-preserving refactor drift | CLI helper extraction (4a) is behavior-preserving: characterization tests on current messages and exit codes for `run`, `tokenize` and `decoy.mask()` before the three copies are replaced | T13: golden of every `_MaskSecretUsageError` text and exit code, captured first; plus `test_cli_surface`-style docs regeneration check for the new flags | Change one word in a usage message or an exit code: T13 fails; drop the `api.py` caller: parity test fails |
| 10 | Frozen contract drift | List in section 12; schema and header snapshots unchanged | T14: vault header keys snapshot (slice 0 T9) re-run in both consumers' CI via the pinned engine; CLI `--json` envelope key snapshot for `report vault`, `unmask`; platform `/unmask` response model snapshot | Add a header key or rename a JSON field: T14 fails |

## 6. Enumerations from the code

### 6.1 Exit paths

Everything that owns state: admitted secret buffers, the vault file, the masked outputs.

- CLI `decoy run`: admission failure before any read (`vault_writer_for_config` at `run.py:791` today; `admit_mask_key` after 4b); run exception into the handler at `run.py:~880-1000` (maps `MaskSecretError` to `EXIT_USAGE` via `_mask_secret_error_types`, `run.py:910-912`, `:995`); success then `vault_writer.write(vault)` at `run.py:854`; `typer.Exit` pass-through.
- CLI chunked: `_run_chunked_mask` (`run.py:794`, `:1437`) with the writer shared across tables.
- CLI `report vault` (`report.py:2290-2335`): YAML error, non-mapping, `VaultError`, success. CLI `unmask` (`cli/unmask.py:189-240`): `PlanCompileError`, `ExecutionError`/`ConfigError`, `VaultError`, generic.
- Platform job: `_build_job_vault_writer` returns `None` (no vault columns, `v2_vault.py:35`), a writer, or raises (`v2_orchestrator.py:152`, pre-try, so a secret error here escapes the job try block; 3a moves provider construction and writer build inside the existing failure path or proves the supervisor path records a registered code, see T5); `_persist_job_vault` success, or the `except Exception` branch recording `vault_write_failed` (`v2_vault.py:78-98`).
- Platform `POST /unmask` (`unmask/router.py:113-195`): unknown table 422, vault missing 422, `ExecutionError` 422 (`:151-152`), success. A `MaskSecretError` is not an `ExecutionError` and currently has no branch; 3a adds one.
- Platform streaming abort: `active_writer.abort()` on exception (`v2_runner.py:~414-418`), unchanged.

### 6.2 Routes

Every engine entry the consumers call, with the key argument each needs.

- Platform: full-frame `run_pipeline` (`api/jobs/v2_runner.py:226`; preview reaches it via `preview_child.py:246` -> `run_v2_pipeline`); streaming `decoy_engine.run_mask_chunked` (`v2_runner.py:372`) and `run_mask_pipeline_chunked` (`:385`); sequential `run_sequential` (`v2_sequential.py:201`, mask-only, no admission, relies on `require_mask_key` at `execution/_sequential.py:332`); out-of-core `run_fk_out_of_core` (`v2_out_of_core.py:327`); vault `vault_writer_for_config` (`v2_vault.py:40`); `unmask_pipeline` (`unmask/router.py:150`); A-8 probe `run_pipeline` (`engine_a8_guard.py:117`, already passes a provider).
- CLI: `run_pipeline` (`run.py:821`), `run_mask_pipeline_chunked` (`run.py:1437`), `decoy.mask()` `run_pipeline` (`api.py:504`), `tokenize` `run_pipeline` (`tokenize.py:334`), `unmask_pipeline` (`cli/unmask.py:189`), `load_vault` (`report.py:2305`), `vault_writer_for_config` (`run.py:791`). `subset` uses `job_seed_for_config` (`subset.py:209`, `:222`) for sampling, not masking: unkeyed, unchanged.
- Engine entries and their key kwarg, all present at `v0.16.0` (counts of `key_provider` in `git show 5da40c2:...`: `_pipeline.py` 2, `_chunked.py` 2, `native/_chunked_entry.py` 5, `_sequential.py` 2, `out_of_core/_runner.py` 2, `unmask.py` 6, `vault.py` 8). This is why 3a is green before the bump.
- Generation-only jobs need no secret (`secret_requirements_for_config` skips `generate_columns` tables, `_requirements.py:212`); passing the provider is harmless there (`_admit_boxed` returns without reading when there are no requirements, `_preflight.py:309-311`).

### 6.3 FK shapes

- Single-column FK (parent and child both keyed to the same root).
- Composite FK (always keyed, `_requirements.py:225-228`): the case that breaks if two providers exist.
- Self-referencing FK.
- Cross-table chain (A -> B -> C) across sequential and out-of-core routes (`v2_sequential.py`, `v2_out_of_core.py`).
- Child rows in a later chunk batch on the streaming route (`v2_runner.py:372-385`, `base_row_offset`).
- Orphan-policy paths are not changed by this plan (the provider does not alter classification).

### 6.4 Fields

Every public result or carrier type this change touches.

- `SecretKeyProvider.secret: bytes`, `.key_version: str` (`keyprovider.py:121-131`); repr is redacted (`:142-146`).
- `AdmittedKey.key_provider`, `.prepared`, `.requirements` (`_preflight.py:272-282`); `PreparedKeyProvider` view.
- `JobRunContext` (`api/jobs/job_run_context.py`): gains no secret field; carries the provider object only if the worker threads it there, in which case its `repr` must be redacted (T6 walks the dataclass fields of `JobRunContext`, `Job`, `JobNodeRun`, `UnmaskOut`, `UnmaskColumnEntry`).
- `MaskSecretError.code`, `.message` (`keyprovider.py:57-76`) and the seven F13 error classes (`secret_providers/_errors.py`).
- CLI JSON envelopes: `report vault` (`command, status, vault, entries, namespaces, ambiguous_dropped, error`) and `unmask` (`command, status, config, input, output, table, columns, error`).
- Platform `UnmaskIn`, `UnmaskOut`, `UnmaskColumnEntry` (`unmask/router.py`).

### 6.5 Output surfaces

stdout, stderr, json, files, logs, audit, evidence, errors, progress, help, parse errors, root callbacks.

- CLI stdout/stderr and `--json` error payload (`error_text = str(exc)` capped at 500, `run.py:1005-1007`); `report vault` and `unmask` `_emit_error` (`report.py:2255-2268`, `cli/unmask.py:123-140`); `--help` text of the new flags (must not show an example containing a plausible secret); Typer parse errors for `--mask-secret`.
- CLI notifications: only `type(exc).__name__` goes out (`run.py:1019-1030`); the engine exact types (`SecretAccessDenied` etc.) are fixed names, accepted.
- CLI evidence manifest (`cli/evidence.py`, built from `_ev_config_dict`, `run.py:671`, `:1097-1102`): the config dict contains `mask_secret_ref`; T6 asserts the marker ref/path is absent from the manifest (hashes only are documented, not verified here).
- Platform: `Job.error`/`JobNodeRun.error` JSON (`v2_vault.py:98`), `JobNodeRun.exports`, evidence manifest, `write_audit` meta for `unmask.reveal` (`unmask/router.py:~163-185`, schema-level facts only today), server logs (`_log.warning(..., exc_info=True)` at `v2_vault.py:79`, whose traceback must not carry key locals: engine boundary already strips frames, T6 renders with `capture_locals=True`), HTTP error bodies, the key-status admin endpoints (`get_master_key_fingerprint` is a one-way hash, unchanged), OpenAPI/MCP descriptions.
- Platform job-config validation error for `mask_secret_ref_not_allowed`: fixed text, no echo of the value or its type name beyond "present".

## 7. Rotation, stated honestly

- A key change is: a different master key (platform), a different secret behind the same ref, a different ref, or a different `key_version`. Effect: every keyed output (hash, fpe, date_shift, shuffle, tokenize, deterministic, composite-FK) gets a new identity, so joins against earlier output break. That is the engine's stated re-key semantics (`secret-providers.md`: "change it to rotate output identity intentionally").
- Existing vaults are not re-encrypted, not migrated, not invalidated and not detected. A vault opens only under the exact key that wrote it; any other key fails `vault_key_mismatch` (Fernet authentication). The vault header carries no key identifier (`vault.py:284-290`: `format, seed_protocol_version, ambiguous_dropped, chunk_rows, chunk_count`), so Decoy cannot say which key a vault wants. No command re-keys a vault; adding a key-id field is a `decoy-vault/v2` change and is not proposed here (Q6).
- Platform: replacing or restoring the master key (`key_restore.py`) makes every existing job vault unreadable; `POST /unmask` returns 422 `vault_key_mismatch`. The platform records the master-key fingerprint at acknowledgement time (`models.py:2207`, `get_master_key_fingerprint`), not per job vault (not verified further), so it cannot tell an operator which fingerprint a given vault needs. Operators must keep the old master key as long as old vaults must be unmaskable. The upgrade-determinism gate (`api/golden`, `tests/test_golden_determinism_gate.py`) is expected to report CHANGED for every keyed pipeline after 3b; that is the correct signal and must be documented, not suppressed.
- CLI: keep the old ref (`file:` path, env value, or Vault `version=N`) for as long as old vaults matter. For Vault KV the old version stays readable until destroyed; "incident invalidation" (destroy the version) deliberately makes the vault unmaskable.
- Seed-keyed vaults from older engines: no migration and no recovery path (owner ruling 2026-10-31, applied; slice 0 plan Q1 closed the same way). After the pin bump the new engine refuses them with `weak_mask_secret` (slice 0).
- The platform and CLI docs gain this section verbatim in substance (4a docs, 3a docs).

## 8. Ordering across repos, and what breaks at each step

Current state: platform on 0.16.0, CLI on 0.15.1; both pre-F13 and green.

| Step | Repo | Change | State after (is main green, what breaks) |
|---|---|---|---|
| E0 | engine | docs and stale-docstring fix; no behavior | Green. |
| E-rel | engine | Release carrying F13 (#339) and slice 0 vault floor. Which of the two owner-approved releases (R-a, R-b) carries them is not stated in the files I could read (Q1). | Green: consumers are pinned; nothing moves. |
| 3a | platform | Provider injection, ref rejection, route sentry, tests. Still pinned to 0.16.0. | Green on 0.16.0 because all entries already take `key_provider` there (6.2). Behavior change: keyed masking output now keys on the derived secret instead of the 8-byte seed; fixtures that pinned old output are regenerated in a reviewed step (class 8). This is the R3 activation for the platform on a live instance. |
| 3b | platform | Pin bump: `pyproject.toml:36`, `.github/workflows/backend.yml` `ENGINE_PIN_SHA` and `ENGINE_PIN_VERSION`, `api/engine_compat.py:171` (`ENGINE_TRANSFORM_OWNERSHIP_SPECIFIER`); re-run the 3a suite. `EXPECTED_SEED_PROTOCOL_VERSION` stays 7 (engine `determinism/_derive.py:136` is 7). | Green only if 3a merged first. Without 3a, every keyed job and `/unmask` fails closed (6.1). |
| 4a | CLI | Shared `decoy/_mask_secret.py` helper replacing the three predicate copies and the two injection copies; `--mask-secret` on `unmask` and `report vault`; inspector keyed through the ref via `key_provider_from_ref`; docs and `decoy explain keys` recipe; fix the stale `unmask` docstring. Pinned to 0.15.1. | Green on 0.15.1. Fixes the existing inspector mismatch (section 1). |
| 4b | CLI | Pin bump to the E-rel tag (`pyproject.toml:99`, `ci.yml:120,168,277`, `release-smoke.yml:80`, `regen-diagrams.yml:56`); `parse_ref`-based validator; single `AdmissionScope` per command; add the seven F13 codes to `src/decoy/ci/rule_codes.py` (its sentry `tests/unit/test_ci_rule_codes.py` re-derives codes from the installed engine and fails when upstream adds one, `rule_codes.py:10-12`); extend `tests/sentry/test_engine_import_surface.py` coverage for `admit_mask_key`, `AdmissionScope`, `parse_ref`. | Without the `rule_codes.py` update the CLI suite goes red at the bump (expected failure, not run). Without 4a the inspector cannot open any vault under the new floor. R3 activation for the CLI. |

Rule: no pin bump merges before its consumer slice; the bump PR is mechanical (pins, one allowlist, re-run). A consumer never depends on an unreleased engine: CI for 3a/4a installs the existing pins.

## 9. Reuse inventory

- Engine: `SecretKeyProvider` (`keyprovider.py:121`), `admit_mask_key`, `AdmissionScope`, `AdmittedKey` (`_preflight.py:272-338`), `parse_ref` (`_types.py:93`), `key_provider_from_ref` (`keyprovider.py:178`), `MIN_SECRET_BYTES`, registered reasons in `reasons.runtime`, `MaskSecretError` and subclasses, `tests/_keyed_secret.py` pattern (imported in engine; consumers write their own marker fixtures, no copy of a fixed key into `api/` or `src/`).
- Platform: `api/keys/derive.py:_hkdf` and the `derive_mask_job_seed_int` pattern, `api/keys/master.py:load_master_key` / `reset_cache`, `api/engine_compat.py` pin gate, `tests/test_engine_internal_import_boundary.py` (model for the AST sentry), `tests/test_engine_a8_guard.py`, `api/audit` `write_audit`.
- CLI: `run.py:605-665` logic (extract, do not rewrite), `tests/sentry/test_engine_import_surface.py`, `tests/sentry/test_yaml_error_leak_isolation.py` and `test_report_raw_value_isolation.py` (marker corpus style), `rule_codes.py` allowlist, `exit_codes.py`.
- New machinery and why: `api/jobs/engine_key.py` (one place to build the provider, so the sentry has one thing to allow), `decoy/_mask_secret.py` (replaces three copies), one platform AST sentry. Nothing else.

## 10. Acceptance tests, red first (hostile corpus)

All new tests are written first and recorded red on the unmodified base (T12). Where a test needs the new engine it is marked and runs in the 3b/4b CI, otherwise it runs on the existing pin.

- T1 `tests/test_engine_mask_key.py::test_derived_secret_is_domain_separated`: secret is 32 bytes, differs from `derive_mask_job_seed_int` prefix and from `derive_pipeline_key` for a label corpus; two different masters give different secrets; same master is stable. Mutation: reuse the job-seed `info`.
- T2 `::test_instance_key_provider_never_seed_key`: result is a `SecretKeyProvider`, not `SeedKeyProvider`; `len(mask_key()) == 32`; `repr` carries no bytes (marker master).
- T3 `::test_job_config_rejects_mask_secret_ref`: the T10 cell table at the validation choke point (`api/jobs/v2_config.py`, which already wraps `PipelineConfigError`, `:37`, `:98`); `None` accepted.
- T4 `tests/test_vault_keying.py`: vault written by a platform job opens with `load_vault(path, instance_key_provider().mask_key())` and not with the job seed or a different master; `assert_vault_writer_keyed` passes; the unmask router opens it end to end (extends `tests/test_unmask_vault.py`, `tests/test_v2_unmask.py`).
- T5 exit-path matrix (6.1 cells), both repos.
- T6 seeded-marker corpus across 6.5, both repos; each cell asserts the secret path executed (non-empty) and absence of the marker, the derived hex, the base64 and the ref text. Includes `traceback.TracebackException(..., capture_locals=True)` of every failure.
- T7 barrier interleavings (class 3).
- T8a AST route sentry (platform; CLI after 4b); fixtures include a `**kwargs` spread, an aliased import (`from decoy_engine import run_pipeline as rp`) and a string-dispatched call, which must each fail the sentry (seeded evasions). T8b route matrix (class 4).
- T9 FK relational test per route and shape (6.3).
- T10 field walk over `mask_secret_ref` (class 6), in both repos.
- T11 single-admission/two-read test (class 7).
- T12 red-baseline files: platform `docs/plans/evidence/2026-10-31-f13-slice3-red-baseline.txt`, CLI `docs/records/...` equivalent (path per repo convention, the worker picks the existing `docs/records` or `docs/plans/evidence` location).
- T13 CLI characterization golden (class 9) and `decoy report vault` / `unmask` flag parity tests: `--mask-secret` and YAML ref produce the same key; both set is `_MaskSecretUsageError`-style usage error, exit `EXIT_USAGE`.
- T14 frozen-contract snapshots (section 12).
- T15 CLI end to end (4a): `decoy run --vault v.bin --mask-secret file:K` then `decoy report vault` with the same flag opens it; with the wrong key prints `vault_key_mismatch`; with none prints `keyed_strategy_requires_secret` (after 4b) or the missing-secret code (4a on 0.15.1), exit `EXIT_USAGE`.
- T16 CI-code allowlist (4b): `tests/unit/test_ci_rule_codes.py` green with the seven new codes added, and the test still derives codes from source (not weakened).

Ten ways a gate reviewer will break this, each mapped to a red-first test: (1) a platform route without a provider (T8a/T8b); (2) a job author sets `mask_secret_ref: file:/etc/passwd` (T3/T10); (3) the public job seed equals a prefix of the secret (T1); (4) master key swapped mid-job (T7); (5) a failure message echoes a ref or key (T6); (6) `/unmask` returns 500 on `MaskSecretError` (T5, 6.1); (7) child FK masked under another key (T9); (8) `report vault` opens with the seed or a different flag than `run` (T13/T15); (9) CLI reads the secret twice and the file changes (T11); (10) the pin bump merges without its consumer slice or without the `rule_codes.py` update (T16, and section 8 ordering enforced by 3b/4b CI).

## 11. Self-attack (mechanism, counter-case, covered?)

- AST sentry (class 4). Counter: the call is wrapped (`functools.partial`, a helper that forwards `**kw`). Covered: the sentry fails closed on any call to the entry names it cannot resolve statically, and `engine_key.py` is the only allowed constructor; seeded evasions in T8a. Residual: a dynamic `getattr(decoy_engine, name)` call; reject `getattr` on `decoy_engine` in the sentry; otherwise a V2 note.
- Once-per-job provider (class 3). Counter: the orchestrator's iteration children and sequential re-admission attempts (`v2_orchestrator.py` comment: "each iteration child and each sequential re-admission attempt reaches this line") each resolve their own provider; they derive from the same master so the bytes match unless the master changes between them. Covered by T7 second cell; if the master changed between attempts, the vault guard fails closed. The plan does not claim cross-attempt immutability.
- HKDF label (P1/class 1). Counter: someone later reuses `v2-mask-secret:instance` for something else. Covered by T1 label corpus including every `info=` literal found in `api/keys/` (the test greps the module, so a new label colliding with it fails).
- `mask_secret_ref` rejection. Counter: the key arrives nested, upper-cased, or in a YAML anchor; engine model has `extra="forbid"` (`_global_settings.py:~117`), but the platform validates before. Covered: T3 walks the real `global_settings` dict after YAML parse and after `model_dump`, plus the chunked/sequential path that re-deep-copies the config (`v2_sequential.py:179`). Residual: the platform's other config ingress (Pipeline create, API import); the worker enumerates ingress points from `api/pipelines/` and adds each to T3 (not enumerated here: see section 15).
- CLI helper dedup (class 9). Counter: `decoy.mask()` is documented as a "port" of `run.py` (`api.py:116`); a future edit changes one copy. Covered: one helper imported by all four callers and a sentry that no other module defines `_is_valid_mask_secret_ref` or writes `["mask_secret_ref"] =`.
- Docs-only dev recipe (3.2). Counter: a user copies `openssl rand -hex 32` into a shell with a weak `$RANDOM` substitute. Not closable by code; documented.
- Reviewer will say "R3 is overclaimed for a pre-release repo". Answer in the Risk line; Q2 gives Cam the down-tier decision.

## 12. Frozen contracts touched

- `secret-provider/v1`: consumed, not changed. `decoy-reasons/v1`: no engine code added; one platform-local code `mask_secret_ref_not_allowed` (Q7 whether it must be registered in the engine registry; `OWNER-DECISIONS` F36 says the engine owns one registry for reason codes).
- `decoy-vault/v2` file format and `vault-key/v1` label: unchanged (T14). Vault key bytes change for every new vault (the key source, not the format).
- Platform: `POST /unmask` gains 422 codes `keyed_strategy_requires_secret` and the F13 provider codes (additive); `SEED_PROTOCOL_VERSION` expectation unchanged (7).
- CLI: new flags `--mask-secret` on `unmask` and `report vault` (additive; `docs/cli-reference.md` is committed and checked by `test_cli_surface`, regenerate in the same PR); exit codes unchanged; `--json` envelopes unchanged in keys; `decoy ci` rule-code allowlist grows by seven.
- Public API: no engine API change in this plan. `decoy.mask(mask_secret=...)` unchanged.
- Dependency pins: platform `decoy-engine==0.16.0` and CLI `==0.15.1` change at 3b/4b only.

## 13. Slices

| Slice | Repo | Risk | Size (estimate) | Content | Gate |
|---|---|---|---|---|---|
| E0 | decoy-engine | R2 | docs, about 40 lines | Fix `config/_global_settings.py:84-95` text; add a "Consumers" section to `docs/security/secret-providers.md` (rotation honesty from section 7, dev recipe, Vault Agent file pattern); no code | Codex |
| 3a | decoy-platform | R3 | about 160 LOC code, about 500 LOC tests, fixtures regenerated | `derive_engine_mask_secret`, `engine_key.py`, thread provider through the seven sites in 6.2, `/unmask` `MaskSecretError` branch, `mask_secret_ref` rejection, AST sentry, docs; pinned 0.16.0 | Codex then Dennis (not plan author) |
| 3b | decoy-platform | R3 | about 10 LOC plus re-run | The three pin coordinates, upgrade-gate expectation note | Codex, Cam authorization before deploy |
| 4a | decoy | R2 | about 120 LOC added, about 80 removed, about 300 LOC tests | Shared helper, flags on `unmask`/`report vault`, inspector via `key_provider_from_ref`, `explain keys` recipe, stale docstring, docs regen | Codex |
| 4b | decoy | R3 | about 60 LOC plus allowlist | Pin bump (5 workflow lines plus `pyproject.toml`), `parse_ref` validator, single `AdmissionScope`, `rule_codes.py`, import-surface sentry | Codex then Dennis, Cam authorization before release |
| 4c (optional) | decoy | R2 | about 80 LOC | `decoy keys init --out PATH` (O_EXCL, 0600, CSPRNG, prints only the ref) | Only if Q4 says yes |

3a and 4a are independent and can run in parallel; 3b needs E-rel and 3a; 4b needs E-rel and 4a. Expected rounds: at most two per slice. The known round-three risks: a missed platform call site (the sentry exposes it in round one), a stale golden fixture that the worker is tempted to loosen, and a CLI test that fakes `run_pipeline` without `key_provider` (the fake must accept it; the "older engine" fakes in `tests/` forward kwargs by name, per the import-surface sentry docstring).

## 14. Pre-mortem

- Reviewer: "the platform job fails at `v2_orchestrator.py:152` outside the try block and the supervisor records an unclassified error." Test T5 pins that provider or writer construction failure lands as a failed job with a registered code; if the existing structure cannot, the worker moves construction into the try.
- A platform route is added later (a new engine entry) without a provider: the AST sentry's name list must be derived from the engine's `__all__` entries that take `key_provider` (computed by `inspect.signature`, like the CLI import-surface sentry), not hand-listed.
- 3b merges before 3a: pin CI red on every keyed test immediately; documented, and the 3b PR description must link the 3a commit.
- The engine release is delayed: nothing blocks 3a/4a; they stay green on the old pins. Cost: the inspector fix ships before the floor exists, harmless.
- A deployed instance upgrades and finds old vaults unreadable: expected (section 7); the upgrade-determinism gate shows CHANGED. This is the R3 reason and the owner authorization point.
- `vault://` in a CLI config surprises a user with `secret_provider_unknown`: documented; opt-in provider slice is Q3.
- `file:` secret made world-readable by the user: not prevented (Q5).
- The A-8 probe key (`bytes(range(32))`) is copied into production code by a future change: the sentry allowlists that one file only, with a comment.
- Hostile `KeyProvider`, `BaseException(secret)` from a provider: V2 notes, not blockers (`OWNER-DECISIONS`).
- The engine release changes the `hvac` extra or registry defaults: CLI does not depend on either beyond `env:`/`file:`.

## 15. What I could not verify, and what I did not run

- Nothing was executed: no tests, no baselines, no `scripts/dev/bootstrap.sh`, no probe. No venv was built from `uv.lock`. Every behavioral claim is from reading code at the SHAs above, including the two "already broken" claims (CLI inspector on 0.15.1) and the "platform fails closed after a naive bump" claim.
- I could not tell which engine release (R-a or R-b) carries F13 and slice 0; `OWNER-DECISIONS` records only "two engine releases adopted". I verified only that `v0.16.0` and `v0.15.1` do not contain F13.
- The platform's other config ingress points (pipeline create/import, API job submit, MCP) were not enumerated; T3 requires the worker to list them from `api/pipelines/` and `api/jobs/`.
- Platform `api/jobs/v2_config.py` as the validation choke point is inferred from imports at `:37`, `:98`, `:234`, `:292`, `:332`; not read in full.
- Whether a platform reason-code registry exists (and whether `mask_secret_ref_not_allowed` must be registered) was not determined (Q7).
- Per-job master-key fingerprint tracking in the platform: only `models.py:2207` (acknowledgement-time) was seen.
- The established-solution citations (Vault Agent templates and sinks, Docker/Kubernetes secrets guidance, 12-factor, RFC 5869, OpenSSH/GnuPG permission behavior) are from my knowledge; I did not re-fetch them this session.
- Test-site counts, fixture counts (87 files under platform `tests/` mention `derive_mask_job_seed_int` or a literal `"seed":`, a loose grep) and LOC sizes are estimates.
- Live Vault behavior remains unverified exactly as the engine docs state (`secret-providers.md`, "Not live-verified"); this plan does not depend on it.

## 16. Open owner questions

- Q1 (Cam): which engine release (R-a or R-b) carries F13 and the slice 0 floor, and may 3b/4b target it? Recommendation: one release carrying both, so consumers bump once.
- Q2 (Cam): is any instance outside disposable dev holding masked data or vaults? If none, re-tier 3a/3b/4b from R3 to R2 and skip the per-activation authorization. Recommendation: keep R3 until the owner states it.
- Q3 (Cam): opt-in native `vault://` in the CLI (register `VaultKV2Provider(client_from_env())` behind a flag) now, or later on demand? Recommendation: later; the Vault Agent file pattern covers it with no code and keeps authentication out of Decoy.
- Q4 (Cam): ship `decoy keys init` (slice 4c)? Recommendation: no, docs-only recipe.
- Q5 (Cam): should the CLI refuse or warn on a group/world-readable `file:` secret, as OpenSSH does? Recommendation: warn-only in a later slice; refusing changes behavior for k8s-mounted secrets (0644 by default on some setups) and the check adds a check-then-use window.
- Q6 (Cam): add a non-secret key identifier to the vault header so a mismatch can say which key is needed? That is a `decoy-vault/v2` change; Recommendation: V2, not now.
- Q7 (Cam): must the platform code `mask_secret_ref_not_allowed` be registered in the engine `decoy-reasons/v1` registry? Recommendation: yes if any platform surface already validates codes against it; otherwise keep it platform-local and note it in section 12.
- Q8 (Cam): platform `key_version` stays the constant `"v1"` in code. Should it become a deployment setting so an operator can rotate output identity without changing the master key? Recommendation: no for V1; the master key is the rotation lever.

## 17. Worker evidence rules

Name the class of every finding and fix the class (the structure in section 5), not only the listed instance. Write the generated tests first (marker corpus, exit-path matrix, interleavings, field walk, route matrix) and record them red on the unmodified base. Before pushing, attack your own diff: mutate each new mechanism once (section 5 mutation column) and show the test goes red. Run baselines with `uv.lock` in the same environment as the branch (`scripts/dev/bootstrap.sh`, `scripts/dev/gates.sh --baseline`, per repo) and state what you did not run. Never widen a threshold, skip a test, loosen a golden tolerance or weaken an assertion to make a fixture regeneration pass; regenerated fixtures are listed with old and new values for the reviewer. If a defect of a class already in the catalog appears a second time, stop and report it rather than patch again.
