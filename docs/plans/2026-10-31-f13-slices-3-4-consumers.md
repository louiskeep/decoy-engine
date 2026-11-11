Status: handoff, plan only (revision 5.1, V1 scope; revision 5.1 narrows the `vault://` guarantee after Codex r5 on `17ff7fbb`; owner 2026-11-11: narrow the guarantee to what V1 needs, section 0). Revision 5 keeps revision 4 (`18847ac0`) whole and marks what V1 still builds: one closeout slice, 5-v1 (section 0). The CLI slices 4a and 4b are MERGED (CLI #78, `b4f19ba`); the platform slices 3a-i, 3a-ii, 3a-iii and the repin are Platform V1, parked until CLI V1 ships (CLI `docs/ROADMAP.md:40`). No implementation, merge, release or deployment approval is granted by this document.

Summary: the platform keys masking from its instance master key (HKDF with its own label, wrapped in the engine's `SecretKeyProvider`) and hands one provider per attempt to every engine entry it calls; previews (bounded and nested) get one random per-request key that never touches the master. Job configs may not name a secret reference. Master-key and KMS failures become fixed-code, value-free errors raised from frames that hold no key material. In the CLI, one shared helper owns ref shape, flag injection and a single admission per command, and `--mask-secret` is added to `decoy unmask` and `decoy report vault`. Neither consumer falls back to the job seed. The platform work lands as three bounded slices stacked on the engine 0.17.0 repin branch, which merges only after them.

Baselines (revision 4, all fetched 2026-11-01; every coordinate in this plan was re-derived at these refs, and a coordinate carries its ref when the file differs between them):

- Engine: `origin/main` `51548303` (merged into this plan branch before editing). Tag `v0.17.0` = `1132d29e`; for every engine file this plan cites under `secret_providers/`, `keyprovider.py`, `vault.py`, `unmask.py`, `execution/_pipeline.py`, `execution/_pipeline_context.py`, `execution/_chunked*.py`, `execution/_sequential.py`, `execution/native/_chunked_entry.py`, `execution/out_of_core/_runner.py`, `execution/out_of_core/_preflight.py`, `execution/_strategies/` and `config/_global_settings.py` there is no diff between `1132d29e` and `51548303` (`git diff --name-only 1132d29e 51548303 -- src` lists six FK files: `execution/_fk_keys.py`, `execution/_pipeline_routing_fk_dtype.py`, and `_batch_join.py`, `_emit.py`, `_join.py`, `_stream_join.py` under `execution/out_of_core/`, none cited here), so "engine" citations hold on both. A-4: branch `claude/jolly-carson-3k5stf` head `16d72eca` (contains `d8f19393`, the merge of main `51548303`), cited as "A-4".
- Platform: `origin/main` `8a581fe` (#136 since `7fc4612d`; touches only `api/auth/*`, `pyproject.toml`, the lock and one test, no cited file). Repin branch `chore/repin-engine-0.17.0` `6e9967e6` (merge-base with main `7fc4612d`, 8 commits, pins `decoy-engine==0.17.0` at `pyproject.toml:40` and `ENGINE_PIN_SHA` `1132d29e` at `.github/workflows/backend.yml:75`). Platform citations are at `6e9967e6`, the base of every 3a slice.
- CLI (`louiskeep/decoy`): `main` `a5d36b4`, read from a public clone because the CLI repo is not attached to this session; no CLI code was run (section 15). It already pins `decoy-engine==0.17.0` (`pyproject.toml:103`) and contains #76 (`57e8b81`).
- Evidence for this revision: `docs/plans/evidence/2026-11-01-f13-rev4/` (environment, measured sizes, probes, base-red lists, prototype patches). Platform probes ran in a venv built by the platform's own `scripts/dev/bootstrap.sh` from `requirements-dev.lock` at the repin head (Python 3.10.20, engine editable at `1132d29e`).

Rules consulted: dev-rules `00-universal`, `risk-and-exceptions` (R2/R3), `config-secrets-and-supply-chain`, `security`, `api-and-compatibility`, `testing`; `DEFECT-CATALOG.md`, `OWNER-DECISIONS-2026-10-30.md`, `HOSTILE-PLAN-REVIEW.md` (decoy-platform `handoffs/queue`, not re-read this revision); engine, platform and CLI `CLAUDE.md`; engine `docs/security/secret-providers.md`.

Risk: R2 for every slice (security logic, compatibility; adversarial review and exact-artifact gate). Basis (owner, applied): pre-release, no backward compatibility, no real vault or persisted output, so the change of keying is not a credential rotation of value. Re-escalation: if real persisted outputs or vaults, or a live credential rotation, are found on any instance that would take these slices, the activation (the repin merge) rises to R3 and section 8a is the recovery text. Merging remains owner-gated. Gate per slice: Codex on the exact artifact; the plan author does not review the build.

Threat model: V1 (self-hosted single-org, trusted engine process). In scope: accidental leak of a secret, derived key, reference or secret-bearing path into a log, error, report, manifest or response (traceback locals are admin-only output: V2-LOG, section 0, Codex g4 HIGH 6 row); a job config author choosing a host secret source; key mix-up between tables or steps of one job. Out of scope (V2 note): hostile in-process objects, hostile `KeyProvider`, `BaseException(secret)` from a provider.

# F13 slices 3-4: platform provider injection and CLI secret resolution, PLAN

## 0. V1 scope (revision 5.1)

Owner 2026-11-11 (dev-loop review decision 2): plans stuck in review are narrowed to a V1 scope that passes in one or two rounds; cut cases go to the V2 backlog. V1 bar: only a product-function break, data loss or corruption, or a value shown to a non-admin blocks V1. Roadmap row 73 ("F13 slices 3b and 5", Phase 1 engine and CLI) is the row this plan serves. Slice 3b no longer exists (revision 4 section 13: the pin bump is the platform repin branch), and slice 5 is the cross-repository closeout of the F13 contract plan (`docs/plans/2026-10-29-f13-secret-provider-contract-preflight.md` at `039c5cef`, section 6 "Slice 5"). Baselines for this section: engine `origin/main` `6e2e57bd` (version 0.22.0), CLI `origin/main` `f77de8d` (pins `decoy-engine==0.22.0`, `pyproject.toml:122`).

**V1 guarantee.** In CLI V1 every keyed command (`decoy run` with or without `--vault`, `decoy tokenize`, `decoy unmask`, `decoy report vault`, `decoy.mask()`) takes its masking secret from exactly one of `--mask-secret REF` or `global_settings.mask_secret_ref`, and the engine admits it once per command as an `env:` or owner-only `file:` reference of at least 32 bytes. A keyed plan with no secret, a weak secret, a group- or world-readable secret file, both places set, a malformed reference or a `vault://` reference (the CLI registers no Vault provider in V1) is refused with a registered code and constant text, and no secret, reference value or secret path reaches stdout, stderr, `--json`, evidence or notifications. For a `vault://` reference the V1 guarantee is exactly this: the constant registered refusal `secret_provider_unknown`, no echo of the reference, and no output or vault written or changed. V1 does not promise that admission comes before source reads: usage and source refusals may come first, and CLI `tests/e2e/test_mask_secret_single_read.py:121-145` (`test_usage_and_source_refusals_come_before_the_secret_read`, at `f77de8d`) pins that order on purpose and stays as it is. Nothing falls back to the job seed. All of this is merged: engine slices 0 to 2 (#339) and the owner-only `file:` check (A-4, #361), CLI 4a and 4b (#78, including Dennis's 4a-0 refusal of `--chunked --vault`, CLI `src/decoy/cli/run/_prepare.py:391`). What V1 still builds is slice 5-v1 below.

**Slice 5-v1 (engine docs plus CLI closeout; R1).**
- Engine: a "Consumers" section in `docs/security/secret-providers.md` (none at `6e2e57bd`; headings stop at "Structural sentry"): the CLI surfaces and their one-place rule, `file:` first (Vault Agent or a Kubernetes secret volume rendered to an owner-only file), `env:` for CI, `vault://` refused by the CLI with `secret_provider_unknown` until row 83, and the platform consumer parked. This is the docs half of E0; its docstring half is already on main (`config/_global_settings.py:84-95`, "no `job_seed` fallback").
- CLI: one test, `tests/sentry/test_mask_secret_ref_shape.py::test_vault_ref_is_refused_without_echo[<surface>]` over `run`, `tokenize`, `unmask`, `report vault` and `decoy.mask()`. Every source and input the surface reads is present and valid, so the only refusal left is the secret's. A `vault://` reference carrying a canary path exits with `secret_provider_unknown` and the constant text, the canary is in no output (stdout, stderr, `--json`, evidence), and no output target, vault or evidence file is created or changed (each path absent before and after, or byte-identical where the test pre-creates it). The test asserts nothing about source-read order. CHAR (expected green on `f77de8d`: `_mask_secret.py:33-35` passes the scheme to the engine, which refuses it at admission, after the sources load and before any write), so it is recorded green, not red; it pins the V1 claim that no CLI test covers today (no test under `tests/` names `vault://`). 5-v1 adds no logic. If a surface goes red because it writes before admission, that is a V1 defect outside 5-v1: the builder stops and reports it rather than fixing it here.
- Closeout record: the six CLI F13 test modules (`tests/e2e/test_mask_secret_flag_parity.py`, `test_mask_secret_single_read.py`, `test_run_mask_secret.py`; `tests/sentry/test_mask_secret_error_text.py`, `test_mask_secret_file_mode.py`, `test_mask_secret_ref_shape.py`) plus the new test, run on the exact CLI head at its pinned engine, on the Mac or a VM, with the command, both SHAs and the result in the CLI's `docs/records`. Size: engine docs under 60 lines; CLI one test module edit under 100 lines.

Every finding from the last rounds: Codex gate 4 on revision 3 (`774ab826`, NO-GO) and Dennis round 1 on revision 4 (`18847ac0`, GO-WITH-FIXES, CLI 4a/4b scope). The 4a/4b code-gate rounds closed with the merge of #78.

| Finding | Where | Revision 5 |
|---|---|---|
| Codex g4 BLOCKER 1: CLI baselines obsolete | CLI 4a/4b | Closed: rebased in revision 4, built and merged as #78 |
| Codex g4 BLOCKER 2: slice 3a over both bounds | platform 3a | Platform V1 (parked); revision 4's measured split into 3a-i, 3a-ii, 3a-iii stands |
| Codex g4 BLOCKER 3: acceptance inventory | 10.1 to 10.5 | CLI cells closed by #78; platform cells (10.1 to 10.3) parked with their slices |
| Codex g4 HIGH 4: A-4 is not a 3a dependency | 3.7 | Closed in revision 4; A-4 has since merged (engine #361), so 3.7's B-5 list applies whenever the platform repins |
| Codex g4 HIGH 5: keyed nested preview returns 500 | platform 3a-iii | Platform V1 (parked) |
| Codex g4 HIGH 6: master-key traceback locals | platform 3a-i | Platform V1 (parked). Already in the V2 backlog as admin-only (backlog "F13 3a" T-i.3 row); the message-text half (T-i.4) stays with 3a-i |
| Codex g4 MEDIUM 7: citations | throughout | Revision 4 re-derived them; platform citations are re-derived again when the platform is unparked (platform main has moved) |
| Dennis r1 HIGH: refuse `--chunked --vault` (slice 4a-0) | CLI | Closed: built (`_ChunkedVaultError`, CLI `run/_prepare.py:391`) and merged in #78 |
| Dennis r1 class 8, 9, 10 items (T-4b.1 label, no CHAR for `run --vault` without a secret, Slice 0 citation, 0600 fixtures, tokenize remedy text) | CLI 4a/4b | Already V2-LOG (backlog, "F13 CLI 4a/4b", `18847ac0`): test labels and wording, no function, data or value effect |
| Live Vault tests not run (engine slice 2; `secret-providers.md` "The five live tests ... have not been run") | engine | Moved to row 83 (external adapters, Phase 2): no V1 surface reaches the Vault provider, since the CLI registers none |
| Slice 5 three-way engine, CLI and platform parity on one reference; mutation and full-suite closeout | engine, CLI, platform | Platform half parked; the CLI half is the 5-v1 closeout record. Mutation follows the owner's 2026-11-10 rule (core logic only); 5-v1 adds no logic |
| F-6 (section 16): nested preview returns 500 for an uncoded engine exception | platform, engine | Parked with Platform V1 (owner: Platform V1 waits until CLI V1 ships, CLI `docs/ROADMAP.md` section 6). The owner choice it carries (code the engine error, or a generic 422 on the route) is made when the platform is unparked |
| Codex r5 V1 (on `17ff7fbb`): the guarantee and the 5-v1 test promised admission before any source read, but `run`, `unmask`, `tokenize` and `decoy.mask()` load sources first and the ordering test pins that | section 0 | Revision 5.1 narrows the guarantee to the refusal, no echo and no write; the order is unchanged and the ordering test stays |
| Codex r5 V2-LOG: `tokenize` missing from the `vault://` matrix; traceback locals still in scope in the threat model and P4; F-6 missing from this table | section 0, threat model, 4 (P4) | Fixed in revision 5.1: `tokenize` added; threat model and P4 point at the HIGH 6 row; F-6 row added |

Also out of this row: the vault key floor (slice 0) is roadmap row 11 with its own plan; its "after 3a and 4a" precondition (section 8) is met for the CLI, and the platform takes the floor with 3a when it is unparked. Nothing new goes to the V2 backlog.

## 0a. Revision 4 remediation map (Codex NO-GO on `774ab826`)

| # | Finding (class) | Disposition | Where |
|---|---|---|---|
| 1 | BLOCKER, CLI baselines obsolete (10 frozen-contract drift, 8 overclaimed evidence) | Accepted. CLI design rebased on `a5d36b4` (already on 0.17.0 with `AdmissionScope` in `report vault`). Old 4a/4b, T15a/T15b, T16 and the CLI half of T17 are deleted; what remains is re-split into 4a (helper, flags, single admission) and 4b (error text), both measured | 1.3, 3.3, 8, 10.4-10.5, 13 |
| 2 | BLOCKER, slice 3a over both bounds (8 overclaimed sizing) | Accepted. Split into 3a-i, 3a-ii, 3a-iii; each MEASURED from a prototype diff (production files and lines) with a hard test-line cap; every slice within 10 production files and 800 lines | 13, evidence README |
| 3 | BLOCKER, acceptance inventory (8 weak tests) | Accepted. Every test has a `file::test` id, its class, and either a fail-before location on the named base (red-first) or an explicit label (green characterization, continuity, sentry). T9 rebuilt to assert key identity, so it is red on the base and red under a shared-but-wrong key | 10 |
| 4 | HIGH, A-4 is not a 3a dependency (10 frozen contract) | Accepted, verified: every engine surface 3a uses exists at `v0.17.0`, and A-4 only adds optional `file_access=` keywords. Dependency removed; the A-4 adaptations are listed for B-5 | 3.7, 8 |
| 5 | HIGH, keyed nested preview (4 route parity, P5) | Accepted, owner decision applied (ephemeral per-request key). Re-verified by running it: on `v0.17.0` hash, FPE and tokenize fail on their namespace or vault check (500), and `text_mask` raises `AttributeError` (500); the fix also sets a constant namespace and maps coded child errors to 422 | 1.1, 3.6, 10.3 |
| 6 | HIGH, traceback locals (1 value-bearing output) | Accepted, verified red by running it. Master and KMS loaders report failures by return value; the raise happens in a frame holding no key; a platform-local boundary (`run_boxed`) also covers HKDF and the engine constructor, whose own frame holds the secret argument. Engine `_opaque` is not at `v0.17.0` (A-4 only) and is private, so it is not reused | 3.4, 10.1 |
| 7 | MEDIUM, citations (8) | Accepted. All coordinates re-derived at the Baselines refs; `api/main.py` primes keys at `:1214-1225` (`load_master_key()` at `:1219`, `.exception` at `:1224`) | throughout |

Repin worker inconsistencies, taken as input (owner): see section 16, findings F-1 and F-2.

## 1. Problem, as verified at the Baselines refs

### 1.1 Platform (repin head `6e9967e6`)

- Every keyed job fails closed. The platform passes no `key_provider` anywhere (grep of `api/` for `key_provider`: only the A-8 probe, `api/engine_a8_guard.py:123`), and `v0.17.0` has no seed fallback (`keyprovider.py:209-227`; `_preflight.py:314-316`). Measured in 32 job, vault and preview test modules: 55 fail on the base (evidence 1). Raise sites: admitting entries `execution/_pipeline_context.py:305` -> `secret_providers/_preflight.py:346` -> `:171` (the closed rebuild of `:316`); sequential `api/jobs/v2_sequential.py:201` -> `execution/_sequential.py:332` -> `keyprovider.py:222`; out-of-core `execution/out_of_core/_runner.py:193` -> `out_of_core/_preflight.py:62` -> `keyprovider.py:222`; streaming, by reading (the native companion is not installed in the probe venv): `execution/_chunked.py:425` -> `_chunked_oracle.py:116` (admission). `POST /unmask` fails the same way (`api/unmask/router.py:150`, engine `unmask.py:254`).
- The failure is logged as `code=none`. `_failure_details` picks the engine code (`api/jobs/_partial_publication.py:174-213`) and the orchestrator passes it to the narrative (`api/jobs/v2_orchestrator.py:271`, `:353`), but `project_failure` keeps only codes in `APPROVED_ERROR_MESSAGES` (`api/persistence/error_projection.py:43-70`) and logs `job_failure code=none exc_type=KeyedStrategyRequiresSecret` (`:153`).
- A job-authored `global_settings.mask_secret_ref` is honored today. With no provider, `admit_mask_key` preflights any configured ref, even for an unkeyed plan (`_preflight.py:314-328`, docstring `:340-342`). Probe: through `run_v2_pipeline`, `env:DECOY_HOST_SECRET` is admitted and `file:/nonexistent/k` fails `missing_mask_secret`, so the server opens a host path the config author chose (evidence 4). New finding F-3.
- Master-key errors carry values, and their traceback locals carry the key text. `api/keys/master.py`: `_decode_env_key` (`:101-114`) binds the env text in `value`, `raw` and `decoded`, raises `f"... (got {len(decoded)})"` at `:113`, and its second decode (`:111`) runs inside an `except`, so a `binascii.Error` chains to the first; `_load_or_generate_file_key` binds the file bytes in `raw` (`:123`) and formats `{_KEY_FILE}` and the length (`:125-133`); `load_master_key` binds the env text in `env` (`:172-175`) and formats nothing for a blank provider (`:162-166`). `api/keys/kms.py`: formatted messages at `:86-88` (with `from exc`), `:112`, `:133-135` (path), `:139-141` (path, `from exc`), `:154` (`from None` inside the handler, so `__context__` is kept), `:156-158` (length, with the unwrapped key bound to `key`), `:168`, `:175-177`, `:179` (`provider!r`). `api/keys/derive.py` binds the master in `master` (`:42`, `:85`) around `_hkdf` (`:28-35`). Probe (evidence 3): a short, a malformed and a non-ASCII `DECOY_MASTER_KEY` each leak the env text into rendered frame locals, two with a chained context. Startup logs the traceback (`api/main.py:1224`), as do job start (`seed_resolve.py:125`, `:141`) and the token-vault key (`api/crypto/vault_key.py:96`).
- Preview never reads the master key by design (`api/jobs/preview_parent.py:844-849`, `preview_seed = secrets.randbits(63)` at `:849`); the bounded preview runs `run_v2_pipeline` in one child (`api/jobs/preview_child.py:246`) after one spawn (`preview_parent.py:858`); node preview filters to the table plus its parents (`:904`) and calls `run_bounded_preview` once (`:905`). On the base both keyed preview routes fail (the eight `test_s6a_bounded_preview.py` failures in evidence 1).
- Nested preview (`api/nested_preview/router.py`, `POST /api/nested/preview`, `:58`) calls `NestedStrategyHandler().run` directly (`:98`) with a `_FakeCtx` that has only `row_errors` (`:48-55`), a seed with `namespace=None` (`:87`), and it re-raises every `StrategyError` but one (`:99-101`). Run on the base (evidence 2): hash, FPE, date_shift and bucket_perturb children fail their namespace check (`_hash.py:45`, `_fpe.py:116`, `_date_shift.py:96`, `_bucket_perturb.py:84`), tokenize fails `tokenize_requires_vault` (`_tokenize.py:75`), and `text_mask` raises `AttributeError` on `ctx.mask_key` (`_text_mask.py:171`): all unclassified 500s. Revision 3's "AttributeError for hash/FPE/tokenize" was wrong in mechanism, right in outcome.
- Config ingress (where a job config is validated): `api/jobs/v2_config.py:29-51` (`PipelineConfig.model_validate` at `:45`), called by the worker submission (`api/jobs/v2_submission.py:123`) and preview (`preview_parent.py:814`); `api/pipelines/v2_validation.py:187` `validate_v2_or_graph` (`model_validate` at `:269`), called by `/validate-yaml` (`api/pipelines/router.py:685`) and job create (`api/jobs/_create_phases.py:337`, which the webhook trigger reaches through `create_job_core`, `api/webhooks/router.py:417`); `POST /unmask` takes `body.config` with no validation (`router.py:113-150`). Pipeline save is structural only (`router.py:82-100`, called at `:417` and `:797`), so a stored ref is refused when it is run, validated or previewed, not when it is saved.

### 1.2 Engine (main `51548303`)

- Stale text for slice E0: `config/_global_settings.py:84-94` still says "pre-GA the keyed surface falls back to `job_seed`" (`:92`); the same line is on A-4.
- `SecretKeyProvider(secret, key_version)` is a frozen dataclass whose generated `__init__` frame holds the `secret` argument; `__post_init__` validates length (`keyprovider.py:134-142`); `repr` is redacted (`:145-148`). So a failing constructor keeps the secret in a traceback local even when the caller binds none (the reason 3.4 boxes the constructor).

### 1.3 CLI (`a5d36b4`)

- Already pinned to `0.17.0` (`pyproject.toml:103`), and `report vault` already admits through the engine (`report.py:2297-2305`: `AdmissionScope`, `admit_mask_key`, `load_vault(..., provider.mask_key())`), from #76. The revision 3 "inspector opens with the job seed" defect is fixed; T15a's premise is gone.
- `decoy unmask` and `decoy report vault` have no `--mask-secret` (`cli/unmask.py:57-115`, `cli/report.py:2211-2247`); only `run.py:237-246`, `tokenize.py:124-133` and `decoy.mask(mask_secret=...)` (`api.py:326`). Their texts still say the YAML ref is the only way (`cli/unmask.py:49`, `:64`, `:95`; `cli/explain.py:232`; `_mask_secret_guidance.py:24-34`), and `report vault --config` still says "must carry the same seed" (`report.py:2226-2227`).
- `decoy run --vault` admits the secret twice: `vault_writer_for_config(config_dict)` (`run.py:791`; engine `vault.py:346-353`) and again inside `run_pipeline` (`run.py:821`) or the chunked entry (`run.py:1443`). A secret that changes between the two reads fails closed at the writer key check (`vault.py:312-331`, `assert_keyed_with`) rather than splitting keys, but it is two reads and an avoidable failure (class 7).
- Three copies of the ref predicate (`run.py:165`, `tokenize.py:98`, imported by `api.py:37`) and three copies of flag injection (`run.py:619-628`, `tokenize.py:222-253`, `api.py:115-150`), plus dead pre-DE-02 engine probes on an exact pin (`run.py:652-667`, `api.py:140-150`).
- Secret error text: engine `MaskSecretError` messages are closed constants at `v0.17.0` (`secret_providers/_errors.py:130-168`, `failure_of`/`error_for`), so revision 3's leak class is closed upstream for secret errors. What remains: four hand-written copies of the secret branch (`run.py:1005-1011`, `tokenize.py:353-360`, `cli/unmask.py:227-233`, `report.py:2306-2311`); `decoy unmask` printing exception text in its runtime branch (`cli/unmask.py:236-237`) and its CSV-read branch (`:189-191`); and handler order in `cli/unmask.py`, where `(ExecutionError, ConfigError)` (`:205`) and `VaultError` (`:210`) precede `MaskSecretError` (`:227`). The classes are siblings under `DecoyError` (`keyprovider.py:57`), so today only a multiple-inheritance class would be mis-caught, and it would lose the surface remedy text, not leak a value.
- The rule-code allowlist is derived from the installed engine's registry (`ci/rule_codes.py:1-13`), so revision 3's "add eight codes" item is obsolete.

## 2. Established solutions (cited; from knowledge, not re-fetched)

- Key hierarchy by HKDF-SHA256 with a distinct `info` per purpose (RFC 5869 section 3.2; NIST SP 800-108 domain separation). The platform already derives pipeline keys and the public job seed this way (`api/keys/derive.py:28-35`, `:74`, `:77-86`); the mask secret is one more labeled child.
- Error projection by code, not by message: SARIF `reportingDescriptor.id` and OWASP ASVS V5.3 (encode against a known-good set), already the pattern of the platform projection (`error_projection.py:1-28`) and the CLI rule codes. Applied to the master-key and mask-secret codes.
- Failure reporting by return value at a secret boundary, raising from a frame that holds no secret: the engine's own admission boundary (`_preflight.py:131-175`, `_OpaqueBox`, `_call_closed`, raise after the handler). The platform cannot import it (private, and the generalized `_opaque` is A-4 only), so it gets a platform-local equivalent of the same shape (3.4).
- Vault Agent, Kubernetes secret volumes and systemd `LoadCredential` render a secret to a file the app reads; Twelve-Factor config in the environment for CI. Applied to the CLI docs (`file:` first, `env:` second, no implicit source).
- Ephemeral per-session keys for exploratory computation (TLS ephemeral keys, the platform's own random preview seed at `preview_parent.py:849`): a key that exists for one request and is never persisted cannot join with, or reveal, durable keyed output.

## 3. Decisions

### 3.1 Config surface

| Consumer | Who names the secret | Surface | Not allowed |
|---|---|---|---|
| Platform | The deployment, never the job author | Existing master key sources only (`DECOY_MASTER_KEY`, `{DECOY_KEY_DIR}/.decoy_master_key`, `DECOY_MASTER_KEY_PROVIDER=kms_aws\|kms_gcp`; `api/keys/master.py:144-177`, `api/keys/kms.py:162-179`). The engine secret is `HKDF-SHA256(master, info="v2-mask-secret:instance")`, 32 bytes, as `SecretKeyProvider(secret, key_version="v1")`, passed as `key_provider=` at every engine entry. Previews use a per-request random provider (3.6). | `global_settings.mask_secret_ref` in any platform config (code `mask_secret_ref_not_allowed`; `None` allowed, any other value refused, `""` included). A second platform secret. A master-key read in any preview path. |
| CLI | The operator, per command | `--mask-secret REF` or `global_settings.mask_secret_ref` (exactly one place), now on `decoy unmask` and `decoy report vault` too (owner decision: add the flag, keep the config key); `env:`/`file:` refs (CLI pre-check, `run.py:165`) resolved by the engine. | Any implicit source (no `DECOY_MASK_SECRET` auto-read, no default key file, no generated key). |

Why the platform derives from the master key: the master key already has backup reminders, a fingerprint, a restore flow and KMS wrapping (`api/keys/key_restore.py`, `backup_reminder.py`, `kms.py`); a second secret would sit outside that machinery. A ref in a job config is a tenant-chosen read of host secrets (F-3), which this threat model keeps in scope.

Domain separation is tested: if the label equalled `v2-job-seed:mask:instance` (`derive.py:74`), the public 8-byte job seed would be the first 8 bytes of the engine secret.

### 3.2 Key sourcing per environment (no weak default)

| Environment | Sanctioned source |
|---|---|
| Platform dev | The auto-generated `.decoy_master_key` (32 random bytes, 0600, `master.py:117-141`). The only fixed key in `api/` stays the A-8 probe (`engine_a8_guard.py:123`, `bytes(range(32))`), allowlisted by file in the T8a sentry. |
| Platform prod | `DECOY_MASTER_KEY` or the KMS envelope provider. |
| Platform previews | A fresh `secrets.token_bytes(32)` per request (3.6). |
| CLI dev | `file:` to a 0600 file from `umask 077; openssl rand -hex 32 > ~/.config/decoy/mask.key`, or `env:DECOY_MASK_SECRET` from `openssl rand -hex 32` (already the CLI guidance text, `_mask_secret_guidance.py:15`). |
| CLI CI / Vault / k8s | `env:NAME` from the CI secret store; Vault Agent or a Kubernetes secret volume rendered to a file, as `file:`. |

The engine certifies length only (`MIN_SECRET_BYTES = 32`, `keyprovider.py:54`). "No weak default" means nothing in either consumer supplies, generates or falls back to key bytes except a CSPRNG.

### 3.3 One resolution path

- Platform: `api/jobs/engine_key.py` (new) holds the only two constructors: `instance_key_provider()` (real jobs, `POST /unmask`) and `ephemeral_preview_key_provider()` (bounded and nested preview). Neither caches, so `reset_cache()` and the restore flow cannot leave a stale provider. The AST sentry (T8a) allows `SecretKeyProvider(` only in `engine_key.py` and `engine_a8_guard.py`.
- Resolution points, one each, none defaulted: a real job resolves once per attempt in `run_v2_pipeline_job` next to `resolve_job_seed` (`v2_orchestrator.py:146-152`), and the same object goes to the vault writer, the `JobRunContext` (new required field beside `job_seed`, `job_run_context.py:59`) and from there to every route (`route_dispatch_exec.py:71-80`, `:108-120`, `:131-140`, `:148-157`), including the streaming coordinator, which today re-resolves the seed on its own (`v2_stream_coordinator.py:136`) and gets the provider threaded instead. Iteration children and sequential re-admission attempts each pass through `run_v2_pipeline_job` (`v2_orchestrator.py:126-127` comment), so each attempt gets its own provider from the same master. `POST /unmask` resolves once per request. The bounded preview child resolves once, inside the child, at its single `run_v2_pipeline` call (`preview_child.py:246`), so no key crosses the process boundary or touches `request.json`.
- Required parameter, no default (class 4): `key_provider` is a keyword-only parameter with no default on `run_v2_pipeline` (`v2_runner.py:140`), `_run_v2_pipeline_streaming` (`:251`), `run_v2_pipeline_streaming_multitable` (`v2_stream_coordinator.py:95`), `run_claim_time_streaming_route` (`:285`), `run_full_frame_branch` (`v2_full_frame.py:70`), `_build_job_vault_writer` (`v2_vault.py:23`), `_run_v2_pipeline_sequential_to_stage` (`v2_sequential.py:148`), `_run_relationship_job_to_targets` (`:355`), `run_sequential_relationship_job` (`:473`), `_run_v2_pipeline_out_of_core_to_stage` (`v2_out_of_core.py:226`), `run_out_of_core_relationship_job` (`:375`) and `JobRunContext`. An omitted provider is a `TypeError` at call time. An explicit `None` reaches the engine, which refuses it for any keyed plan (`keyprovider.py:221-222`) and needs none for an unkeyed one; the sentry also rejects a literal `key_provider=None` under `api/`. The `job_seed: int | None = None` defaults (`v2_runner.py:201`, `:339`, `v2_vault.py:38`) stay: after F13 the seed is generation-only.
- CLI: `src/decoy/_mask_secret.py` (new) holds `is_valid_mask_secret_ref`, `apply_mask_secret(config, flag, *, error, both_set, invalid)` (each surface passes its existing text, so messages do not change) and `admitted_key_provider(config)`, a context manager over the engine's `AdmissionScope` and `admit_mask_key`. `decoy run` enters it once inside the existing `with spinner(...)` (`run.py:478`, extended with an `ExitStack`) and passes the one provider to `vault_writer_for_config(..., key_provider=)`, `run_pipeline(..., key_provider=)` and `_run_chunked_mask` -> `run_mask_pipeline_chunked(..., key_provider=)`; `report vault` uses it instead of its own scope; `decoy unmask` applies the flag through `apply_mask_secret` and lets `unmask_pipeline` admit once (`unmask.py:247-257`). `tokenize.py` and `api.py` use the shared predicate and injection. The helper imports only public engine paths (`decoy_engine.secret_providers`), so the CLI internal-boundary allowlist (`tests/sentry/test_engine_internal_boundary.py`) is unchanged.

### 3.4 Value-free secret boundary

- Platform registry: `api/keys/mask_secret_errors.py` (new; precedent `SEED_ERROR_CODES`, `api/seeds/secrets.py:58`). `MESSAGES` (constant text per code) and `MASK_SECRET_ERROR_CODES` for `master_key_invalid`, `master_key_unavailable`, `kms_misconfigured`, `kms_dependency_missing`, `kms_unavailable`, `mask_key_derivation_failed`, `mask_secret_ref_not_allowed`. `MaskSecretConfigError(ValueError)` carries `code` and `message` only; `MasterKeyError(MaskSecretConfigError)` keeps existing `except ValueError` handlers working. Not engine reasons (owner decision).
- Decide, then raise, from a key-free frame (class 1, traceback locals, the finding 6 fix). `_decode_env_key()` reads the env itself and returns `bytes | None`; `_load_or_generate_file_key()` returns `bytes | str` (a code); `EnvelopeMasterKeyProvider.load()` delegates to `_unwrap() -> bytes | str`. None of them raises; `load_master_key()` and `load()` raise the coded error after the helper returned, from a frame that binds no key, so the exception's traceback holds no frame that ever held key material and has no `__context__`. `KmsError`/`KmsUnavailableError` keep their types and gain `code` plus constant text.
- `run_boxed(work, code)` in the same module: runs `work()`; a platform mask-secret error passes through (it was raised from a key-free frame); any other exception is dropped and `MaskSecretConfigError(code)` is raised after the handler, so `__cause__` and `__context__` are `None` and no inner frame survives. `derive_from_master(info)` in `api/keys/derive.py` wraps `_hkdf(load_master_key(), info)` in it (used by `derive_pipeline_key`, `derive_mask_job_seed_int` and the provider), and `engine_key.py` wraps the `SecretKeyProvider(...)` construction in it too, because the engine constructor's own frame holds the secret argument (1.2). This is the platform-local box the finding asked for; the engine's `_OpaqueBox` (`_preflight.py:131`) is private and A-4's `_opaque` is not at the pin.
- Job narrative: `APPROVED_ERROR_MESSAGES` gains every `MASK_SECRET_ERROR_CODES` entry plus `keyed_strategy_requires_secret` and `weak_mask_secret` (the engine refusals a job can still reach), so these failures log their code instead of `code=none` (finding F-2; it belongs to this boundary, so it is in 3a-i).
- Router surfaces: `POST /unmask` maps `MaskSecretConfigError` and engine `MaskSecretError` to 422 `{code, message}` (registered code, constant text). Nested preview maps coded child errors to 422 with constant text (3.6).
- CLI (4b): `mask_secret_error_text(exc, keyed_remedy)` in `_mask_secret.py` returns the surface's remedy for `keyed_strategy_requires_secret`, else `code: <engine message if it is a str, else empty>`; never `str(exc)`. Used by `run.py`, `tokenize.py`, `cli/unmask.py`, `report.py`; in `cli/unmask.py` it is checked before the `ExecutionError`/`ConfigError`/`VaultError` branches, so dispatch is on the code, not on the class. `decoy unmask`'s runtime branch and its CSV-read branch print the exception type only.

### 3.5 Platform config ingress (`mask_secret_ref` refused)

One validator, `reject_mask_secret_ref(config)` in `api/pipelines/mask_secret_guard.py` (new; FastAPI-free and non-mutating like `config_guard.py`), presence-based, raising `MaskSecretConfigError("mask_secret_ref_not_allowed")`.

| Row | Ingress (at `6e9967e6`) | Guard call |
|---|---|---|
| R1 | `POST /validate-yaml` (`api/pipelines/router.py:685`) | `v2_validation.py` before `:269`, as a `ValidationResult` error with the constant message and `path="global_settings.mask_secret_ref"` |
| R2 | Job create (`api/jobs/_create_phases.py:337`) and webhook trigger (`api/webhooks/router.py:285-302` -> `create_job_core`, `:417`) | same (`validate_v2_or_graph`) |
| R3 | Worker submission (`api/jobs/v2_submission.py:123`, reached from the worker at `api/jobs/runner.py:110`; scheduler, iteration and re-admission runs reach it through the same worker path, by reading, not traced) | `v2_config.py` before `:45` |
| R4 | Bounded preview, job and node routes (`api/jobs/routes_preview.py:59-150`, `api/pipelines/router.py:1272-1363` -> `preview_parent.py:814`) | same (`validate_v2_config`) |
| R5 | `POST /unmask` (`api/unmask/router.py:113`) | the router, before anything else |
| R6 | Pipeline save (`router.py:417`, `:797`; structural only, `:82-100`) | none: storing is not reading; the ref is refused at R1-R4 when the config is used, and T3 proves no execution route bypasses them |

Nested preview takes no pipeline config (`NestedPreviewIn`, `:31-35`) and is not a row. After 3a-ii the engine also ignores the ref whenever a provider is passed (`_preflight.py:309-313`), so F-3 is closed in behavior by 3a-ii and explicitly by 3a-iii.

### 3.6 Nested preview (owner decision, applied)

- `_FakeCtx` gets `mask_key = ephemeral_preview_key_provider().mask_key()`: one key per request, from `secrets`, wrapped in the same provider type, never persisted, never derived from the master.
- The seed gets a constant namespace (`"nested_preview"`), because keyed children refuse `namespace=None`.
- `StrategyError` and `PlanCompileError` from the child (both carry a registered `code`) become 422 `{code, message: "the child strategy cannot run in preview"}`; the engine message is not echoed. `nested_target_matched_nothing` keeps its pass-through.
- Tokenize gets that 422 (`tokenize_requires_vault`): a preview has no token vault and must not create one.
- Why this is not a weak default: the key is a fresh 32-byte CSPRNG value per request, so it is not a default at all; it keys nothing durable (nothing it produces is stored or joinable); and using the instance key here would be worse, not stronger: any developer-role caller (`pipelines.write`) could compute the production keyed value of a chosen input, a dictionary oracle against masked job output, and the route would read the master key, which no preview may do (`preview_parent.py:844-849`). Outputs differing across requests is intended and is asserted.

### 3.7 A-4 is not a 3a dependency; what B-5 adapts

Every engine surface 3a uses exists at `v0.17.0`: `decoy_engine.SecretKeyProvider` (top-level export), `decoy_engine.keyprovider.MaskSecretError`, and `key_provider=` on `run_pipeline` (`execution/_pipeline.py:148`), `run_mask_chunked`/`run_mask_pipeline_chunked` (`native/_chunked_entry.py:596`, `_chunked.py:379`), `run_sequential` (`_sequential.py:231`), `run_fk_out_of_core` (`out_of_core/_runner.py:138`), `vault_writer_for_config` (`vault.py:335`) and `unmask_pipeline` (`unmask.py:221`). 3a never resolves a ref and never reads a `file:` secret, so A-4's `file_access=` and `secret_file_permissions_unsafe` do not touch it. Diffing those signatures between `v0.17.0` and A-4 `16d72eca`: A-4 adds an optional `file_access: FileAccess | None = None` to each entry and to `key_provider_from_ref`, boxes keys internally, and leaves `ctx.mask_key` a `bytes` attribute (A-4 `_hash.py:66`, `:76`, `_fpe.py:176`, `_text_mask.py:171`).

Adapted later, in B-5 (the consumer repin that takes A-4), not here: the platform engine reason-catalog snapshot gains `secret_file_permissions_unsafe` and the registry version (`1.1.0` on main, `1.2.0` on A-4, `reasons/_registry.py:24`); T8a's entry list is derived from engine signatures and so picks up `file_access` with no edit, but B-5 decides whether the platform passes a `file_access` policy; the nested-preview `_FakeCtx` is re-checked against A-4's `StrategyContext` fields; the CLI repin adopts E1 (file-mode refusal), its `file:` test fixtures move to mode 0600, and `mask_secret_error_text` is re-checked against A-4's closed set (A-4 `_errors.py:129-146` `_CLOSED`, `:185-192` `KEPT_FILE_FAILURES`).

### 3.8 `file:` secret permissions (E1)

Built inside A-4 (A-4 `_local.py:89-118`: `_owner_only` checks `S_ISDIR` then mode `0o077` on the opened descriptor's `stat_result`, read through `fileaccess.read_checked`; `tests/secret_providers/test_file_permissions.py`), gated by A-4's own review. Not part of any slice here; the CLI inherits it at B-5. Revision 3's decision text (owner-only, descriptor `fstat`, no bypass) stands as the contract A-4 is gated against.

## 4. Guarantees

- P1 (no seed key): no engine entry called from `api/` or `src/decoy/` runs keyed work with a key derived from `job_seed`; every keyed run, vault write, vault read and unmask uses a provider of at least 32 bytes.
- P2 (one key per attempt or command): within one platform attempt every table, route, chunk and the vault writer use one provider object; within one CLI command the run and the vault use one admitted key.
- P3 (no tenant-chosen source): a platform config never makes the server read an env var, file or provider its author named.
- P4 (value-free): no secret, derived key, reference or secret-bearing path appears in any output surface (6.5), (rendered traceback locals of platform frames are admin-only output, V2-LOG per section 0's Codex g4 HIGH 6 row, and are not part of the V1 guarantee).
- P5 (fail closed with a registered code): every secret failure surfaces as an engine code or a 3.4 platform code, never a 500, an unclassified exception or `code=none`.
- P6 (rotation is explicit): a key change never silently re-keys a vault (section 7).
- P7 (no unprovided entry): no engine entry is reachable from `api/` without an explicit provider (required parameters and T8a).

## 5. Class table

| # | Class | Closing structure | Proving tests (section 10) | Mutation that must turn them red |
|---|---|---|---|---|
| 1 | Value-bearing output, incl. traceback locals and cause chains | 3.4: return-coded loaders, `run_boxed`, constant messages, projection by code; CLI `mask_secret_error_text` | T-i.2, T-i.3, T-i.4, T-i.5, T-iii.4, T-4b.1, T-4b.2 | f-string with the path back into `master.py`; `raise ... from None` inside the handler in `run_boxed`; bind `secret = derive_from_master(...)` and raise in the constructor; print `str(exc)` in `cli/unmask.py` |
| 2 | Cleanup missing on an exit path | CLI: one `AdmissionScope` per command entered through `ExitStack`/`with`; platform: immutable provider dropped with the context; vault written once by `_persist_job_vault` | T-4a.3 (scope closed on success and on each failure) | replace the `ExitStack` entry with a bare `__enter__` |
| 3 | State machine and concurrency | One provider per attempt, never replaced inside it; previews per request | T-ii.4, T-ii.6, T-iii.3 | build the provider per route or per table; let preview call `instance_key_provider()` |
| 4 | Route parity and silent fallback | Required keyword on every wrapper; AST sentry over `api/`; nested preview keyed and coded | T-ii.1, T-ii.2, T-ii.3, T-iii.1, T-iii.2, T-iii.6, T-4a.1 | default `key_provider=None` on `run_v2_pipeline`; drop `key_provider=` at `v2_sequential.py:201`; re-raise `StrategyError` in nested preview |
| 5 | Referential integrity | One provider per attempt and per preview request | T-ii.3 (key identity plus FK, every route), T-ii.6 | give a child table its own provider; build the preview provider per table |
| 6 | Untrusted producer | `reject_mask_secret_ref` at R1-R5; generated ingress walk | T-iii.5, T-iii.7 | truthiness instead of presence; drop the guard call in `/unmask`; add a route with a `config` body |
| 7 | Check then use | CLI single admission (one read); platform never reads a ref after 3a-ii | T-4a.2 | re-admit in `run_pipeline` as well as for the writer |
| 8 | Weak tests and overclaimed evidence | Red records on the named base; labels for characterization and continuity; measured sizes; named pinned edits only | T-12 records; section 13 | n/a (review rule) |
| 9 | Behavior-preserving refactor drift | CLI helper extraction under a characterization golden captured first | T-4a.4 | change one word of a usage text or an exit code |
| 10 | Frozen contract drift | Section 12; snapshots unchanged except the listed additions | T-iii.8, T-4a.5 | rename a `/unmask` field; drop a `--json` key |

## 6. Enumerations from the code

### 6.1 Exit paths

- Platform job (`v2_orchestrator.py`): provider construction failure. Revision 3's pre-mortem asked whether a failure at the pre-`try` setup (`:146-152`) lands as a failed job; it does not today (a raise there escapes `run_v2_pipeline_job` to the supervisor). 3a-ii resolves the provider and builds the writer where `_build_job_vault_writer` already sits, before the `try` at `:221`, right after `resolve_job_seed` (`:148`), which already reads the master key for mask jobs (`seed_resolve.py:125`). So a master-key failure in any of the three (seed, provider, writer) must be recorded the same way the token-vault failure is (`:153-163`: record, raise inside the `try`). The 3a-ii prototype did not do this; it is a required part of 3a-ii and the measured size gains at most 8 lines (section 13). Then the route raises inside the `try`, `_failure_details` picks the code, the narrative shows it (3a-i).
- Platform job success: writer filled, `_persist_job_vault` (`v2_vault.py:65-106`), its write failure recorded as `vault_write_failed` with a fixed message.
- Bounded preview: spawn or child failure -> `PreviewOutcome(kind="failed")` (`preview_parent.py:432`, `:476`, `:501`, `:692`, spawn `:864-872`); success.
- Nested preview: success; coded child error -> 422; `nested_target_matched_nothing` -> pass-through; uncoded engine exception -> 500 (F-6).
- `POST /unmask` (`router.py:113-195`): guard refusal 422 (new), provider failure 422 (new), unknown table 422 (`:120-128`), vault missing 422 (`:100-108`), `ExecutionError` or `MaskSecretError` 422 (`:151-152`, extended), audit write failure 500 (unchanged, deliberate), success.
- Platform startup (`api/main.py:1214-1225`): `MasterKeyError` logged with its constant text and re-raised (unchanged structure).
- CLI `decoy run`: admission failure (now before any read, inside the `ExitStack`), run failure (`run.py:~880-1030`), success with vault write (`run.py:854`), chunked (`:794`, `:1398-1452`). `report vault` (`report.py:2283-2335`): YAML error, non-mapping, flag usage error (new), secret error, `VaultError`, success. `decoy unmask` (`cli/unmask.py:145-240`): flag usage error (new), CSV read error, `PlanCompileError`, `ExecutionError`/`ConfigError`, `VaultError`, `MaskSecretError`, runtime.

### 6.2 Routes and their provider source

| Route | Engine entry (`6e9967e6`) | Provider |
|---|---|---|
| Full frame | `run_pipeline` via `run_v2_pipeline` (`v2_runner.py:226`) via `run_full_frame_branch` (`v2_full_frame.py:99`) | attempt (`instance_key_provider`) |
| Streaming, dispatcher on/off | `run_mask_chunked` (`v2_runner.py:372`), `run_mask_pipeline_chunked` (`:385`), via the coordinator (`v2_stream_coordinator.py:184`) | attempt |
| Sequential | `run_sequential` (`v2_sequential.py:201`) | attempt |
| Out-of-core | `run_fk_out_of_core` (`v2_out_of_core.py:327`) | attempt |
| Vault writer | `vault_writer_for_config` (`v2_vault.py:40`) | attempt |
| Bounded preview (job `routes_preview.py:59`, node `pipelines/router.py:1272`) | `run_v2_pipeline` in the child (`preview_child.py:246`) | one ephemeral per request |
| Nested preview (`nested_preview/router.py:58`) | `NestedStrategyHandler().run` (`:98`) | one ephemeral per request |
| Unmask (`unmask/router.py:113`) | `unmask_pipeline` (`:150`) | per request (`instance_key_provider`) |
| A-8 probe (`engine_a8_guard.py:119`) | `run_pipeline` with a fixed test key | sentry allowlist, unchanged |
| CLI | `run_pipeline` (`run.py:821`), `run_mask_pipeline_chunked` (`:1443`), `vault_writer_for_config` (`:791`), `unmask_pipeline` (`cli/unmask.py:194`), `load_vault` (`report.py:2305`), `tokenize` `run_pipeline` (`tokenize.py:334`), `decoy.mask()` `run_pipeline` (`api.py:505`) | one admission per command |

Other engine-using platform modules were checked and reach no keyed entry: `text_redact/preview.py` (its handler reads no `mask_key`), `org_disguises/core.py` (reads the handler names only), `composites/preview.py`, `subset/*` (`job_seed_for_config` for sampling), `storm/*`, `generation/*`.

### 6.3 FK shapes

- single-column
- composite (always keyed, `_requirements.py:225-228`)
- self-reference
- chain A -> B -> C (sequential and out-of-core)
- child rows in a later batch (streaming, `base_row_offset`)
- two tables in one preview request (job and node routes)

### 6.4 Fields

- `SecretKeyProvider.secret`, `.key_version` (redacted repr, `keyprovider.py:145-148`)
- `JobRunContext.key_provider` (frozen dataclass default repr shows the provider's redacted repr)
- `MaskSecretConfigError.code`, `.message`, `.args`
- `KmsError.code`, `.message`
- `UnmaskOut`, `UnmaskColumnEntry` (unchanged)
- CLI JSON envelopes of `report vault` and `unmask` (unchanged keys)

### 6.5 Output surfaces

- Platform: `Job.log` narrative and `api.job_failure` log line (`error_projection.py:153`)
- Platform: error node `code`/`message`
- Platform: server logs (`v2_orchestrator.py:270` `_log.exception`, startup `main.py:1224`)
- Platform: HTTP 422 bodies of `/unmask`, `/validate-yaml`, job create and nested preview
- Platform: rendered traceback locals of platform frames
- Platform: preview scratch `request.json`
- CLI: stdout
- CLI: stderr
- CLI: `--json` error payload (`run.py:1005-1007`)
- CLI: notifications (type name only, `run.py:1019-1030`)
- CLI: `--help` of the new flags

## 7. Rotation, stated honestly

- 3a with the repin is a one-time change of keyed output identity on the platform: today's `main` (engine 0.16.0) keys masking from the 8-byte job seed; after the repin merge every keyed output, vault and FPE value is keyed by the master-derived secret. The upgrade-determinism gate (`tests/test_golden_determinism_gate.py`) is expected to report CHANGED for keyed pipelines; that is the signal, documented, not suppressed. Pre-release, nothing of value exists (Risk).
- A later key change (new or restored master key) makes existing job vaults unreadable (`vault_key_mismatch`); vaults carry no key identifier (`vault.py` header), and no command re-keys a vault. Keep the old master key as long as old vaults must open.
- CLI: keep the old ref as long as old vaults matter. Nothing in 4a or 4b changes CLI key bytes (the same ref admits the same secret; one admission instead of two).
- Revision 3's T17 (identity across a pin bump) is deleted: there is no pin bump left in this plan (the platform's is the repin branch itself; the CLI's is done).

## 8. Ordering, branches and gating

| Step | Repo, branch | Content | Gate | State after |
|---|---|---|---|---|
| E0 | engine, from main | `config/_global_settings.py:84-94` text; "Consumers" section in `docs/security/secret-providers.md` (sections 3.2, 7) | Codex | Docstring half on main (`6e2e57bd`); the docs half is in 5-v1 (section 0). |
| 5-v1 | engine docs, then CLI, from main | Section 0: Consumers docs, one CLI test, closeout record | Codex | The only V1 build left on row 73. |
| 3a-i | platform (Platform V1, parked), branch from `chore/repin-engine-0.17.0` `6e9967e6`, PR into the repin branch | 3.4 registry and boundary, `engine_key.py`, projection codes | Codex | Repin branch still red on keyed jobs, but failures log their code. Dispatchable now. |
| 3a-ii | platform (Platform V1, parked), from the repin branch after 3a-i merges into it | Provider threaded to every job route and the bounded preview child | Codex | Keyed jobs and bounded previews green (40 base-red tests turn green). |
| 3a-iii | platform (Platform V1, parked), after 3a-ii | `/unmask` provider and 422s, nested preview, ingress guard, T8a sentry | Codex | Repin branch green on F13 surfaces. |
| 4a | CLI, from `main` | Shared helper, flags on `unmask`/`report vault`, single admission | Codex | MERGED with 4b (CLI #78). |
| Repin merge | platform | `chore/repin-engine-0.17.0` into `main` | Owner | Only after 3a-i, 3a-ii, 3a-iii are merged into it and 4a is merged; the repin branch must first merge platform `main` (`8a581fe`, #136 overlaps `pyproject.toml` and the lock). |
| 4b | CLI, after 4a | Error text projection | Codex | MERGED (CLI #78). |
| B-5 | both | Consumer repin to an engine containing A-4 (3.7 list) | separate plan | Not part of this plan. |
| Slice 0 | engine | Vault key floor (`origin/plan/slice0`) | own plan | After 3a and 4a; consumers already supply 32-byte providers. |

The repin branch must not be force-pushed while a 3a slice is open against it; if it moves, each open slice merges the new head (no rebase of a shared branch). The 3a slices need no engine tag and no A-4 merge.

## 8a. Rollback and roll-forward

Premise (owner): pre-release, nothing persisted is of value; each row says what holds if that fails.

| Action | Restores | Cannot restore | Recovery |
|---|---|---|---|
| Revert the repin merge (back to engine 0.16.0 and seed keying) | Seed-keyed output for new runs | Output and vaults written under the derived key are unreadable by seed-keyed code | Prefer roll-forward; keep the master key so a re-applied 3a can read them |
| Revert one 3a slice on the repin branch before the merge | The previous slice's state | Nothing persisted (the branch is unreleased) | Re-apply after the fix |
| Roll forward a bad 3a | n/a | n/a | Never change `v2-mask-secret:instance` or `key_version="v1"` in a hotfix: either is a rotation (section 7) |
| Revert 4a or 4b | The old CLI surfaces | Nothing: key bytes are unchanged | Re-apply |

## 9. Reuse inventory

- Engine: `SecretKeyProvider`, `admit_mask_key`, `AdmissionScope`, `MaskSecretError`, entry `key_provider=` parameters (3.7), registered reasons.
- Platform: `api/keys/derive.py:_hkdf`, `load_master_key`/`reset_cache`, `api/seeds/secrets.py` (registry precedent), `api/pipelines/config_guard.py` (guard shape), `api/persistence/error_projection.py` (projection), `tests/test_engine_internal_import_boundary.py` (AST sentry model), `tests/test_mg1_5_nested_preview.py` (client fixture), `tests/test_s6a_bounded_preview.py` (real-subprocess preview harness).
- CLI: `run.py:619-667` logic (extracted, not rewritten), `_mask_secret_guidance.py`, `tests/sentry/test_mask_secret_ref_shape.py` (canary corpus style), `tests/sentry/test_engine_internal_boundary.py`.
- New, and why: `api/keys/mask_secret_errors.py` (registry and `run_boxed`), `api/jobs/engine_key.py` (the only constructors), `api/pipelines/mask_secret_guard.py` (the only ingress check), `src/decoy/_mask_secret.py` (replaces three copies), one platform AST sentry. A shared test fixture `tests/_engine_key.py` holding a test provider for the 64 direct wrapper calls (tests only; no fixed key under `api/`).

## 10. Acceptance tests

Labels: RED (written first, recorded failing on the named base at the stated location, then green); CHAR (characterization, green on base by design, captured before the change); CONT (an existing test, red on base, that the slice turns green; no edit unless listed as a pinned edit); SENTRY (structural check, with seeded fakes that must fail it). Platform base for 3a-i is `6e9967e6`; for 3a-ii and 3a-iii it is the previous slice's head, and the red record states both (red on `6e9967e6` and on the previous slice). CLI base is `a5d36b4`. "Fails at" is the line that produces the red today.

Every RED test is recorded red on its base in `docs/plans/evidence/` of the owning repo before the fix (T-12), and each new mechanism is mutated once before push (section 5 column) with the red result recorded.

### 10.1 Slice 3a-i (platform)

| Id | Test | Label | Asserts | Fails at (base) |
|---|---|---|---|---|
| T-i.1 | `tests/test_f13_engine_key.py::test_instance_provider_is_a_32_byte_secret_provider` | RED | `SecretKeyProvider` (not `SeedKeyProvider`), `len(mask_key()) == 32`, `repr` holds no marker bytes | `ModuleNotFoundError: api.jobs.engine_key` (absent) |
| T-i.2 | `tests/test_f13_engine_key.py::test_mask_secret_label_is_domain_separated` | RED | secret differs from `derive_mask_job_seed_int()` prefix and from `derive_pipeline_key` over a label corpus; every `info` literal under `api/keys/` and `engine_key.py` is unique (AST); two masters give two secrets; one master is stable; mutation: label `v2-job-seed:mask:instance` | absent |
| T-i.3 | `tests/test_f13_master_key_boundary.py::test_failure_frames_hold_no_key_material[<entry>-<case>]`, entry in `load_master_key`, `derive_mask_job_seed_int`, `derive_pipeline_key`, `instance_key_provider`; case in `short_env`, `malformed_env`, `non_ascii_env`, `short_file`, `unreadable_file`, `hkdf_raises`, `ctor_raises`, `kms_wrong_length` | RED | render every frame below the test frame (`walk_tb(tb.tb_next)`, `capture_locals=True`) and every link of `__cause__`/`__context__`; no marker master, its base64, hex, env text or derived secret anywhere; `__cause__` and `__context__` are `None`. Positive control `::test_scan_finds_a_bound_key` (CHAR) proves the scan sees a deliberately bound local | `master.py:101-114` (`value`, `raw`, `decoded` locals; chained decode at `:111`), `:172-175` (`env`), `:123-133` (`raw`), `derive.py:42`, `:85` (`master`), `kms.py:155-158` (`key`); `instance_key_provider` cells: absent |
| T-i.4 | `tests/test_f13_master_key_boundary.py::test_failure_is_a_registered_code_with_constant_text[<case>]`, cases as T-i.3 plus `blank_provider`, `kms_missing_wrapped`, `kms_unreadable`, `kms_unwrap_raises`, `kms_unsupported_provider`, `kms_missing_key_id`, `kms_dependency_missing` | RED | `code in MASK_SECRET_ERROR_CODES`, `str(exc) == MESSAGES[code]`, no path, length, env value or provider text in `args` | `master.py:113` (length), `:125-133` (path, length), `:162-166` (untyped `ValueError`), `kms.py:86-88`, `:112`, `:133-135`, `:139-141`, `:154`, `:156-158`, `:168`, `:175-177`, `:179` |
| T-i.5 | `tests/test_f13_master_key_boundary.py::test_key_modules_raise_no_formatted_message` | SENTRY | AST: no `raise` in `api/keys/master.py`, `kms.py`, `derive.py`, `mask_secret_errors.py`, `api/jobs/engine_key.py` builds its message with an f-string, `%`, `.format` or `+`; seeded fake module with one f-string raise fails it | red on base at `master.py:113`, `:125`, `kms.py:112`, `:134`, `:140`, `:154`, `:157`, `:179` |
| T-i.6 | `tests/test_f13_error_projection.py::test_mask_secret_failure_projects_its_code[<code>]` over `MASK_SECRET_ERROR_CODES` plus `keyed_strategy_requires_secret`, `weak_mask_secret` | RED | `project_failure(TrustedFailure(code=...))` returns that code and its constant body; the `api.job_failure` record says `code=<code>` | `error_projection.py:43-70` (codes absent), log `:153` (`code=none`) |
| T-i.7 | `tests/test_f13_engine_key.py::test_providers_are_not_cached` and `::test_ephemeral_provider_never_reads_the_master` | RED | `reset_cache()` plus a new master changes `instance_key_provider().mask_key()`; a `load_master_key` spy that raises is never called by `ephemeral_preview_key_provider()`; two ephemeral providers differ | absent |
| pinned | `tests/test_master_key.py:41` (`match="32 bytes"` -> `exc.code == "master_key_invalid"`, same input); `tests/test_master_key_kms.py:224-231` (fake raises `KmsError("kms_dependency_missing")`; assert that code; the "not installed" text assertion stays) | edit | same inputs, stronger assertion (code), not weakened | |

### 10.2 Slice 3a-ii (platform)

| Id | Test | Label | Asserts | Fails at (base) |
|---|---|---|---|---|
| T-ii.1 | `tests/test_f13_route_keying.py::test_wrapper_requires_key_provider[<fn>]` over the 12 callables of 3.3 | RED | `inspect.signature(fn).parameters["key_provider"]` is `KEYWORD_ONLY` with no default | parameter absent (`v2_runner.py:140-152`, `:251-265`, `v2_stream_coordinator.py:95-107`, `:285-295`, `v2_full_frame.py:70-84`, `v2_vault.py:23`, `v2_sequential.py:148-152`, `:355-370`, `:473-482`, `v2_out_of_core.py:226-231`, `:375-384`, `job_run_context.py:44-75`) |
| T-ii.2 | `tests/test_f13_route_keying.py::test_engine_receives_the_attempt_provider[<route>]`, route in `full_frame`, `streaming_dispatcher`, `streaming_legacy`, `sequential`, `out_of_core`, plus `vault_writer` | RED | spy `instance_key_provider` returns a sentinel provider; spies on the engine entries record `key_provider`; every call of the attempt received that same object; `instance_key_provider` called exactly once per attempt | `None`/missing at `v2_runner.py:226-249`, `:372-392`, `v2_sequential.py:201-209`, `v2_out_of_core.py:327-339`, `v2_vault.py:40` |
| T-ii.3 | `tests/test_f13_route_keying.py::test_fk_join_under_the_instance_key[<route>-<shape>]`, routes as T-ii.2, shapes `single`, `composite`, `chain` (sequential, out_of_core), `later_batch` (streaming) | RED | job lands `ok`; every masked child FK value is in the masked parent key set; AND the masked parent keys equal a direct `decoy_engine.run_pipeline(parent_only_config, key_provider=instance_key_provider())` over the same rows (key identity), so a shared-but-wrong key (the seed, or a second provider for every table) fails | job fails `keyed_strategy_requires_secret` at `_preflight.py:316` via `:171` (full frame, streaming), `keyprovider.py:222` via `_sequential.py:332` (sequential) and via `out_of_core/_preflight.py:62` (out-of-core) |
| T-ii.4 | `tests/test_f13_route_keying.py::test_master_reset_mid_attempt_keeps_one_key` | RED | after the writer is built, `reset_cache()` and a new `DECOY_MASTER_KEY`; the run completes, the vault opens with the first provider's key, not the second | keyed refusal (as T-ii.3) |
| T-ii.5 | `tests/test_f13_route_keying.py::test_provider_construction_failure_lands_a_coded_failed_job` | RED | with `DECOY_MASTER_KEY` short, `run_v2_pipeline_job` lands the job `failed` with error node `master_key_invalid` (and `Job.log` code), and does not raise to the supervisor (6.1) | `ValueError` from `seed_resolve.py:125` escapes before the `try` at `v2_orchestrator.py:221` |
| T-ii.6 | `tests/test_f13_preview_keying.py::test_keyed_preview_is_keyed_once_per_request[job_route\|node_route]` and `::test_two_table_fk_preview[job_route\|node_route\|composite]` | RED | keyed preview completes; `DECOY_KEY_DIR` empty and unchanged afterwards with no `DECOY_MASTER_KEY` set (the child cannot have read or created the master); two previews give different hash output; masked `orders.customer_id` within masked `customers.id` (sample sizes keep every parent) | preview child exits 1 with the keyed refusal (`_preflight.py:316`); the eight base-red `test_s6a_bounded_preview.py` tests |
| T-ii.7 | `tests/test_f13_preview_keying.py::test_request_json_holds_no_key` | CHAR | the scratch `request.json` holds no provider and no 32-byte value; green on base (no key exists there today) and must stay green | n/a |
| CONT | the 40 tests red on `6e9967e6` and green on the 3a-ii prototype (`evidence/2026-11-01-f13-rev4/platform-base-red-tests.txt` minus `platform-proto-3a-ii-red-tests.txt`), e.g. `tests/test_v2_runner.py::TestRunV2MaskJob::test_fk_sequential_output_equals_full_frame`, `TestRunV2MaskJobOutOfCore::test_out_of_core_output_equals_full_frame`, `tests/test_s6a_bounded_preview.py::TestRealSubprocessOutcomes::test_node_preview_returns_only_the_requested_table` | CONT | unchanged tests, green | as listed |
| mechanical | 64 direct calls in 30 test files gain `key_provider=TEST_KEY_PROVIDER` (`evidence/.../platform-direct-wrapper-call-sites.txt`); no assertion changes | edit | | `TypeError` (the 23 cells of `platform-proto-3a-ii-red-tests.txt` that are not `/unmask`) |

### 10.3 Slice 3a-iii (platform)

| Id | Test | Label | Asserts | Fails at (base) |
|---|---|---|---|---|
| T-iii.1 | `tests/test_f13_unmask_keying.py::test_unmask_uses_the_instance_key` | RED | `unmask_pipeline` receives the provider from `instance_key_provider()`; a job vault written by a real job opens end to end | `router.py:150` passes none; refusal from `unmask.py:254` |
| T-iii.2 | `tests/test_f13_unmask_keying.py::test_unmask_secret_failure_is_a_422[master_key_invalid\|mask_secret_ref_not_allowed\|keyed_strategy_requires_secret]` | RED | 422, `{code, message}` with the constant text, no echo | 500: `MaskSecretError` not mapped (`router.py:151` maps only `ExecutionError`); `mask_secret_ref` honored |
| T-iii.3 | `tests/test_f13_nested_preview_keyed.py::test_keyed_child_preview[hash\|fpe\|date_shift\|text_mask]` | RED | 200; masked differs from input; two requests differ (hash, fpe, date_shift); a raising `load_master_key` spy is never called | 500: `_hash.py:45`, `_fpe.py:116`, `_date_shift.py:96` (namespace), `_text_mask.py:171` (`AttributeError`), re-raised at `nested_preview/router.py:99-101` |
| T-iii.4 | `tests/test_f13_nested_preview_keyed.py::test_tokenize_child_is_a_coded_422` and `::test_child_error_text_is_not_echoed` | RED | 422 `tokenize_requires_vault` with the constant message; the engine message (column name) absent | 500 at `_tokenize.py:75`, router `:99-101` |
| T-iii.5 | `tests/test_f13_mask_secret_ingress.py::test_ref_is_refused[<row>-<value>]`, rows R1-R5 of 3.5, values `"", " ", 0, [], {}, b"x" (as YAML binary), "env:X", "file:/x", "vault://m/p?version=1", 2 KB string, NUL byte`; plus `::test_null_ref_is_allowed[<row>]` | RED (refusal cells), CHAR (null cells) | refusal with `mask_secret_ref_not_allowed`, constant text, no echo of the value in body or logs; for R3 the worker never reads the path (a `file:` canary path is never opened: `os.open` spy) | accepted today: no guard; at R3 the engine opens the path (`_preflight.py:319-328`, F-3) |
| T-iii.6 | `tests/test_f13_engine_entry_sentry.py::test_every_engine_entry_call_passes_a_provider` | SENTRY | AST over `api/`: every call to an engine callable whose signature has `key_provider` (derived with `inspect.signature`, not hand-listed) passes `key_provider=` and not the literal `None`; `SecretKeyProvider(` only in `engine_key.py` and `engine_a8_guard.py`; `ephemeral_preview_key_provider` called only in `preview_child.py` and `nested_preview/router.py`; `instance_key_provider` called only in `v2_orchestrator.py` and `unmask/router.py`. Seeded fakes that must fail: a `**kwargs` spread, an aliased import, `getattr(decoy_engine, name)`, a `functools.partial`. Fails closed on an unresolvable callee | red on base at `v2_runner.py:226`, `:372`, `:385`, `v2_sequential.py:201`, `v2_out_of_core.py:327`, `v2_vault.py:40`, `unmask/router.py:150`; on the 3a-ii head only at `unmask/router.py:150` |
| T-iii.7 | `tests/test_f13_mask_secret_ingress.py::test_every_config_ingress_is_mapped` | SENTRY | walks the FastAPI app: any route whose body model has, at any depth, a field named `config`, `yaml`, `yaml_content`, `yaml_text` or a `dict[str, Any]`/`Any` field not on a reviewed list, and any `POST`/`PUT`/`PATCH` under pipelines, jobs, unmask, webhooks that reads a raw `Request` body, must map to a row of 3.5; seeded fakes (top-level `config`, a `config` two models deep, a raw-`Request` `POST /jobs/x`) fail it; non-vacuity: it finds the known rows | n/a (sentry) |
| T-iii.8 | `tests/test_f13_unmask_keying.py::test_unmask_response_model_is_unchanged` | CHAR | `UnmaskIn`, `UnmaskOut`, `UnmaskColumnEntry` JSON schemas equal the snapshot captured on base | n/a |
| CONT | `tests/test_unmask_vault.py::TestVaultRoundTrip::test_unmask_with_vault_job_id_recovers_source`, `::TestVaultRoundTripOnOutOfCoreRoute::test_unmask_recovers_source_through_out_of_core_route`, `tests/test_v2_unmask.py::TestUnmaskEndpoint::test_fpe_without_namespace_is_422` | CONT | unchanged | keyed refusal |
| pinned | `tests/test_v2_unmask.py:118-130`: `reversed_unverified` -> `reversed` and the stale comment removed (a real secret now authenticates the reversal, engine `unmask.py` `_authenticated`); values assertion at `:116` unchanged | edit | | |

### 10.4 Slice 4a (CLI)

| Id | Test | Label | Asserts | Fails at (`a5d36b4`) |
|---|---|---|---|---|
| T-4a.1 | `tests/e2e/test_mask_secret_flag_parity.py::test_flag_and_yaml_ref_give_the_same_result[unmask\|report_vault]` and `::test_flag_and_yaml_ref_both_set_is_a_usage_error[unmask\|report_vault]` | RED | `decoy run --vault V --mask-secret file:K` then `unmask`/`report vault` with `--mask-secret file:K` equals the YAML-ref result; both set exits `EXIT_USAGE` with the shared text and no ref value | Typer "No such option: --mask-secret" (exit 2): no option on `cli/unmask.py:57-115`, `report.py:2211-2247` |
| T-4a.2 | `tests/e2e/test_run_single_admission.py::test_run_reads_the_secret_once[full\|chunked]` and `::test_secret_change_between_reads_cannot_fail_the_run` | RED | an `admit_mask_key` spy counts one call per `decoy run --vault`; with a `file:` secret rewritten after the first read, the run succeeds and the vault opens under the first bytes | two admissions (`run.py:791` and inside `run_pipeline`); the rewrite fails at `vault.py:312-331` (`assert_keyed_with`) |
| T-4a.3 | `tests/e2e/test_run_single_admission.py::test_admission_scope_closes_on_every_exit[success\|admission_error\|run_error]` | RED | a spy on `AdmissionScope.close` sees exactly one close per command on each exit | no scope in `run.py` (two engine-internal scopes) |
| T-4a.4 | `tests/unit/test_mask_secret_helper.py::test_usage_texts_and_exit_codes_unchanged[<surface>-<case>]`, surfaces `run`, `tokenize`, `decoy.mask()`, cases `both_set`, `invalid_flag`, `invalid_yaml_ref`, `empty_ref` | CHAR | golden of every message and exit code, captured on `a5d36b4` before the extraction | n/a (green by design) |
| T-4a.5 | `tests/sentry/test_mask_secret_single_path.py::test_one_predicate_and_one_injection` | SENTRY | no module under `src/decoy/` other than `_mask_secret.py` defines a ref predicate body or assigns `["mask_secret_ref"] =`; seeded fake module fails it | red on base at `run.py:165`, `:628`, `tokenize.py:98`, `:253`, `api.py:129` |
| T-4a.6 | `tests/e2e/test_mask_secret_flag_parity.py::test_help_and_docs_name_the_flag` | RED | `decoy unmask --help` and `decoy report vault --help` list `--mask-secret`; `docs/cli-reference.md` regenerated and checked by the existing `test_cli_surface` | flag absent |

### 10.5 Slice 4b (CLI)

| Id | Test | Label | Asserts | Fails at (`a5d36b4`) |
|---|---|---|---|---|
| T-4b.1 | `tests/sentry/test_mask_secret_error_text.py::test_keyed_refusal_gets_the_surface_remedy[<surface>-<double>]`, surfaces `run`, `tokenize`, `unmask`, `report_vault`, `decoy.mask()`; doubles `_MSExec(MaskSecretError, ExecutionError)`, `_MSConfig(MaskSecretError, ConfigError)`, `_MSVault(MaskSecretError, VaultError)` carrying code `keyed_strategy_requires_secret`, raised from the patched engine entry | RED for the three `unmask` cells, CHAR for the rest | exit `EXIT_USAGE` and the surface's remedy text (`_mask_secret_guidance`), whatever else the class inherits | `cli/unmask.py:205-209` catches `_MSExec`/`_MSConfig` as `ExecutionError`/`ConfigError` and `:210-226` catches `_MSVault` as `VaultError`, printing the engine's Python-API text instead of `UNMASK`; the other surfaces already dispatch on the code (`run.py:1008`, `report.py:2306` precedes `VaultError`) |
| T-4b.2 | `tests/sentry/test_mask_secret_error_text.py::test_unmask_runtime_failure_prints_the_type_only` | RED | a patched `unmask_pipeline` raising `RuntimeError("<canary>")` prints `unmask failed: RuntimeError`, no canary | `cli/unmask.py:237` prints `{exc}` |
| T-4b.3 | `tests/sentry/test_mask_secret_error_text.py::test_unreadable_masked_csv_prints_no_cell` | RED | a masked CSV whose parse error quotes a canary cell prints the path the user gave and the error type, no canary (F-5, folded) | `cli/unmask.py:189-191` prints `{exc}` |
| T-4b.4 | `tests/sentry/test_mask_secret_error_text.py::test_real_ref_failures_are_value_free[<surface>-env_missing\|file_missing]` | CHAR | real `env:DECOY_CANARY_<hex>` unset and `file:<tmp>/canary-<hex>/k` missing: no canary on stdout, stderr, `--json`, evidence manifest or notification. Green on base because the engine text is closed (`_errors.py:130-168`); the test keeps it so | n/a |

What 4b does not claim: at `v0.17.0` every engine-raised `MaskSecretError` already carries constant text, and every surface prints `message`, never `str(exc)`, for it; a canary placed in `message` would print on base and after 4b alike, so no test pretends otherwise. 4b's secret-path value is one text function and a dispatch on the code rather than on the class; its value-bearing fixes are the two `decoy unmask` branches (T-4b.2, T-4b.3).

### 10.6 Records

T-12: each slice commits its red record (test ids, base sha, failure line) under the owning repo's evidence directory (platform `docs/plans/evidence/`, CLI the existing `docs/records` or equivalent), states the environment (bootstrap from the lock, Python, engine sha) and lists what it did not run.

## 11. Self-attack

- Required keyword. Counter: `key_provider=None` passed explicitly. The engine refuses `None` for keyed plans (`keyprovider.py:221-222`) and T8a rejects the literal; a computed `None` is caught at run time by the engine, not by structure (stated, not hidden).
- `run_boxed` swallows a real bug. Counter: an `AttributeError` in derivation becomes `mask_key_derivation_failed`. Accepted for this boundary only (the exception may hold key material); the server log keeps the code and the call site, and T-i.3 cells keep the boundary narrow (only master read, HKDF and the constructor are inside it).
- Return-coded loaders. Counter: a new failure path in `master.py` raises inside a helper. T-i.3 and T-i.5 cover the module; a new raise with a formatted message fails T-i.5, and a raise inside a key-bearing frame fails T-i.3 for the cases listed (a new case needs a new cell; the class rule in the worker brief).
- Ephemeral preview key. Counter: someone makes preview deterministic by deriving from the master. T-ii.6 (key directory, different outputs) and T-iii.3 (spy) fail; the module docstring states why.
- Guard. Counter: the ref arrives under another spelling. The engine model is `extra="forbid"`, so only `mask_secret_ref` exists; the guard reads the parsed mapping. Counter: a new ingress. T-iii.7.
- Streaming coordinator. Counter: it keeps resolving its own seed (`v2_stream_coordinator.py:136`) and could later resolve its own provider. T-iii.6 allows `instance_key_provider` only in `v2_orchestrator.py` and `unmask/router.py`.
- CLI single admission. Counter: the engine admits again internally when a provider is passed. With a provider the engine does not read the ref (`_preflight.py:309-313`); T-4a.2 counts reads, not calls.
- CLI helper extraction. Counter: tokenize checks the flag shape before the both-set rule (`tokenize.py:222-236`), unlike `run`. 4a keeps tokenize's order (only its predicate is shared) so T-4a.4 stays byte-identical; a later unification is a visible text change, not drift.

## 12. Frozen contracts touched

- Engine: none (E0 is a docstring and docs).
- Platform: `POST /unmask` gains 422 codes (`mask_secret_ref_not_allowed`, the 3.4 codes, engine `MaskSecretError` codes), request and response models unchanged (T-iii.8); `/validate-yaml` and job create gain one error code; nested preview gains 422 child codes where it returned 500; `APPROVED_ERROR_MESSAGES` gains codes (additive); `JobRunContext` and twelve internal function signatures gain a required keyword (internal). Golden keyed outputs change once with the repin (section 7).
- CLI: `--mask-secret` on `decoy unmask` and `decoy report vault` (additive; `docs/cli-reference.md` regenerated in 4a); guidance text for `unmask` and `report vault` names the flag; `report vault --config` help text corrected; exit codes and `--json` keys unchanged; `decoy unmask` runtime error text shortened to the type (4b).

## 13. Slices (measured)

Production sizes are measured from the prototype diffs in `docs/plans/evidence/2026-11-01-f13-rev4/` (`git apply --numstat`); they are the size of the change, not a promise of its exact text. Test lines are a hard cap the gate enforces (a slice over its cap is split, not waved through); production plus cap is at most 800 for every slice.

| Slice | Repo | Production files (measured) | Production lines (measured +/-) | Test-line cap | Total cap | Content |
|---|---|---|---|---|---|---|
| E0 | engine | 1 code file (docstring) + 1 doc | under 60, docs | 0 | 60 | 1.2, 3.2, 7 text (docstring on main; docs half moved to 5-v1) |
| 5-v1 (V1) | engine, CLI | 1 doc (engine); 1 test module edit (CLI) | under 60, docs | 80 | 140 | section 0 |
| 3a-i | platform | 6: `api/keys/mask_secret_errors.py` (new, 61), `api/keys/master.py` (+44/-44), `api/keys/kms.py` (+37/-35), `api/keys/derive.py` (+10/-4), `api/jobs/engine_key.py` (new, 46), `api/persistence/error_projection.py` (+6) | +204/-83 = 287 | 320 | 607 | 3.3 constructors, 3.4, T-i.* |
| 3a-ii | platform | 10: `job_run_context.py`, `v2_orchestrator.py`, `v2_vault.py`, `v2_runner.py`, `v2_full_frame.py`, `route_dispatch_exec.py`, `v2_stream_coordinator.py`, `v2_sequential.py`, `v2_out_of_core.py`, `preview_child.py` (all `api/jobs/`) | +41/-3 = 44, plus at most 8 for the 6.1 pre-`try` recording (52) | 520 (of which mechanical call-site edits at most 100: 64 sites, 30 imports, one fixture module) | 572 | 3.3 threading, T-ii.* |
| 3a-iii | platform | 5: `api/pipelines/mask_secret_guard.py` (new, 22), `api/jobs/v2_config.py` (+2), `api/pipelines/v2_validation.py` (+9), `api/unmask/router.py` (+16/-2), `api/nested_preview/router.py` (+16/-3) | +65/-5 = 70 | 560 | 630 | 3.5, 3.6, `/unmask`, T-iii.* |
| 4a | CLI | 8: `src/decoy/_mask_secret.py` (new, 57), `cli/run.py` (+30/-57), `api.py` (+19/-32), `cli/report.py` (+29/-6), `cli/unmask.py` (+24/-2), `_mask_secret_guidance.py` (+6/-5), `cli/tokenize.py` (+2/-6), `cli/explain.py` (+1/-1); plus `docs/cli-reference.md` regenerated (at most 20) | +168/-109 = 277 (+20 docs) | 420 | 717 | 3.3 CLI, T-4a.* |
| 4b | CLI | 5: `_mask_secret.py` (+10), `cli/run.py` (+2/-3), `cli/tokenize.py` (+5/-2), `cli/unmask.py` (+3/-5), `cli/report.py` (+1/-4) | +21/-14 = 35, plus at most 6 to move the secret branch ahead of the class branches in `cli/unmask.py` and fold F-5 (41) | 300 | 341 | 3.4 CLI, T-4b.* |

Old slices removed: revision 3's 3a (split above), 3b (the pin bump is the repin branch), 4a and 4b (rebased and re-split above), E1 (inside A-4), 4c (optional `decoy keys init`, still unscheduled, Q4).

## 14. Pre-mortem

- A 3a slice merges into the repin branch while the repin worker force-pushes: section 8 forbids it; slices merge the new head.
- The repin merges after 3a-ii only: keyed jobs work but `/unmask` fails closed and nested preview 500s; section 8 requires all three.
- A worker loosens a golden to make keyed outputs pass: forbidden; the determinism gate's CHANGED is documented (section 7), not re-recorded silently.
- A test double fakes `run_v2_pipeline` without `key_provider`: the fake must accept it (T-ii.2 spies record it).
- 4a changes a usage text while extracting: T-4a.4 golden.
- B-5 forgets the A-4 adaptation list: 3.7 lists it; it is the B-5 plan's input.

## 15. What I could not verify, and what I did not run

- Revision 5 added section 0 and changed the status line and the section 8, 13 and 18 rows. It was written by a plan author and has not been reviewed. Sections 1 to 17 otherwise keep revision 4's text and citations (engine `51548303`, platform `6e9967e6`, CLI `a5d36b4`); only section 0 was checked at engine `6e2e57bd` and CLI `f77de8d`. The 5-v1 CLI test was not written or run, and the six CLI modules were not re-run for this revision.
- CLI: the repo is not attached to this session; it was read from a public clone at `a5d36b4`. No CLI code was executed: the clone's `scripts/dev/bootstrap.sh` was refused by the session's permission policy, so no CLI baseline, test or probe ran. CLI prototype files were only parsed (`ast.parse`). Every CLI "fails at" in 10.4-10.5 is from reading code.
- Platform: probes and two test runs only (evidence README: 32 job/vault/preview modules on base and on the 3a-ii prototype; master-key, nested-preview, validation, config and A-8 modules on the 3a-iii prototype). Not run: the full platform suite, `scripts/dev/gates.sh`, mutation testing, the native dispatcher (companion not installed), PostgreSQL-marked tests. The repin worker's whole-suite counts (249 failed, 311 errors, 508 with the keyed error) were not reproduced.
- Engine: no engine test was run; engine facts are from `git show`/`git grep` at the Baselines refs.
- The prototypes are sizing artifacts: 3a-ii lacks the 6.1 pre-`try` recording, and none of them has its tests. They were never pushed to the platform or CLI.
- `HOSTILE-PLAN-REVIEW.md`, `DEFECT-CATALOG.md` and the owner-decision file were not re-read this revision.

## 16. Findings, owner questions and closed decisions

Findings (class, owner, disposition):

- F-1 (class 4, two sources for one decision; input from the repin worker). Studio writes the engine key `post_scan: {integrity: ...}` (repin `2a0568c`) while the post-mask hook gates on the DB column `pipeline.run_storm` (platform `api/storm/post_mask_hook.py`, not re-traced here). Owner: the STORM rename wave. Not in any F13 slice.
- F-2 (class 1/P5, error projection logs `code=none`; input from the repin worker). Verified at `error_projection.py:43-70`, `:153`. Owner: this plan, slice 3a-i, because it is the error boundary of these codes (3.4). The wider pattern (every engine code renders `code=none`, by the module's own design, `error_projection.py:19-22`) is not changed here: it is an observability decision for the platform owner.
- F-3 (class 6, untrusted producer; new). On the repin head a job-authored `mask_secret_ref` is admitted and its `file:` path opened by the server (1.1, evidence 4). Owner: this plan; closed in behavior by 3a-ii (provider wins, `_preflight.py:309-313`) and explicitly by 3a-iii (guard), and one more reason the repin must not merge before 3a-iii.
- F-4 (class 4/P5; new, reported). Before 3a-ii, a master-key failure during attempt setup (`v2_orchestrator.py:146-152`) escapes before the `try` at `:221` (6.1). Folded into 3a-ii with T-ii.5 because 3a-ii moves provider construction there.
- F-5 (class 1; new, folded into 4b). `decoy unmask` prints the CSV read exception text (`cli/unmask.py:189-191`, `could not read {masked}: {exc}`), which can quote a cell. Same class and same function as T-4b.2, so 4b fixes both (T-4b.3).
- F-6 (class 4/P5; reported, engine-owned). Nested preview still returns 500 for an uncoded engine exception, e.g. `faker` with no provider (`ValueError`, `_faker.py:55`). The 3.6 fix maps coded errors only. Owner: engine (code the error) or the platform owner (a generic 422 for the route).

Defaults applied unless Cam objects: Q3 native `vault://` in the CLI deferred (file pattern instead); Q4 no `decoy keys init`; Q6 no key id in the vault header; Q8 platform `key_version` constant `"v1"`.

Closed by the owner on 2026-11-01 and applied: `--mask-secret` on `unmask` and `report vault` with the config key kept; nested preview gets one ephemeral per-request key, never persisted, never master-derived; the repin worker's two inconsistencies are input (F-1, F-2); slice order 3a-i, 3a-ii, 3a-iii, 4a, then the repin merges.

## 17. Worker evidence rules

Name the class of every finding and fix the class, not the instance. Write the tests first and record them red on the named base (state the base sha and the failing line) before the fix; run baselines from the lock (`scripts/dev/bootstrap.sh`, `scripts/dev/gates.sh --baseline`) in the same environment as the branch and say so. Before pushing, mutate each new mechanism once (section 5) and show its test goes red; never commit with a mutation applied. Self-attack the diff with value-bearing canaries in ordinary shapes (64 hex, base64 of 32 bytes, a long integer, a path under a canary directory). Never widen a threshold or allowlist beyond what is stated, skip a test, weaken an assertion or edit a pinned test except the edits named in section 10. If a defect of a catalog class appears that this plan does not list, stop and report. Measure the slice before pushing; over its cap means split, not merge.

## 18. Review history

- After revision 4: Dennis round 1 on `18847ac0` for the CLI slices (GO-WITH-FIXES, 1 HIGH: slice 4a-0, refuse `--chunked --vault`); CLI 4a/4b code gates (Codex NO-GO on `b9fd22db` and `b97df3a6`, fixed in `bdadb4fb`), merged as CLI #78. The platform slices were not reviewed again. Revision 5 is section 0.
- Codex r5 on `17ff7fbb` (NO-GO: 1 V1, 3 V2-LOG, no DESIGN): the guarantee claimed admission before any source read. Revision 5.1 narrows the `vault://` guarantee and the 5-v1 test (section 0); no ordering or logic change.
- Codex round 1 on `fef6c78c` (GO-WITH-FIXES, 3 HIGH, 3 MEDIUM, 2 LOW) and argon round 2 on `6e767cc8` (GO-WITH-FIXES, 3 MEDIUM, 2 LOW): folded in revisions 2 and 3; their structures stand where the code still supports them (value-free boundary, required providers, ingress table, preview once per request, domain separation). Details in the git history of this file (revision 3 is `774ab826`).
- Codex on revision 3 `774ab826` (NO-GO: 3 BLOCKER, 2 HIGH, 1 MEDIUM): all seven accepted after checking each against the code; disposition in section 0. Corrections found while re-verifying, beyond the findings: the nested-preview failure mechanism on `v0.17.0` (namespace and vault checks, `AttributeError` only for `text_mask`); the job-authored ref is honored on the repin head (F-3); the engine constructor's own frame holds the secret, so the revision 3 "one expression, no local" rule was not enough (3.4); provider construction failure escapes the job's `try` (F-4); the CLI rule-code item is obsolete (1.3); save is structural only, so the ingress table changed (3.5).
