Status: plan (rev 3.3 for E1: rev 3.2 plus the 2026-10-08 strict-error-boundary delta below; pending Codex plan gate on the delta)

# PLAN: Close the host-file read/write class at the engine's open calls (engine + platform)

Status: plan, rev 3.1 (Opus, 2026-10-02). Build-ready pending owner acknowledgement (Codex PLAN gate
reached its three-round cap; round-3 patches applied). Risk class R3
(authorization boundary; a wrong design fails silently as a leaked or overwritten file).

**Revision 3.1 note.** Codex PLAN gate round 3 on rev 3 (`0e8c3b24`) returned 0 BLOCKER, 2 HIGH,
1 LOW, closing `OutputTxn` confinement, the D9 package exemptions and Parquet publication. The review
cap was reached, so Codex's own round-3 plan-text patches are applied here after the three-round
cap, narrowing or completing only, with no other changes and no acceptance test weakened. HIGH 1
(STORM pending rehydration): D7 "Binding and rehydration" and the separated grant-fd and resource
lifetimes; AT-P14 extended; Appendix A2 STORM pending success control. HIGH 2 (descriptor-only
primitives): D9 rule 2 split into descriptor-only and name-resolving classes, with new fixtures.
LOW: K14 and D7 self-test step 4 (`/proc` mandatory only for the full and fd-handoff classes).

**Revision 3 note.** Folds Codex PLAN gate round 2 (REVISE: 2 BLOCKER, 2 HIGH; H2, H4, H5, H6 of
round 1 closed) as patches, no redesign, no acceptance test weakened. B1 (`OutputTxn` confinement):
the transaction validates every child name centrally on every operation (D1 "Component
confinement"; AT-E6, AT-E14). B2 (package-wide exemptions): removed; third-party sinks only in exact
adapter modules, raw fd-relative primitives only in an exact list of capability implementation
modules, everything else item by item in the registry (D9, Appendix B; AT-E1, AT-P2). H1
(platform-generated staging): new `StagingGrant` capability bound to owner and a server-minted id,
with retention and fd transfer for both scheduler modes (D7 "Platform staging"; Appendix A2 split into
cloud staging, preview scratch and STORM pending rows; AT-P14). H2 (directory publish semantics):
per-sink semantics preserved; the Parquet directory sink publishes with fd-relative
`renameat2(flags=0)`, no exchange-and-delete (D1 "Multi-file directory transactions"; AT-E6).
Overbuild trim: the full library probe runs only where those adapters are used (D7 self-test).

**Revision 2 note.** Folds Codex PLAN gate round 1 (REVISE: 2 BLOCKER, 6 HIGH; architecture accepted
as directionally right and proportionate) and Cam's 2026-10-02 owner decisions. B1: directory
capabilities, opaque stage transactions, capability retention for delayed and lazy I/O, per-task
`copy_context` (D1, D2, D4). B2: sentry rewritten around alias resolution, named adapter modules and
an item-specific registry; whole-`api/` and whole-engine scans; full sink registry; expanded syscall
trace (D9, D10, Appendix B). H1: full P1 platform inventory with a per-surface table (P1, Appendix
A2). H2: denial hygiene at every boundary (D5). H3: CLI logging and accepted output names preserved;
compatibility items are unchecked evidence gates (D8, E1). H4: DuckDB reads and writes (D8). H5:
per-process `openat2` self-test, no symlink opt-in (D3, D7). H6: explicit dependency graph (Slices).
Acceptance tests strengthened per the gate. Cam additions: operator vs admin principle (Guarantee 8,
D7, CD-1), project-scoped sharing seam for S1B (D7, K13), prodsim on hold (below).

Cam decision 2026-10-01: fix engine and platform together, ordered before scheduler S14.

**Scheduling note (Cam, 2026-10-02).** Prodsim (S14, S15) is on hold until Cam is present. Every
reference to S14 or S15 in this plan is dependency ordering (what must land before what), not a
schedule.

Repos and bases: decoy-engine `origin/main` @ `64e44dac` at rev 2 (rev 1 inventoried `25710b5a`, version
0.7.0). E1 does not build on either SHA as a fixed base: it rebases onto the engine `main` that
exists after B5 lands or freezes, and reruns the full sink inventory there (Slices, step 3).
decoy-platform `origin/main` @ `e314b4ae` (pins `decoy-engine>=0.5.0,<0.7.0`; the A8p branch moves it
to `<0.8.0`).

Supersedes:
- platform `fix/arbitrary-file-read-write` @ `0c5285d0` (three dennis BLOCKs, never merged). Harvest
  list in "Slice P1".
- platform `docs/file-access-authorization-redesign` @ `1140ebdc` (rev 6, Codex PLAN gate round 4:
  0 BLOCKER / 0 HIGH / 2 MEDIUM, all folded, never reached GO). This plan keeps its inventories, its
  strace detector, and its platform-only surfaces. It changes the order and the primary mechanism:
  rev 6 shipped a platform field walker first (Slice A) and moved enforcement into the engine two
  slices later. Here the engine enforcement ships first and no field walker is built at all. The
  reasons are in "Why not a better walker".

## Problem

Any authenticated user of a Decoy box can make the server read or write files the user has no right
to. Reproduced live on `main` or on the fix branch, 2026-09-26:

| Id | Vector | How it was shown |
|---|---|---|
| V1 | `sources.<n>.path` absolute host path (master key, `/etc/*`) | live, main |
| V2 | relative `sources.<n>.path` outside `uploads/`, resolved against the worker CWD (`.decoy_master_key` in dev) | live, main |
| V3 | `uploads/<stored_name>` of another user's upload | live, main |
| V4 | unregistered file in `upload_dir` | live, main |
| V5 | `quarantine.output_path` writes anywhere the server can write | live, main |
| V6 | `code_set.provider_config.corpus_source: customer:<host path>` copies host content into job output | live, FIX r1 |
| V7 | `joint_mask.provider_config.reference: customer:<host path>` | code, FIX r1 |
| V8 | `generate_columns[].snapshot_file` host path (`type: statistical`) | code, FIX r1 |
| V9 | `global_settings.mask_secret_ref: file:<host path>` | code, FIX r1 |
| V10 | denial body names another user's `stored_name`; denial differs for missing vs foreign (existence oracle) | live, FIX r1 |
| V11 | resolved `${env.*}` secret values echoed into job logs | live, FIX r1 |
| V12 | denied host parquet's schema (column names) in the validation error | live, FIX r1 and r3 |
| V13 | claim-time pricing (`worker_budget.price_job_at_claim`, `admission._cheap_file_size`, `scheduler_claim._local_source_identity`) stats and reads footers before the run gate | code, FIX r2 |
| V14 | iteration children resolve and persist templated host paths into `yaml_snapshot`, then get priced, before any gate | code, FIX r2 |
| V15 | bare (no `customer:`) `joint_mask.reference: ../../x` or absolute: `reference_tables/_loader.load_table` builds `_DATA_DIR / f"{name}.parquet"` with no registry check | live, FIX r3 |
| V16 | `POST /api/pipelines/validate-yaml` (any logged-in user) runs `validate_v2_or_graph` -> `validate_joint_mask_config` -> `load_table` before any gate: schema, existence, size | live, FIX r3 |
| V17 | graph-mode `POST /api/jobs` validates (and loads) before the create gate | code, FIX r3 |
| V18 | `snapshot_file` on any generate type: `generators/derivation._snapshot_content_digest` opens it unbounded (existence oracle; `/dev/zero` read) | live (oracle), FIX r3 |
| V19 | `${var.*}`, `${trigger.*}`, `${iteration.value}` resolved after the check | live, MCP slice-0 r1 |
| V20 | webhook jobs judged as the admin who created the webhook | live, MCP slice-0 r2 |
| V21 | `nested` strategy forwards child `joint_mask`/`code_set` config past positional checks | rev 6 Codex r3 |
| V22 | `POST /api/data-tables/query`: DuckDB `read_text`, `read_blob`, `glob`, `read_json_auto`, `parquet_metadata`, `FROM '/path'` past the regex denylist | rev 6 Codex r1 |
| V23 | legacy `api/files/router.py` path-token routes: string-prefix containment (`outputs_backup/` escapes), no owner check | rev 6 Codex r2 |
| V24 | STORM `file_id` by id with no owner check (route and worker) | rev 6 audit |
| V25 | `/api/files` upload list returns every user's `stored_name` | rev 6 Codex r3 |

**Root cause.** Every fix so far put the check in the platform and the open in `decoy_engine`. The
check enumerated a hand-kept list of config fields; the engine opened whatever string reached a
`pq.read_table`, `open`, or `pl.scan_parquet`. Each round closed the fields it knew about and the
next adversarial pass found another field (V6 to V9, then V15, V18, V21), another code path that
opened before the check (V13, V14, V16, V17), or a check that ran on a different value than the open
(V19). The list lives in one repo and the opens in another, so the list cannot be complete. The
engine today has no concept of "who is asking": a path in a config is ambient authority.

## Guarantee (pinned) and non-goals

**Guarantee.** With engine 0.9.0 (the release carrying Slice E1) and a platform pinned to it:

1. **One gate.** Every file the engine opens, stats, lists, creates, links, renames or deletes on
   behalf of a caller or a config value goes through one object, the `FileAccess` policy passed in
   by the caller, or a narrower capability that policy issued (file handle, directory capability,
   output transaction). Third-party file APIs are called only from an exact list of named adapter
   modules that accept those capabilities; raw fd-relative primitives only from an exact list of
   capability implementation modules, under rules that forbid caller or config path arguments.
   Every other call site, including other files inside `fileaccess/` and `api/file_access/`, reaches
   a filesystem sink only as an item listed one by one in a reviewed registry (D9). There is no
   package or directory exemption and no general exemption for module constants or "non-user"
   expressions. A CI sentry enforces this.
2. **No ambient fallback in the platform.** Every platform process that imports the engine (API,
   queue worker, scheduler, execve'd dispatch worker, preview child, launcher, any other entry point
   the builder finds) calls `decoy_engine.fileaccess.forbid_ambient()` at startup. After that call
   any engine entry point invoked without an explicit policy raises `FileAccessUnavailable` instead
   of falling back to the permissive CLI policy. The call is one-way for the process.
3. **Deny by default, decided on the opened object.** The platform policy grants a request only if
   the resolved locator maps to an allowed root for that principal and role, is opened beneath that
   root with `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)` (always; there
   is no symlink-following option), is a regular file or a directory of a directory role (`fstat`),
   is not a denylisted inode (master key, key directory, database file), and, for uploads, passes the
   single upload-readability predicate (D7). The engine then reads that file descriptor, never the
   path again.
4. **Same rule before, during and after the run.** Every platform code path that touches a
   config-named or job-produced file (validate-yaml, create, preview, nested preview, admission
   pricing, claim identity, `pin_source`, iteration fan-out, analytics, STORM, Quality, Synth,
   evidence, vault persistence, unmask, data-table SQL) uses the same policy object or a capability
   it issued, or does not touch the file. Order no longer matters because there is no "before the
   gate": the gate is the open.
5. **One denial, nothing attached.** A denied request produces one exception type with one message,
   `file access denied: <field key>`, where the field key is the config position (for example
   `tables.orders.columns.zip.joint_mask.reference`), escaped and bounded (D5). It is raised
   `from None` and crosses no process, API, job-row, webhook or log boundary with a cause, context,
   traceback, frame locals, warning or third-party exception text attached. It never contains the
   locator, a resolved path, a basename, a `stored_name`, a variable value, or file contents.
   Missing, foreign, outside-root, symlink, non-regular, unresolved-template and denylisted cases
   produce the same normalized API body, job log and job row (V-test preamble).
6. **Writes are rooted.** Quarantine output, target files and directories, and vault files are
   created only beneath a platform-allocated root (per owner after Slice P3), through
   directory-fd-relative calls inside a policy-issued output transaction.
7. **CLI unchanged.** With no policy argument and no `forbid_ambient()`, engine behavior, output
   bytes, output names, accepted names, the default log file, determinism baselines and error text
   are unchanged, except the two narrow schema corrections in "Behavior changes" (bare reference
   names, DuckDB external access). Any further difference found by the E1 characterization tests is
   either removed or added to that list with a migration test and a CHANGELOG entry before E1 merges.
8. **Operator sets host reach; admins do not.** Anything that widens what the server process can
   reach on the host (server file roots, key material location, database file location) is set by the
   server operator in deploy config or environment, read once at process startup. No API route, MCP
   tool or web UI control can set or widen it. In-app admins manage users, groups, projects and
   connections only. The web UI may display these settings read-only.

**Non-goals.**
- A malicious `decoy_engine` package or malicious native libraries. The engine is first-party and
  trusted but fallible; the design survives engine mistakes (a new field, a new open), not sabotage.
- OS sandboxing (seccomp, Landlock as the gate, per-job containers). Seccomp cannot inspect path
  arguments; a per-job sandbox is disproportionate for a self-hosted single-org box (up to ~25 users).
  Landlock stays an optional CI detector.
- Multi-tenant isolation, per-tenant encryption, timing side channels on the denial path.
- Cloud sources and targets (`credentials_ref`, `endpoint_url`): SSRF and credential-scope class,
  tracked as the named pre-GA follow-up FA-2 (CD-7, K5).
- `mask_secret_ref: env:NAME` choosing any worker env var: not a file read; FA-2 (K6).
- Rewriting the engine's public API around handles. Entry points gain one keyword-only argument;
  the vault and unmask entry points also accept a handle (D1).
- Project-scoped upload sharing. This plan builds the seam; S1B turns it on (D7, K13).

## Established patterns surveyed

Each implementing module cites the ones it uses in its docstring.

- **Object capabilities.** Dennis and Van Horn, "Programming Semantics for Multiprogrammed
  Computations" (CACM 1966); Miller, "Robust Composition" (PhD thesis, 2006). Authority travels as an
  unforgeable reference held by the code that uses it, not as a name resolved against ambient state.
  Here: the `FileAccess` object and the handles, directory capabilities and transactions it issues
  are the capabilities; a string in a config is only a request. Objects that do I/O later keep the
  capability they were built with (D2), which is the capability discipline applied to time.
- **Capsicum** (Watson, Anderson, Laurie, Kennaway, USENIX Security 2010) and **WASI preopens**
  (`wasmtime_wasi::WasiCtxBuilder::preopened_dir`). A process reaches files only through directory
  descriptors it was granted. Here: each root is a directory fd opened once at policy construction,
  and directory-valued inputs are directory capabilities over an already-open fd.
- **Rust `cap-std`** (Bytecode Alliance) and **Go 1.24 `os.Root`**. Library-level traversal-resistant
  file APIs that open relative to a root directory handle and refuse escapes, using `openat2` with
  `RESOLVE_BENEATH` where available. `cap-std`'s `Dir` (open, read_dir, create_dir, rename within
  the handle) is the model for the directory capability and output transaction.
- **`openat2(2)`** (Linux 5.6, `RESOLVE_BENEATH`, `RESOLVE_NO_SYMLINKS`, `RESOLVE_NO_MAGICLINKS`,
  `RESOLVE_IN_ROOT`). Path resolution and the containment decision happen in one kernel call, which
  removes the check-then-open window (CWE-367). The platform already calls it through `ctypes`
  (syscall 437) in `api/launcher/ack.py::probe_openat2_support`. That probe runs only in launcherd and
  treats anything other than `ENOSYS` as support, so it is not reused as the gate; D7 adds a
  per-process self-test.
- **`renameat2(2)`** with `flags=0` (plain `rename(2)` semantics, relative to directory fds) for the
  existing Parquet directory sink, and `RENAME_NOREPLACE` only where a sink already refuses any
  existing destination. `RENAME_EXCHANGE` is not used.
- **chroot jails**, and why they are not used: `chroot` needs root, is per-process rather than
  per-request, and is escapable by a process that keeps a directory fd outside the new root. The
  per-request equivalent is `RESOLVE_IN_ROOT` / `RESOLVE_BENEATH`.
- **Django `django.utils._os.safe_join`** and **Werkzeug `werkzeug.security.safe_join`** (Flask).
  Lexical joins: normalize, reject absolute and `..`, check the prefix with a trailing separator.
  Neither resolves symlinks and both leave a window between the check and the open. Used here only
  as the lexical pre-filter for uploads-form locators, never as the gate.
- **`os.path.realpath` + `os.path.commonpath`** containment. Correct for symlinks at check time,
  still racy (the tree can change before `open`), and `str.startswith` variants admit sibling
  prefixes (`/out` vs `/outputs_backup`, the V23 bug). Used only by the portable fallback for
  non-Linux CLI use of `RootedFileAccess`, which is documented as not race-free.
- **Python data model for exception chaining** (PEP 3134, PEP 415 `raise ... from None`): a raised
  exception carries `__cause__`, `__context__` and `__traceback__` (and through it every frame's
  locals) unless explicitly suppressed. The denial path is built on that.
- **Static def-use resolution**: `beniget` (already approved by Cam as a test-only dependency for
  S13b) resolves imports, re-exports and assignment aliases for the sentries.
- **CWE-22** (path traversal), **CWE-59** (link following), **CWE-117** (log injection), **CWE-209**
  (information exposure through an error message), **CWE-367** (TOCTOU), **CWE-532** (sensitive data
  in logs), **OWASP API1:2023** (Broken Object Level Authorization: check every client-supplied
  object reference against the principal at the point of use).
- **PEP 578** (audit hooks are a detector, not a sandbox; `os.stat` raises no audit event), so the
  runtime detector for tests is `strace -f`, as rev 6 established.
- **DuckDB "Securing DuckDB"**: `enable_external_access = false`, `lock_configuration = true`,
  `autoinstall_known_extensions = false`, `autoload_known_extensions = false`,
  `allow_unsigned_extensions = false` are the vendor controls for untrusted SQL.

## Design

### Why not a better walker

Rev 6 Slice A, and all three FIX rounds, decide by walking the config for fields that name files.
The walker must know every field, every scheme, every nesting form, and must run on the value the
engine will open, in the process that opens it, before any other code touches the file. Three
rounds of live exploits each broke one of those conditions. Rev 6 answered with an engine-owned
`STRATEGY_RESOURCE_FIELDS` map plus a fail-closed lexical rule plus a later engine resource
contract, which is two systems for one boundary until its Slice D. This plan builds only the second
system: authorization at the open. It needs no field list, because a field the engine adds tomorrow
still has to open its file through the policy, and the sentry fails CI if it does not.

### D1. The engine `FileAccess` policy and its capabilities (engine, public, additive)

New public package `decoy_engine/fileaccess/` (each module under the 600-LOC cap):

```python
class FileRole(StrEnum):      # what the engine wants the file for
    SOURCE, SUBSET_INPUT, REFERENCE, CORPUS, SNAPSHOT, SECRET, VAULT,
    CONFIG, FIXED_WIDTH_LAYOUT, QUARANTINE_OUTPUT, TARGET_OUTPUT, SPILL_HINT,
    JOB_OUTPUT,                               # read back a job's own output (platform hooks)
    CUSTOM_PROVIDER_ROOT, MODEL_PACK_ROOT,    # directory-valued inputs
    LOG_OUTPUT                                # caller-configured log file (D8)

@dataclass(frozen=True)
class FileRequest:
    role: FileRole
    access: Literal["read", "stat", "read_dir", "write"]
    locator: str          # scheme already stripped by the engine (customer:, file:)
    field_key: str        # config position, e.g. "tables.t.columns.c.code_set.corpus_source"

class FileHandle(Protocol):   # an open, already-authorized regular file
    display: str              # what error and log text may show (see D5)
    def fileno(self) -> int: ...
    def library_path(self) -> str: ...   # only adapters call this, see D3 step 8
    def stat(self) -> os.stat_result: ...
    def close(self) -> None: ...

class DirHandle(Protocol):    # an open, already-authorized directory (B1)
    display: str
    def entries(self) -> Iterator[DirEntry]: ...       # name + type, via the open dir fd
    def open_file(self, rel: str) -> FileHandle: ...   # openat2 relative to this fd
    def open_dir(self, rel: str) -> "DirHandle": ...   # same constraints
    def close(self) -> None: ...

class OutputTxn(Protocol):    # opaque, policy-issued stage capability (B1)
    # every `component` is validated by the transaction itself (D1, "Component confinement")
    def create_file(self, component: str) -> int: ...  # fd, O_CREAT|O_EXCL|O_NOFOLLOW in the stage dir
    def open_file(self, component: str) -> int: ...    # reopen a file this transaction created
    def create_dir(self, component: str) -> "OutputTxn": ...  # nested stage dir, same transaction
    def remove(self, component: str) -> None: ...      # unlinkat within the stage dir only
    def rename(self, src: str, dst: str) -> None: ...  # renameat within the stage dir only
    def publish(self) -> None: ...                     # atomic, mode fixed at begin()
    def abort(self) -> None: ...                       # idempotent; also on garbage collection
    # context manager: __exit__ aborts unless publish() succeeded

class OutputDir(Protocol):    # write capability: an output root fd plus one allowed leaf
    display: str
    def begin(self, kind: Literal["file", "dir"],
              mode: Literal["exclusive", "replace"]) -> OutputTxn: ...

class FileAccess(Protocol):
    def open_read(self, req: FileRequest) -> FileHandle: ...
    def open_dir(self, req: FileRequest) -> DirHandle: ...
    def stat(self, req: FileRequest) -> os.stat_result: ...
    def output_dir(self, req: FileRequest) -> OutputDir: ...
    def allows_subprocess(self) -> bool: ...

class FileAccessDenied(DecoyError): ...      # str() == f"file access denied: {encoded field_key}"
class FileAccessUnavailable(DecoyError): ... # no policy where one is required (D2)
```

Rules:
- **Directory capabilities.** `FileAccess` exposes directory capabilities only for the finite
  directory-valued resources in Appendix A: custom-provider roots, model-pack roots, and
  transactional output directories. A `DirHandle` may enumerate entries and open descendants only
  relative to its already-open directory fd, with the same `openat2` constraints as a root
  (`RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS`; regular files and, for
  `open_dir`, directories only). It never exposes its host pathname: there is no `path` attribute,
  `library_path()` exists only on `FileHandle`, and `display` follows D5. Input datasets stay
  file-valued; the policy never hands out a directory for a `SOURCE`, `REFERENCE`, `CORPUS` or
  `SNAPSHOT` read.
- **Opaque stage capability.** Staging returns an `OutputTxn` object, not a name. The caller never
  supplies, sees or reuses a stage pathname. The stage directory is created by the policy with a
  random name beneath the same output root fd (same filesystem, so publication is a rename), and the
  transaction holds its directory fd. A transaction is bound to the `OutputDir` that issued it and to
  that policy's principal; it cannot be serialized, and passing it to another `OutputDir` or using it
  after `publish`/`abort` raises. Across processes a transaction travels only as its directory fd
  over `SCM_RIGHTS`, never as a name.
- **Component confinement (round 2 B1).** `OutputTxn` itself validates every child name it is
  given, in one private function called at the top of every operation that takes a name
  (`create_file`, `open_file`, `create_dir`, `remove`, `rename` source and destination, and the
  final leaf at `publish`). Callers never validate on its behalf, and no operation reaches a syscall
  with an unvalidated name. A valid component is a `str` that is non-empty, is not `.` or `..`,
  contains no `/` (so no absolute path and no separator), contains no NUL, and is at most 255 bytes
  when encoded with `os.fsencode`. Anything else raises `FileAccessDenied` before any syscall. Every
  operation is then a single `dir_fd`-relative call on the stage directory fd with
  `O_NOFOLLOW`/`AT_SYMLINK_NOFOLLOW` (or `openat2` with `RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS` where a
  lookup is needed), so even a name that passed validation cannot follow a planted symlink. The same
  validator guards `DirHandle.open_file` and `open_dir` when the `rel` argument is one component;
  multi-component `rel` values are resolved only by `openat2` with `RESOLVE_BENEATH`.
- **Multi-file directory transactions (round 2 H2: per-sink semantics preserved).**
  `begin(kind="dir")` expresses the existing Parquet sink's staged directory of many files and its
  atomic directory rename (`execution/_transactional_sink.py` ~149). Publication semantics are
  preserved per existing sink, not unified. The current Parquet directory sink documents that
  `commit()` is one `os.replace` (POSIX `rename(2)`), which publishes into an absent or empty
  destination and fails closed with `ENOTEMPTY`/`EEXIST` on a non-empty one, leaving the occupant
  untouched and the stage intact for `abort()`. Its transaction therefore publishes with
  fd-relative `renameat2(stage_parent_fd, stage_name, dest_parent_fd, leaf, flags=0)`, which keeps
  exactly that rename behavior. `RENAME_EXCHANGE` and exchange-then-delete are not used anywhere.
  `RENAME_NOREPLACE` is used only for a sink whose characterization shows it already refuses an
  existing destination of any kind. `begin(kind="file")` keeps the rev 1 file path (`os.link` with
  `src_dir_fd`/`dst_dir_fd` for `exclusive`, `os.replace` with dir fds for `replace`). The
  `replace` mode is offered only to call sites whose E1-0 characterization proves they already
  replace today (for example the target file writers under the existing target lock); every other
  sink keeps its current mode, and the mode is fixed per sink in `fileaccess`, not chosen by the
  caller at publish time. E1 writes characterization tests of each sink's publish, failure and
  zero-record semantics before the port, and the new transaction must match them.
- **Delayed I/O retains its capability.** Any object or iterator that can perform I/O after the
  call that created it returns (writers such as `VaultWriter`, sinks, readers, lazy iterators such
  as the one returned by `run_mask_pipeline_chunked` at `execution/_chunked.py` ~358, callbacks)
  captures the specific `FileAccess` or narrower capability at construction and uses that reference
  for every later operation. Authorization covers iterator advancement, context-manager lifetime,
  callbacks, and final publish or abort, not only the initial call. The contextvar is a lookup for
  synchronous code inside a call; it is never how deferred code finds its authority.
- **Vault and unmask take capabilities.** `VaultWriter.write` accepts an `OutputDir` (a `str`
  target remains accepted only under `LocalFileAccess`); `unmask_pipeline` and `load_vault` gain
  `vault: FileHandle | None = None` keyword-only, and a `vault_path=` string under a strict policy
  raises `FileAccessUnavailable`.

Shipped implementations:
- `LocalFileAccess`: today's semantics (plain `open`, paths relative to CWD, symlinks followed).
  This is the CLI default. `display` is the path, so CLI error text stays as it is.
- `RootedFileAccess(roots, *, deny_inodes=())`: a generic deny-by-default primitive. Each root is
  `(name, dir_fd, roles, access)`. Linux: `openat2` with
  `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS`, always. There is no
  `follow_symlinks` parameter. Elsewhere: refuses to construct unless
  `allow_portable_fallback=True`, which uses `realpath` + `commonpath` and is documented as racy. The
  platform never sets that flag.
- `PreopenedFileAccess(grants)`: serves only requests matching a pre-opened `(role, field_key)` fd;
  everything else is denied. Used for engine-spawned children (D6).

The platform's policy (D7) implements the protocol and reuses `RootedFileAccess` for the kernel
part. Ownership, roles and admin roots stay in the platform (best-practices §3.3: the engine stays
narrow and caller-agnostic).

### D2. How the policy reaches every open (engine)

- Public entry points gain `file_access: FileAccess | None = None`, keyword-only (compatibility
  contract §4.1). The list is declared once as `decoy_engine.fileaccess.ENTRY_POINTS` and covers at
  least: `run_pipeline`, `run_pipeline_isolated`, `run_job_with_governor`, `run_mask_pipeline_chunked`,
  `run_mask_chunked`, `run_sequential`, the out-of-core runner, `profile_source`, `compile_plan`,
  `validate_plan`, `validate_config`, `run_config_only_checks`, `validate_joint_mask_config`,
  `estimate_job_capacity`, `vault_writer_for_config`, `load_vault`, `unmask_pipeline`, `run_subset*`,
  `run_discovery_sql`, `run_storm`, `read_fixed_width`, `load_table`, `apply_table_transforms`,
  `load_custom_providers`. The builder confirms the full list from `__all__` and the platform's
  import sites on the rebased engine (Slices, step 3); a sentry fails on any public callable that
  reaches a `fileaccess` call without being in the list.
- An entry point installs the policy in a `contextvars.ContextVar` for the duration of the call.
  Internal code obtains it only via `fileaccess.current()`, which raises `FileAccessUnavailable`
  when nothing is installed. Internal code never has a default, so it cannot "forget" in a way that
  falls open.
- **Lazy results.** An entry point whose result can do I/O later (a generator, an iterator, an
  object with deferred I/O methods) passes the policy it resolved into that result explicitly
  (D1, "Delayed I/O retains its capability"). For generators the wrapper is
  `fileaccess.bind_iterator(policy, it)`, which runs every `__next__`, `send`, `throw` and `close`
  of the underlying generator inside a context in which that policy is installed, so consumption
  after the entry point returned, interleaved consumption of two iterators bound to different
  policies, and consumption while another policy is installed all use the policy the iterator was
  created under. A sentry flags any `ENTRY_POINTS` member that returns a generator or iterator
  without going through `bind_iterator`.
- **No downgrade.** If an entry point is called with `file_access=None` while a policy is already
  installed (an engine entry point calling another), it keeps the installed one. It installs
  `LocalFileAccess` only when nothing is installed and `forbid_ambient()` has not been called.
- **Threads.** Contextvars do not cross into `ThreadPoolExecutor` workers. Every engine thread pool
  submits through `fileaccess.run_in_context(fn)`, which calls `contextvars.copy_context()` once
  per submitted task and runs the task in that fresh copy. A single `Context` object is never
  shared by concurrent tasks (`Context.run` raises `RuntimeError` on concurrent entry, and a shared
  context would also let one task's `ContextVar.set` leak into another). A sentry flags
  `executor.submit`/`map` and `threading.Thread(target=...)` in engine source that do not go
  through it. A thread that slips through fails closed (`FileAccessUnavailable`), never open.
- **Processes.** See D6.

### D3. One resolution order, one canonical form

For every config-derived locator:

1. **Templates first, in the platform.** `api/jobs/variables.resolve_in_config` resolves `${var.*}`,
   `${env.*}`, `${trigger.*}`, `${storm.*}`, `${iteration.value}` and whole-mapping tokens, for read
   locators and for templated write locators (`quarantine.output_path`, `targets.<n>.path`). The
   engine never resolves templates.
2. **Unresolved means denied.** The platform policy denies any locator containing `${`. Platform
   pre-run code (admission, claim) that sees an unresolved value does not stat it and prices it as
   unknown size (the existing conservative branch), which fixes the rev-6 P-5 fail-closed regression.
3. **Scheme stripping in the engine.** The engine knows its schemes (`customer:` for reference and
   corpus, `file:` for secrets) and strips them before building the `FileRequest`. The policy sees a
   bare locator plus a role. Bare `joint_mask.reference` names never become requests: they must be
   in the shipped registry (D8) or validation fails.
4. **Lexical pre-filter in the policy** (Werkzeug-style, never the gate): reject non-`str`, empty,
   NUL, length over 4096, and for the uploads form anything other than exactly
   `uploads/<stored_name>` with `stored_name` matching the server's generated-name pattern
   (lowercase hex plus extension; the builder pins the regex from `api/files` upload code). This
   also keeps `uploads/vaults/...` and `uploads/_pending/...` out of the uploads form. No string
   normalization is applied and then trusted.
5. **Root mapping.** The policy picks the root by locator form and role, never by probing the
   filesystem: `uploads/<n>` maps to the upload root; the reserved `staging:<kind>:<id>/<component>`
   form maps only to a live `StagingGrant` held by this policy instance (D7), never to the staging
   or pending root; job outputs map to the output root; vault
   locators are never config-supplied (the platform builds them from a job id, D7); any other form
   maps to an operator-set server-file root (CD-1) only for admins; everything else is denied
   without a syscall.
6. **Kernel resolution.** `openat2(root_fd, relative, O_RDONLY|O_CLOEXEC|O_NOCTTY|O_NONBLOCK,
   RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS|RESOLVE_NO_MAGICLINKS)`. `fstat`: must be `S_ISREG` (or
   `S_ISDIR` for a directory role); the `(st_dev, st_ino)` must not be in the deny set (master key
   file, every entry of the key directory, the database file for SQLite installs). Then clear
   `O_NONBLOCK`.
7. **Ownership.** For uploads the single readability predicate (D7) runs **before** step 6, so a
   foreign or missing upload is denied without touching the filesystem.
8. **Use the descriptor, through an adapter.** The engine reads through the `FileHandle`, and only
   named adapter modules (D9) pass it to a library. Libraries that accept a file object (pyarrow
   `pq.read_table`, `ParquetFile`, `pa.OSFile` via fd, `csv` readers) get the file object. Libraries
   that need a path (polars `scan_parquet`, DuckDB `read_parquet`) get `library_path()`, which for
   strict policies is `/proc/self/fd/<n>`: a reopen of the same inode with no further name
   resolution. This is TOCTOU-safe only while the original fd stays open, so the handle stays open
   until the operation that uses the library path (for lazy scans, until the bound iterator or
   frame is closed) completes, and the per-process self-test (D7) proves that lifetime per library.

**Case.** The platform runs on Linux filesystems, which are case-sensitive. Decisions are made on
DB byte-equality of server-generated lowercase names and on `(st_dev, st_ino)` after open, never on
a case-folded string compare. Strict mode on a case-insensitive filesystem is not supported (K4).

**Symlinks.** Refused at every component under every strict policy, including operator-set
server-file roots. The rev 1 per-root symlink opt-in is removed: an operator who needs a symlinked
tree configures its real directory as the root. The platform creates upload, output and vault trees
itself and never needs symlinks.

### D4. TOCTOU

- Reads: one `openat2` call resolves and opens. Every later use is the fd or its `/proc/self/fd`
  link while the fd is open. The sentry forbids re-opening by path (only adapters see
  `library_path()`, and nothing outside the exact adapter and capability-module lists and the
  item-specific registry reaches a sink; D9).
- Directory reads: `DirHandle.entries()` enumerates via its own fd (`os.scandir(fd)`), and every
  child is opened with `openat2` relative to that fd, so a rename of the directory's path after open
  does not redirect later child opens.
- Writes: an `OutputTxn` holds the stage directory fd. Files are created
  `O_CREAT|O_EXCL|O_NOFOLLOW` with `dir_fd`; subdirectories one component at a time with `mkdirat`
  + `openat2(O_DIRECTORY, RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS)` (the pattern already in
  `api/jobs/staging_fs.py`); publication is one `renameat2` or `linkat` between dir fds (D1).
- Cross-process: the flag-on execve worker already receives source fds over `SCM_RIGHTS`
  (`api/jobs/source_fd_broker.py`, `dispatch_worker_main.bind_validated_source_fds`). Those are
  opened by the same platform policy at claim (P1), and the worker's own policy serves them from the
  received fds and opens any other role itself under a fresh principal (the worker has a DB session,
  `dispatch_worker_main.py` ~385).

### D5. Errors, logs and secrets never echo locators (denial hygiene)

- `FileAccessDenied` is the only denial type. Every denial is raised `from None`, so `__cause__` is
  `None` and `__suppress_context__` is true; the policy additionally clears `__context__` and
  constructs the exception outside any `except` block that holds a library error, so no chained
  object survives. The exception carries only the encoded field key.
- **Boundary scrubbing.** Before crossing a process, API, job-record, webhook or log boundary, one
  adapter (`api/file_access/denial.py` in the platform, `fileaccess/_errors.py` in the engine)
  replaces any exception whose chain contains a `FileAccessDenied`, or which originated inside a
  file adapter under a strict policy, with a freshly built error that has no cause, no context and
  no traceback, so no frame locals are reachable from it. Third-party exception text (pyarrow,
  polars, DuckDB, pandas, OS errors) never crosses for denials; for an authorized file it crosses
  only with paths replaced by `display`. Warnings raised inside adapters are captured
  (`warnings.catch_warnings(record=True)`) and re-emitted only as allowlisted engine warning
  categories with `display` substituted; anything else is dropped. A logging filter on every
  platform handler strips `exc_info` and `stack_info` from records whose exception chain contains a
  denial.
- **Encoding.** The field key in messages and telemetry is built from config keys, which are
  user-controlled (table and column names). It is encoded before use: non-printable and control
  characters (including newline, carriage return and ESC) are backslash-escaped, the result is
  bounded to 256 characters with an explicit truncation marker, and it is always emitted as a JSON
  string value or an escaped log field, never interpolated raw (CWE-117).
- **Public shape.** The platform maps a denial to HTTP 403 with body
  `{"detail": "file access denied", "field": "<encoded field key>"}` on every route, and writes the
  same text to the job log and job row. By-id references (`LocalRef`, STORM `file_id`, workflow
  `source_file_id`, analytics `file_id`) return the same 404 for missing and not-readable, through
  the owned-upload lookup harvested in P1-8.
- Under a strict policy, `FileHandle.display`, `DirHandle.display` and `OutputDir.display` are the
  encoded field key. Every engine message that names a file today (for example `load_table`'s
  `failed to read Parquet at {path}`, `_check_version`'s warning, `_codeset_loader`, `keyprovider`)
  switches to `display`. Under `LocalFileAccess`, `display` is the path, so CLI text is unchanged.
- Content-derived messages (missing `id` column, column lists from a joint_mask reference) may appear
  only for files the principal was allowed to read, which the gate now guarantees.
- **Security telemetry.** The platform policy logs one INFO record per denial with bounded,
  allowlisted fields only: principal id (int), job id (int or null), encoded field key, role (enum),
  and a reason code from a closed set (`not_owned`, `not_found`, `outside_root`, `not_regular`,
  `unresolved`, `denylisted`, `bad_form`). It never records the denied path, basename,
  environment-derived value, URI query or raw control characters. The reason code never reaches the
  requester. The builder confirms `/api/server-logs` is admin-only (K7).
- `${env.*}` hygiene: `api/jobs/variables.py` and every job-log writer emit token names, never
  resolved values. Enforced by the sentinel-secret tests (AT-P6).

### D6. Engine subprocesses

`run_pipeline_isolated` and `_isolated_worker` start fresh interpreters that cannot inherit a
contextvar. Rule: if the installed policy's `allows_subprocess()` is false (every strict policy),
the parent opens each input through the policy, passes the fds with `pass_fds`, and the child
installs `PreopenedFileAccess` over them; outputs come back through the parent's `OutputTxn` (its
stage directory fd passed the same way). If the builder finds a path in this route that cannot be
expressed that way, the route raises `FileAccessUnavailable` under strict policy and the platform
does not use it (today it calls it only from the probe router, `api/jobs/disk_quota_matrix.py`; the
builder confirms).

### D7. The platform policy (`api/file_access/`)

- **Principal.** `PrincipalFileAccess(principal, *, roots, deny_inodes, db_session_factory)`
  implements `FileAccess`. The principal is re-read from the database (`populate_existing()`) when
  the policy is constructed, and the platform constructs a fresh policy at admission, at claim and at
  run start (and at iteration-child spawn), so a role change, deactivation or ownership change
  between admission and run is honored (CD-3). Inactive or missing principal: a policy that denies
  everything. Webhook and trigger jobs are judged as the pipeline's current owner, re-read at each of
  those points, not the webhook creator and not an admission-time snapshot (CD-3). validate-yaml,
  preview, nested preview, analytics and unmask use the caller.
- **Upload readability: one predicate (project-ready).** `api/file_access/upload_scope.py` defines
  the only rule for "may this principal read this upload", in one place, in two forms generated from
  one definition: `readable_uploads_clause(principal)` (a SQLAlchemy filter) and
  `upload_readable(principal, upload_id_or_stored_name)` (which runs a query using that clause, so
  there is no second Python-side rule). Every consumer uses it: policy step 7, the owned-upload 404
  lookup, the upload list (V25), analytics, STORM, fixed-width preview. Until S1B: admins read all
  active uploads; everyone else reads `UploadedFile.owner_id == principal.user_id`. When S1B lands,
  the clause gains one disjunct, "the upload belongs to a project the principal's group can access",
  built on `api/authz/projects.py::can_access_project` semantics (user -> group -> project through
  `Group`, `GroupMember`, `GroupProjectAccess`, active `Project`). Nothing else in the policy changes:
  roots, roles, the denial shape and the tests that assert "not readable means generic denial" stay.
  `UploadedFile` has no `project_id` today, so how an upload joins a project (its own column, or the
  project of the pipeline or job that references it) is S1B's decision; this plan pins only that the
  answer is expressed inside this one clause (K13).
- **Role table (non-admin).** `SOURCE`, `SUBSET_INPUT`, `REFERENCE`, `CORPUS`, `SNAPSHOT`,
  `FIXED_WIDTH_LAYOUT` read from readable uploads only, plus `SOURCE` and `FIXED_WIDTH_LAYOUT` reads
  of platform-generated files through a live `StagingGrant` this policy holds for this principal
  (next bullet); `SECRET` denied (CD-6); `QUARANTINE_OUTPUT`,
  `TARGET_OUTPUT` write beneath the output root (per owner after P3); `JOB_OUTPUT` read only of the
  principal's own job's published outputs (the persisted locator, beneath the output root);
  `SPILL_HINT` stat beneath the output root; `VAULT` write only to the platform-built leaf
  `job_<id>.dcyvault` of a job the principal owns, beneath the vault root; `CONFIG`,
  `CUSTOM_PROVIDER_ROOT`, `MODEL_PACK_ROOT`, `LOG_OUTPUT` denied (the platform passes config as a
  mapping, uses package-data model packs, takes custom providers from the operator-set
  `CUSTOM_PROVIDERS_DIR` root, and logs to its own handlers). Admin: the same, plus reads beneath
  `DECOY_SERVER_FILE_ROOTS` (CD-1), plus `VAULT` read for unmask (the route already requires
  `outputs.unmask` with `min_role="admin"`).
- **Platform staging (round 2 H1).** Some valid flows read files the platform itself generated,
  which are neither registered uploads nor admin roots: cloud sources downloaded into
  `<upload_dir>/staging/job_<id>` (`api/jobs/v2_cloud_staging._staging_dir_for`, `stage_cloud_sources`,
  `_stage_s3_source`, `_stage_gcs_source`; preview callers with no job row use
  `job_scratch-<uuid4>`), preview scratch files (`api/jobs/preview_sample.py` ~313 to ~347, the
  `scratch_dir` handed to `read_fixed_width`, and the preview child's scratch directory), and STORM
  chunked-upload pending directories `<upload_dir>/_pending/<upload_id>` (`api/files/chunked_router.py`,
  read at `api/storm/router.py` ~554). These are served by one platform-internal capability,
  `StagingGrant`, in `api/file_access/_staging.py`, never by a broad registry exception and never by
  making the staging or pending tree a read root for any config form.
  - *Binding.* A grant is minted only by platform code at the moment it creates the resource, and
    records `kind` (`cloud_staging`, `preview_scratch`, `storm_pending`), the owner principal id
    (job owner, preview caller, or the chunked upload's initiating user), and a server-minted
    identifier: the job id (or the `scratch-<uuid4>` token for job-less previews), the preview
    scratch `uuid4`, or the chunked `upload_id` (`uuid.uuid4().hex` minted at upload init and stored
    with its owner). The grant holds the directory fd it created (`mkdirat` under the operator-set
    staging or pending root fd, `O_DIRECTORY|O_NOFOLLOW`), and child names inside it pass the same
    component validator as `OutputTxn`.
  - *Binding and rehydration.* A grant is minted when platform code creates a resource. For
    `storm_pending` only, a later process may rehydrate a grant from authoritative server state,
    never from locator text or UUID possession alone. It loads the `StormScan` and associated `Job`
    using the internal job identity, re-reads the principal, requires
    `StormScan.chunked_upload_id == id`, requires the initiating owner, current job owner and
    principal id to agree, requires the applicable pending or streaming state, validates the
    server-minted upload-id grammar, and only then opens that one directory with `openat2` beneath
    the pending-root fd. Any failed check is denied before a pending-root syscall.
  - *Use.* The platform registers a grant on the specific `PrincipalFileAccess` instance for that job,
    preview or scan; it cannot be derived from config text. The rewritten local locator is a
    reserved form `staging:<kind>:<id>/<component>`; the policy serves it only if this instance
    holds a live grant of that kind and id whose owner equals the policy's principal, opening the
    component with `openat2` relative to the grant's directory fd. A config that spells the reserved
    form for another job, preview or upload finds no grant and gets the generic denial, without a
    syscall. A grant is never transferable between principals and never widens to the staging root.
  - *Retention.* A grant lives until the work that needs it is finished: for cloud staging, through
    the run and its post-success hooks (Quality, Synth, evidence and STORM post-mask may re-read the
    staged source); for preview scratch, until the preview response is sent or the child times out.
    Delayed readers keep the grant by reference (D1 retention rule). For these two kinds, on release
    the platform removes the tree fd-relative and closes the fd. For STORM pending: grant-fd lifetime
    and resource lifetime are separate. Closing a rehydrated `storm_pending` grant closes only that
    process's fd. Only upload finalization, cancellation, abandonment cleanup or the startup orphan
    sweep removes the pending tree. Cloud and preview lifecycle owners retain their existing
    remove-and-close behavior. The startup orphan sweep (`_recover_stuck_jobs_on_restart`, `api/main.py` ~1171) keeps
    its by-name cleanup as one registry entry whose construction rule is "staging root fd +
    `job_<int>` or `job_scratch-<hex>`".
  - *Fd transfer, flag-off scheduler mode.* The queue worker stages in-process, creates the grant in
    the same process, registers it on the job's policy, and runs the engine there; no path or fd
    leaves the process.
  - *Fd transfer, flag-on scheduler mode.* Today the flag-on path stages into a verified staging dir
    through its working fd (`dir_fd`, V1-7 S2). Whichever process stages mints the grant. When the
    claim/dispatch parent stages, it sends the grant's directory fd (and each staged file fd) to the
    execve dispatch worker over the existing `SCM_RIGHTS` source-fd broker together with
    `(kind, id, owner_id)` metadata; the worker re-reads the owner from the database, rejects the
    grant if the job's current owner differs (CD-3), and registers it on its own policy. When the
    worker stages itself, it mints the grant locally. Both modes are tested (AT-P14).
  - *Preview and STORM.* The preview parent mints preview and job-less cloud staging grants and
    serves the child through `BrokeredFileAccess`, so the child receives only fds. STORM routes mint
    or look up the pending grant with the caller as principal; `run_storm_job`, normally in a queue
    worker with no fd handoff from upload init, rehydrates the grant per "Binding and rehydration"
    with a fresh principal before using it.
- **Roots are operator config (Guarantee 8).** Roots (upload, output, vault, data tables,
  staging, pending, `CUSTOM_PROVIDERS_DIR`, `DECOY_SERVER_FILE_ROOTS`) are read from deploy env or config at process
  startup and opened once per process as directory fds (`O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`).
  `DECOY_SERVER_FILE_ROOTS` is empty by default. The master key file, the key directory and the
  database file are always in the deny set, even beneath a root. Startup refuses any configured
  server-file root that equals, contains, or is contained in the key directory, the master key file's
  directory, or the directory that holds the database file. No settings model, API route, MCP tool or
  admin page writes any of these; an admin settings page may show them read-only. A platform sentry
  (D9) fails if any request-handling module assigns to them.
- **Per-process `openat2` self-test (fail closed, scoped to what each process uses).** The builder
  enumerates production entry points from `pyproject` scripts, the Dockerfile and `api/*_main.py`,
  classifies each, and AT-P1 pins the list and the class:
  - *Full test* (steps 1 to 4 below) in every process that constructs `RootedFileAccess` or
    `PrincipalFileAccess` and uses the library adapters, with step 3 limited to the adapters that
    process actually loads (for example the API, queue worker and execve dispatch worker as the
    classification finds them).
  - *Rooted only* (steps 1, 2 and 4, no library handoff) in a process that constructs
    `RootedFileAccess` but loads no library adapter. It checks only `openat2` and confinement
    failures; `/proc` is not required for this class (K14).
  - *Fd handoff only* in a child that serves files only through `BrokeredFileAccess` or
    `PreopenedFileAccess` (for example the preview child): it checks that a received fd reads through
    `/proc/self/fd/<fd>` in each adapter it loads, including after the original name is unlinked, and
    refuses to start on a missing or unreadable `/proc/self/fd`. It does not probe `openat2`, because
    it never resolves a path.
  - *None* in a process that neither imports the engine nor constructs `RootedFileAccess`; it needs
    no engine policy probe and does not call `forbid_ambient()`.
  The full test, run before serving work:
  1. a confined `openat2` of a probe file beneath a private temp root, which must succeed;
  2. symlink, `..` escape, absolute and magic-link probes, each of which must fail with the
     expected errno (`ELOOP` or `EXDEV`);
  3. a `/proc/self/fd/<fd>` handoff to each supported library adapter (pyarrow, pandas, polars,
     DuckDB) while the owning fd is open, which must read the probe; and a check that the probe file
     unlinked after open is still read through the handoff (the lifetime the guarantee relies on);
  4. startup refusal on `ENOSYS`, `EPERM`, `EACCES` or any unexpected result in the steps the class
     runs, and, for the full and fd-handoff classes only, on a missing or unreadable `/proc/self/fd`,
     with an operator-facing message naming the failed step.
  There is no insecure platform fallback. The launcher's existing `probe_openat2_support` stays for
  its own purpose and is not treated as this check.
- **Preview broker.** `BrokeredFileAccess` for the bounded preview child, which has no DB: the child
  sends each `FileRequest` to the parent over the existing preview socket; the parent applies its
  `PrincipalFileAccess` and returns an fd over `SCM_RIGHTS` or the generic denial. No discovery pass
  is needed because the parent judges each request as it arrives (the broker pattern of Chromium's
  sandbox and systemd's fd passing).
- **Startup order.** `forbid_ambient()` (in every engine-importing process) and the applicable
  self-test class run first thing,
  next to the existing engine ownership gate in `api/engine_compat.py`. A startup check also refuses
  an engine whose `fileaccess.CONTRACT_VERSION` is not the one the platform was built against.

### D8. Engine source fixes that the policy alone does not express

- **Bare reference names** (V15): `load_table` accepts a bare name only if it is in one shipped
  registry (merge `transforms/joint_mask._KNOWN_TABLES` and `_loader._SHIPPED_VERSIONS` into
  `reference_tables/_registry.py`). Shipped files are package data and read through the
  registry-listed package-data reader. `customer:` is the only path form and always goes through the
  policy. Applies in all modes, including the CLI (CD-5).
- **`snapshot_file` everywhere** (V8, V18): `plan/_generation.read_and_pin_snapshots`,
  `generation/statistical/_spec._load_snapshot` and `generators/derivation._snapshot_content_digest`
  read through the policy with role `SNAPSHOT`. The digest is computed over the same bytes, so the
  determinism fingerprint does not change. No schema change in this program (CD-4).
- **DuckDB, reads and writes** (V22, H4). One adapter, `fileaccess/adapters/duckdb.py`, is the only
  module that creates DuckDB connections, and it has two kinds:
  - *Untrusted-SQL connections* (`run_discovery_sql`, the platform data-table query): in-memory
    only; created with `enable_external_access=false`, `autoinstall_known_extensions=false`,
    `autoload_known_extensions=false`, `allow_unsigned_extensions=false`, then
    `lock_configuration=true` after the permitted tables are registered as Arrow objects opened
    through the policy. User SQL is split with DuckDB's own statement parser
    (`extract_statements`) and must be exactly one statement of type `SELECT`; every other type is
    rejected before execution, which covers `COPY ... TO`, `EXPORT DATABASE`, `IMPORT DATABASE`,
    `ATTACH` and `DETACH` (read or write mode), `INSTALL`, `LOAD` (by name or by path), `SET`,
    `RESET`, `PRAGMA`, `CALL`, `CREATE SECRET`, `CREATE`/`INSERT` into file-backed objects, and
    multi-statement payloads. The locked configuration is the second layer: an attempt to re-enable
    external access, unlock configuration or load an extension fails even if the type check were
    bypassed. The regex denylist stays only for friendlier errors.
  - *Engine-internal connections* (out-of-core relations, Parquet scans): never execute
    caller-supplied SQL text; file arguments come only from `library_path()` of policy-issued
    handles or from `OutputTxn` fds; external access is enabled only on these.
  If Arrow scans fail with external access off, the build stops and reports; the fallback is
  loading the tables into DuckDB before locking.
- **Default log file (H3: preserved).** `internal/logging.get_logger()` keeps today's CLI behavior:
  with no strict policy installed and no `forbid_ambient()`, the default
  `config.get("file", "logs/decoy_engine.log")` rotating file handler under the CWD is unchanged.
  Under a strict policy the logger never opens a path: a `file` setting is ignored unless the caller
  supplies an `OutputDir` with role `LOG_OUTPUT`, and the platform supplies none (it logs through
  its own handlers). The rotating handler lives in `fileaccess/adapters/logging.py`.
- **Name safety (H3: accepted names preserved).** Logical names (table, source, target and column
  names) are decoupled from filesystem names. Staging uses opaque policy-generated names inside the
  `OutputTxn`; a logical name becomes a filesystem component only for the final published leaf, as
  one `dir_fd`-relative component that must be a single component (no `/`, no NUL, not `.` or `..`,
  at most 255 bytes encoded). Every name accepted by today's `_transactional_sink._validate_table_name`
  and by the other sites (`_isolated_run`, `_isolated_worker`, `_isolated_commit`,
  `subset/_materialize`, `out_of_core/_runner`, `out_of_core/_relation`, `native/_pool_quality`)
  stays accepted with the same output name. E1 writes a characterization test of today's accepted
  and rejected name sets before the port; a name accepted today that would now be refused is
  either kept (preferred) or listed as a compatibility change with a migration test and a CHANGELOG
  entry. The rev 1 ASCII / no-space / 128-character pattern is dropped.

### D9. The guard tests (the structural part)

The sentries are built on resolved def-use information (`beniget`, test-only), not on matching the
text of a call.

**Resolution.** Sentries resolve imports, re-exports (`from pkg import read_table` where `pkg`
re-exports `pyarrow.parquet.read_table`), assignment aliases (`reader = pq.read_table`),
bound-method aliases (`m = path_obj.read_bytes`), attribute chains, `functools.partial` of a sink,
`getattr(obj, "literal")`, `importlib.import_module("literal")`, `__import__("literal")`, and
`pathlib.Path` objects stored in a variable before `.open()`, `.stat()`, `.iterdir()` and the other
Appendix B methods. Unresolved dynamic dispatch whose target could be a security-sensitive sink
(`getattr` with a non-literal name on a module or object that has sink attributes, `importlib` with a
non-literal, a callable taken from a mapping whose values include a sink) is rejected, not assumed
safe.

**No package exemption (round 2 B2).** Being inside `decoy_engine/fileaccess/` or
`api/file_access/` grants nothing. A sink call passes the sentry only in one of three ways, each
named by exact module path in the sentry's configuration (`tests/sentry/file_access_modules.toml`,
`tests/policy/platform_file_access_modules.toml`), which follows the same ratchet as the registry:

1. **Named adapter modules (third-party and stdlib data/serialization file APIs only).** Exactly:
   engine `decoy_engine/fileaccess/adapters/arrow.py`, `pandas.py`, `polars.py`, `duckdb.py`,
   `serial.py` (json, yaml, pickle, joblib, numpy), `logging.py`, `archive.py`; platform
   `api/file_access/adapters/pandas.py`, `arrow.py`. Adapter functions accept only `FileHandle`,
   `DirHandle`, `OutputDir` or `OutputTxn` parameters; the only path values they may pass to a
   library are `handle.library_path()` or `handle.fileno()` of a parameter, or an fd from an
   `OutputTxn` parameter. The sentry checks the argument's def-use chain back to one of those
   expressions; any other argument (a `str` parameter, a module constant, an attribute of a request)
   fails, even inside an adapter.
2. **Capability implementation modules (raw fd-relative primitives only).** Exactly: engine
   `decoy_engine/fileaccess/_rooted.py` (`RootedFileAccess`, `DirHandle`), `_output.py`
   (`OutputDir`, `OutputTxn`, the component validator), `_preopened.py`, `_openat2.py` (the
   `openat2`/`renameat2` syscall wrappers); platform `api/file_access/_staging.py` (D7 staging
   grants) and `_selftest.py`. In these modules only `os.open`, `os.fstat`, `os.scandir`, `os.mkdir`,
   `os.unlink`, `os.rmdir`, `os.rename`, `os.replace`, `os.link`, `os.close` and the wrapper
   functions are allowed. Raw primitives are divided into two classes. Descriptor-only operations
   such as `os.fstat(fd)`, `os.scandir(fd)` and `os.close(fd)` pass only when the descriptor's
   def-use chain reaches a held root, directory, stage, transferred or self-test probe fd.
   Name-resolving operations pass only with every applicable non-`None` `dir_fd`, `src_dir_fd` or
   `dst_dir_fd` traced to such a held fd, and every name traced to the component validator, a
   policy-generated random name, or a relative path supplied to the `openat2` wrapper. `os.scandir`
   with a string argument fails under the descriptor-only rule. A name-resolving primitive without a
   dir fd, with an absolute literal, or with a name that traces to a caller or config value fails.
   Third-party sinks remain forbidden in capability implementation modules.
3. **Item-specific registry.** Every other call site, in any module including other files inside
   `fileaccess/` and `api/file_access/` (for example `fileaccess/_local.py`'s ambient CLI opens,
   the portable `realpath` fallback, platform root opening at startup in `api/file_access/_roots.py`,
   package data, engine temp, `/proc` and cgroup), passes only with its own registry entry.

So legitimate code such as `adapters.arrow.read_table(handle)` passes, a direct
`pq.read_table(handle.library_path())` outside an adapter fails, a future `fileaccess/helpers.py`
calling `open(req.locator)` or `pq.read_table(path)` fails, and there is no "literal or module
constant" exemption and no "non-user expression" exemption:
`HOST_PATH = os.getenv("HOST_PATH"); pq.read_table(HOST_PATH)` fails.

**Reviewed registry, item by item.** Any unavoidable ambient path (package data, engine-created temp
directories, `/proc` and cgroup files, the operator-set `CUSTOM_PROVIDERS_DIR`) is listed
individually in `tests/sentry/file_access_registry.toml` (engine) or
`tests/policy/platform_file_io_registry.toml` (platform). Each entry names the module, the qualified
function, the sink, the construction rule of the path (for example "`importlib.resources.files
(__package__) / <literal>`", "`tempfile.mkdtemp()` result held in a local and never returned"), the
owner (a person or team), and a one-line reason. Entries match one call site, not a module or a
pattern. The registry may shrink freely; growing it needs a reviewed reason line and the owner's
name (best-practices §5.1 ratchet). Caller-supplied and config-supplied paths are never registrable,
with one named exception: the opens inside `fileaccess/_local.py` (`LocalFileAccess`, today's CLI
semantics). Each of those sites is its own registry entry whose construction rule is "the
`FileRequest.locator` of a request served by `LocalFileAccess`", and they are registrable only
because `LocalFileAccess` is constructed solely in `fileaccess/_entry.py` (sentry), is never
installed after `forbid_ambient()` (AT-E2), and is never imported by `api/**` (platform sentry).

**Engine sink sentry** `tests/sentry/test_file_access_boundary.py` scans every production module
under `src/decoy_engine/`, including `fileaccess/` itself. It flags every resolved reach of an
Appendix B sink that is not allowed by rule 1 or 2 above and is not an exact registry entry. It also flags: thread
pools and `threading.Thread` outside `fileaccess.run_in_context` (D2); entry points returning
iterators outside `bind_iterator` (D2); `subprocess`/`multiprocessing` launches outside the D6 route;
any construction of `LocalFileAccess` outside `fileaccess/_entry.py`; any DuckDB connection outside
the DuckDB adapter.

**Engine entry sentry** `tests/sentry/test_file_access_entry_points.py`: every public callable whose
call graph (static, within the package) reaches `fileaccess.current()` is in `ENTRY_POINTS` and
accepts `file_access=`.

**Platform engine-call sentry** `tests/policy/test_engine_file_access_calls.py`: every call in
`api/**` to a name in `decoy_engine.fileaccess.ENTRY_POINTS` (resolved through aliases and
re-exports as above) passes `file_access=` with a non-`None` value; `api/**` never imports
`LocalFileAccess`.

**Platform I/O sentry** `tests/policy/test_platform_file_io.py` scans every production module under
`api/`, including modules loaded only by child processes (`preview_child`, `dispatch_worker_main`,
`launcher_main`, `worker_main`, `scheduler_main`, `mcp_server`), with the same resolution and sink
registry, including `api/file_access/` itself. Calls pass only under rule 1 or 2 above (platform
lists) or via an item-specific platform registry entry. Also fails on `str(...).startswith(str(root))` containment anywhere in `api/**` (V23
shape), and on any assignment to an operator root setting outside startup config (Guarantee 8).
Rev 1's probe counted about 150 non-constant primitive calls in a subset of `api/`; the first commit
of P1 reruns the scan over all of `api/` and classifies every hit (adapter, registry entry, or
converted), and the build record lists the classification.

**Fixtures.** Each sentry ships negative fixtures for every bypass form above (direct call, `import
as`, `from import as`, re-export through a package `__init__`, assignment alias, bound-method alias,
`functools.partial`, literal and non-literal `getattr`, `importlib`, `__import__`, `Path` in a
variable, module constant from `os.getenv`, a call inside a lambda, a sink reached through a
mapping), plus privileged-package fixtures: a direct sink (`open(req.locator)`,
`pq.read_table(path)`, `os.stat(path)`) in a new non-adapter module inside
`decoy_engine/fileaccess/` and inside `api/file_access/`; a third-party sink inside a capability
implementation module; a raw primitive without `dir_fd` and one whose name traces to a caller value
inside a capability implementation module; `os.scandir(req.locator)` and descriptor-only calls
(`os.fstat`, `os.scandir`, `os.close`) whose descriptor argument traces from a caller-controlled
integer, inside a capability implementation module; and an adapter function passing a `str`
parameter to a library. Positive fixtures cover each approved adapter, each capability-module rule
(including `os.fstat(held_fd)`, `os.scandir(held_dir_fd)` and `os.close(held_fd)`) and each registry
category.

These sentries are the reason the class stays closed: a new field, helper, route or pre-run touch
that opens a file has to go through the policy, or CI fails before review.

### D10. Runtime detector for tests

Reuse rev 6's `tests/support/sentinel_strace.py` design, with the trace set expanded:
`strace -f -qq -s 65535 -yy -e trace=open,openat,openat2,creat,stat,lstat,fstat,newfstatat,statx,access,faccessat,faccessat2,readlink,readlinkat,getdents64,mkdir,mkdirat,rename,renameat,renameat2,link,linkat,symlink,symlinkat,unlink,unlinkat,rmdir,truncate,ftruncate,chmod,fchmodat,utimensat,chdir,fchdir,execve,execveat,clone,clone3,fork,vfork,sendmsg,recvmsg,pidfd_getfd`.
`-f` follows every created process; `-yy` decodes fd paths, so a sentinel fd inherited across
`execve` or passed over `SCM_RIGHTS` is attributed. The sentinel's basename and parent directory
name each carry a fresh uuid4 token. The helper fails (never skips) if `strace` is missing or ptrace
is refused, and has a self-test covering in-process, preview-child and execve-worker opens,
`dir_fd`-relative opens, directory enumeration, `renameat2` publication, pyarrow, polars, pandas
and DuckDB reads and DuckDB writes.

**What a denial assertion means.** "No unauthorized access" means zero successful unauthorized
reads or writes of the sentinel: no syscall returns a descriptor to the sentinel that is then read,
mapped or written; no successful write, rename, link, unlink, truncate, chmod or directory listing
of the sentinel or its parent outside a granted root; no `/proc/self/fd` handoff of it. Denials the
policy decides without the kernel (bad form, unresolved, not readable per the upload predicate,
outside every root) must show zero syscalls mentioning the token. Denials that need a kernel answer
(symlink, non-regular, denylisted inode, missing inside a permitted root) may show only the policy's
own confined `openat2` and `fstat` from `fileaccess`, ending in `close` with no read. Control-path
syscalls that do not name the token (opening the upload root, the database) are not findings.

## Behavior changes (user-visible)

- Engine, all modes: bare `joint_mask.reference` names outside the shipped registry fail validation
  (previously: `FileNotFoundError`, or a traversal read). DuckDB discovery SQL can no longer reach
  files other than the registered tables, and accepts exactly one `SELECT`. No other CLI change:
  logging, output names and accepted names are preserved (Guarantee 7); any further difference the
  E1 characterization tests find is added here before E1 merges.
- Platform: non-admins can read only their own uploads through any config field (until S1B adds
  project sharing); admins can read host files only beneath operator-set roots (CD-1); webhook jobs
  run as the pipeline's current owner (CD-3); `mask_secret_ref: file:` is admin-only (CD-6); all file
  denials share one message; by-id file references answer 404 for both missing and not readable.
- CHANGELOG entries in both repos; `docs/reference/webhooks.md` notes CD-3; the operator deploy
  guide documents `DECOY_SERVER_FILE_ROOTS` and the startup self-test.

## Slices, order, and release pairing

Each slice: own branch, written-first acceptance tests with red-before recorded, dennis gate with
live reproduction of every V-row it claims, Codex FINAL on the diff, coverage + mutation on changed
units (`pre-merge-testing-sequence`), Cam merges. Nothing pushes or merges without Cam.

**Dependency graph** (ordering, not a schedule; prodsim is on hold until Cam is present):

```
A8p (platform, engine pin on the supported 0.7.x line)
  |
B5 (Rust Phase B: lands, or freezes its final engine/platform call-site contract)
  |
E1 (engine: rebase onto then-current main, rerun full sink inventory, ship named 0.9.0 artifact
  |  paired with its required Rust/native component)
  |
P1 (platform: rebase after A8p and B5, pin that exact 0.9.0 artifact)
  |
P3 (per-owner output roots)  --> S14 --> S15   (S14 and S15 are built on P3's final output layout)

P2 (platform-only) runs in parallel wherever it does not conflict; it must land before S14.
Final: rerun the scheduler verification matrix against the merged P1 and P3 paths.
```

1. A8p lands on the supported 0.7.x line.
2. B5 lands, or freezes its final engine and platform call-site contract (admission pricing for
   native chunk cost, worker budget). E1 and P1 audit and wire B5's final call sites, not obsolete
   ones.
3. E1 rebases onto the engine `main` that exists after step 2, reruns the complete sink inventory
   (the E1-4 sentry output replaces Appendix A as the authoritative list), and ships as a named
   0.9.0 artifact paired with its required Rust/native component (wheel and companion versions
   recorded together).
4. P1 rebases after A8p and B5 and pins that exact 0.9.0 artifact.
5. P2 may proceed independently where conflict-free.
6. P3 lands before S14, so S14 and S15 are built against the final output layout (CD-8).
7. The final scheduler matrix is rerun against the merged P1 and P3 paths, both flag states, and
   the results go in the build record.

### Rev 3.2 delta for E1 (2026-10-07, against engine main `2ed9eb4c`)

E1 rebases onto engine main as it stands after Phase C slices #200-#221. A Codex delta gate checked E1 against that main and returned BUILD-READY-AFTER-DELTAS. These deltas fold in without reopening the settled design. Each one is a requirement on E1, with the same force as the sections it extends.

1. **B6a hold-back spill** (HIGH; D1/D9, E1-2, Appendix A R8 and registry, AT-E3/E6). Inventory these as sinks:
   - `execution/_chunked_output_sink.py:429-462`: `_Spill.add`, `_make_directory`, `replay`, `close`. These cover Arrow IPC writes and reads, parent creation, temporary-directory creation, unlink and cleanup;
   - `execution/_transactional_sink.py:225`.

   `spill_parent` derives from the sink's target parent, so it is NOT an automatic engine-owned ambient-temp exemption. Under strict policy, E1 allocates and keeps capability-backed scratch on the output filesystem, and routes IPC through the Arrow adapter. Tests cover forced spill, replay, failure cleanup and denied spill destinations.
2. **B6b lazy readers and lifetime** (HIGH; D1-D4, R1, E1-2, AT-E3/E11). Cover `LazySource.footer_facts`, `column_null_counts`, and the `OpenedLazyBatches` returned by `open_batches` (`profile/_readers.py:145, 211, 242, 261`; `execution/_chunked_input.py:120, 321`).
   - Authorization comes before routing and footer reads, as well as before batch reads.
   - The authorized handle is kept through `OpenedLazyBatches` and `InputChunks` closure, including early failure and interrupted consumption.
   - Footer comparison, row-count checks and bounded buffering are preserved.
   - Policy-switch and rename-race tests extend to these objects, not only to returned generators.
3. **Publication lifecycle** (MEDIUM; D1, R8, E1-0, AT-E6). The publication reference is now `_transactional_sink.commit()` at `:265`, with the rename at `:291` (not "~149"). Characterize B6a auto-chunk and fully dispatched multi-table output (`_chunked_output_sink.py:114`, `_pipeline.py:683`):
   - commit is the final pipeline action;
   - failures abort the open publish session;
   - "no writes" is told apart from "a zero-row streamed table".

   The absent, empty and non-empty directory rename semantics stay as specified.
4. **Isolated execution** (HIGH; D5/D6, R14, AT-E7/E8).
   - **What changed:** every isolated job now builds lazy sources, and child sink publication produces staged output that the parent publishes in a separate step (`_isolated_worker.py:201-225`, `_isolated_run.py:505-511`, `_isolated_commit.py:89`).
   - **What E1 must cover:** carry capabilities through both stages, streamed and resident finalization, row-error staging and parent readback, plus the new `_result_from_envelope` failure cleanup.
   - **Keep #219:** OOM classification and cleanup stay as they are, and denial scrubbing still runs before envelope truncation.
   - **Fallback:** D6's fail-closed alternative stays if this route cannot carry capabilities.
5. **Import-time package read** (MEDIUM; D9/D10, registry, AT-E1/E3). Register the module-initialization read of the packaged `when_grammar.lark` (`expressions/_when_parser.py:30-32`), with its construction rule and owner. Registry entries may identify module-level call sites. Add a cold-import detector control. No new sink primitive is needed, because Appendix B covers `Path.read_text`.
6. **Current-main route corpus** (MEDIUM; AT-E3/E4/E9/E10 and compatibility evidence). The characterization and parity corpus exercises:
   - transform-bearing lazy-source materialization (`_pipeline_sources.py:143-173`);
   - B6b streamed input (`_chunked_input.py:376`) and B6a streamed output;
   - multi-table dispatch;
   - unified and chunked native `when`;
   - nullable exact-integer Faker sources (`_exact_int_faker.py:44`).

   It pins actual route evidence and current-main output and error behavior.
7. **Release baseline** (LOW). `pyproject.toml` declares 0.7.0, `decoy-engine-native/pyproject.toml` declares 0.1.0, and a local `v0.8.0rc1` tag exists. 0.9.0 remains the planned version, and the paired native artifact is recorded when released. The chunked entry anchor is now `execution/_chunked.py:368` (not ~358). **Tagging or publishing 0.9.0 is an outward action that needs Cam's explicit go.**

No other changed filesystem sites were found in DuckDB, reference tables, model packs, vault or native Rust code, and no new production basetemp surface.

### Rev 3.3 delta for E1 (2026-10-08, Opus; escalation after repeated gate failures on one defect class)

The E1 build has now failed review on the same defect class three times: third-party error text, chains or secret material escaping under a strict policy (dennis round 1 H1/H3, dennis round 2 M-1, Codex final round 1 findings 3 to 6). Each fix closed one site and left the next. Per the escalation rule, the design is corrected here before any further code. The Codex final round 1 also found two confinement holes (findings 1 and 2). This delta supersedes D5's "originated inside a file adapter" scoping and adds three requirements with the same force as D1 to D10.

**X1. The strict error boundary (replaces per-adapter scrubbing as the guarantee).**
- **One owner.** `fileaccess.strict_boundary(position)` is a context manager in `fileaccess/_errors.py`. Under a strict (non-ambient) policy, it wraps one whole policy-bearing OPERATION, from the first open through every downstream step that consumes the authorized content (read, decode, parse, convert, execute, fetch). Ending at acquisition is not enough. Under `LocalFileAccess` it is a no-op, so CLI behavior and text are unchanged.
- **What may leave it:**
  - (a) `FileAccessDenied`, `FileAccessUnavailable` and `FileAccessReadError`, unchanged;
  - (b) an allowlisted resource failure, rebuilt as a FRESH exception of a fixed type with a FIXED message from a closed table. The types are `MemoryError`, `pyarrow.ArrowMemoryError`, `duckdb.OutOfMemoryException`, and `OSError` with errno `ENOMEM`/`ENOSPC`/`EDQUOT` (errno kept, no filename). Each fixed message satisfies `is_memory_failure` (or the disk-full classifier) by construction. A text-matched shape, such as the Arrow `Wrapping ... failed` and OpenSSL markers, is recognized only to choose which fixed message to use. NO original text survives;
  - (c) every other exception, which becomes `FileAccessReadError(position)`.
- **No chain or context survives.** The replacement is raised AFTER the `except` block exits, so `__cause__`, `__context__` and `__suppress_context__` are clean. `traceback.format_exception` of the result renders no original text, and no attribute (`.object`, `.args`, `.filename`) of the original is reachable from it.
- **Outer wrappers must not undo it.** Under a strict policy, any engine `except Exception` that wraps a call reaching a boundary re-raises the classes in (a) and (b) unchanged; it does not rewrap them into `ValueError`, `PlanCompileError` or similar. Applies to `reference_tables/_loader.py:107`, `transforms/_codeset_loader.py:429`, and every site the inventory below finds.
- **Inventory first.** The builder lists every policy-bearing operation (every registry entry and every adapter call site), names its completion point, and places one boundary around acquisition through completion. Known sites:
  - keyprovider read plus `_decode_secret_material` (hex and base64);
  - `data_discovery` execute plus fetch;
  - the statistical snapshot parse (`generation/statistical/_spec.py:164`, `plan/_generation.py:141`);
  - reference-table and corpus loaders;
  - YAML, JSON and Parquet adapters;
  - the out-of-core stager.

  The inventory goes in the build record. A sentry asserts that every adapter call in `src/` sits lexically inside a `strict_boundary` or inside a function the inventory names as boundary-wrapped by its caller.
- **The per-adapter `guarded` stays** as an inner layer, but it is no longer the guarantee.

**X2. Opaque scratch identifiers (confinement for config-named scratch).** Every engine scratch path derived from a config value (table, column or edge name) uses an opaque per-run identifier instead:
- `t0000`, `t0001`, ... from the table order;
- `e0000`, ... for edges.

These are mapped in memory and never built from the name. This covers `out_of_core/_runner.py` (`joins/`, `relations/`, `staged/`), `out_of_core/_relation.py` relation filenames, the stream driver, and any other site the inventory finds. Every scratch path is built beneath the engine's own `mkdtemp` descriptor, and the registry classifies these sites as engine-generated only after this change.

**X3. Shipped-corpus registry.**
- `load_corpus(name, ...)` resolves a name to a file ONLY when `name` is a key of `_SHIPPED_CORPORA`, through a registry-owned constant path, and checks membership before any filesystem call.
- Any other name, including absolute paths, `..`, separators and stems of real files, raises the existing unknown-corpus error with no filesystem access.
- A customer corpus requires `path=` and policy authorization.
- The same rule applies to every other public loader that accepts a "shipped" name (the builder lists them).

**Acceptance tests (added to the E1 set; written first, red on head `cce9eb68`):**
- **AT-X1a (sentinel, per inventoried site).** Under a strict policy, plant a distinct sentinel in authorized content: a cell, a secret file (including valid UTF-8 with a non-ASCII char and a bad hex/base64 body), malformed JSON or YAML, a SQL cast failure, and a YAML tag carrying an OpenSSL marker string. For each, assert the sentinel is absent from `str(exc)`, from `traceback.format_exception(exc)`, from a `logging` handler's output with `exc_info=True`, and from every attribute reachable by walking `__cause__`/`__context__` (both must be `None`).
- **AT-X1b (classification survives).** Inject `MemoryError`, `ArrowMemoryError`, `duckdb.OutOfMemoryException`, ENOMEM, ENOSPC, EDQUOT, and the Arrow `Wrapping <cell> failed` shape at each inventoried site, including through the outer loaders. The error that reaches the engine's caller keeps the fixed class, and `is_memory_failure` (or the disk classifier) is true. The #219 OOM tests pass under a strict policy.
- **AT-X1c (ambient unchanged).** The same inputs under `LocalFileAccess` produce today's errors and text.
- **AT-X2.** Table names `/abs`, `../up`, `a/b`, `.`, `..` and a 300-character unicode name, through both out-of-core drivers under a strict policy. No filesystem call targets a path outside the engine scratch root (strace detector plus an intercepted-mkdir probe). Output equals the same job with plain names.
- **AT-X3.** `load_corpus` with an absolute stem of a real file, `../x`, `a/b`, or a shipped name with a suffix makes zero filesystem calls (detector) and raises unknown-corpus. Shipped names still load.

**Process.** This delta goes through the Codex plan gate. dennis and Codex then gate the built result. Findings 1 to 6 from Codex final round 1 are the acceptance criteria. The record maps each to its test.

### Slice E1: engine `fileaccess` (decoy-engine `feat/file-access-policy`, ships as 0.9.0)

- E1-0 Characterization tests written first, on the rebased base: transactional sink publish,
  failure and zero-record semantics; accepted and rejected output name sets; default CLI logging
  destination; error text for every Appendix A row under no policy.
- E1-1 `decoy_engine/fileaccess/` per D1, D2 (protocol, `FileHandle`, `DirHandle`, `OutputDir`,
  `OutputTxn`, three implementations, contextvar install, `bind_iterator`, `forbid_ambient`,
  `run_in_context`, `ENTRY_POINTS`, `CONTRACT_VERSION`, error scrubbing per D5) and the adapters
  (D9).
- E1-2 Port every Appendix A row through the policy: sources and profile readers
  (`profile/_readers.py`, `_fixed_width_reader.py`), subset (`_keys`, `_preflight`, `_materialize`),
  snapshots (three sites, D8), code_set corpus (`_codeset_loader`), reference tables (D8), mask
  secret (`keyprovider.resolve_mask_secret_ref`), vault (`load_vault`, `VaultWriter.write` onto an
  `OutputDir`, `unmask_pipeline(vault=)`), config file loading (`validation/_config._load_config`),
  out-of-core spill estimate (`_spill_estimate`, `_budget`), quarantine writers
  (`write_jsonl_staged`, `publish_staged_jsonl`, the full-frame writer) and target writers onto
  `OutputTxn` (file and multi-file directory), lazy chunked iterators via `bind_iterator`, isolated
  run (D6), DuckDB (D8), STORM model packs and custom providers via `DirHandle`, name hints.
- E1-3 D8 fixes (registry, snapshot reads, DuckDB adapter, logger, name decoupling).
- E1-4 Sentries (D9 engine half) with the registry populated item by item.
- E1-5 Release 0.9.0: CHANGELOG, the compatibility checklist below in the PR body, `docs/security/`
  page describing the policy contract for integrators, the paired native component version.

**Compatibility contract §9 checklist (evidence gates).** Every box starts unchecked. The builder
ticks a box in the E1 PR body only after the named evidence exists, and links it.
- [ ] Read the contract.
- [ ] Additive: one keyword-only `file_access=None` on entry points, `vault=` on vault and unmask
  entry points, new public package `decoy_engine.fileaccess`; no symbol removed or renamed.
  Evidence: public-API diff.
- [ ] No `name/vN` artifact shape changes (vault, distribution-snapshot unchanged; the vault reader
  receives bytes through a handle, same format). Evidence: artifact round-trip tests.
- [ ] No determinism baseline changes: snapshot digests hash the same bytes. Evidence: AT-E9.
- [ ] Vault read/write format untouched; only the open and write calls move. Evidence: vault
  cross-version corpus.
- [ ] No disguise edits. Evidence: diff.
- [ ] CLI logging destination and accepted output names unchanged. Evidence: AT-E19.
- [ ] Exactly two behavior changes go through §5 with CHANGELOG entries, not shims: bare reference
  names restricted to the registry; DuckDB external access off and single-`SELECT` only. Evidence:
  CHANGELOG diff, AT-V15, AT-V22.
- [ ] Cross-version compatibility corpus re-run. Evidence: run log.
- [ ] `run_mask_chunked` and `run_mask_pipeline_chunked` raise the same errors for the same rejected
  config before any chunk beyond the first, pinned output types unchanged. Evidence: AT-E10.
- [ ] Passthrough handling untouched. Evidence: passthrough suite.

### Slice P2: platform-only surfaces (`fix/file-authz-platform-surfaces`, off main, no engine dep)

Carried from rev 6 with the same designs, because they are platform objects, not engine opens:
- P2-1 STORM (V24): `storm/router.py` create, batch tabs and rescan use the owned-upload 404
  lookup over the D7 predicate; the worker `run_storm_job` re-checks with a fresh job-owner
  principal before resolving the file.
- P2-2 Upload list (V25): filtered by `readable_uploads_clause` (D7).
- P2-3 Data tables: `DataTable.created_by` is the owner; list, get, delete and query registration
  filter by a fresh principal before any `Path.exists`, `unlink` or registration; null-owner legacy
  rows are admin-only; materialization is claim-then-write into
  `<data_tables_dir>/u_<owner_id>/<uuid4>.parquet` with a 409 `table_name_taken` on conflict and a
  visible `materialization_conflict` on STORM scans (rev 6 A-7 in full).
- P2-4 Legacy path-token file routes (V23): one `jail_open(root_fd, relative)` helper on `openat2`;
  readability checks before any `stat`; not-readable and missing both answer 404; definition list
  traverses only the caller's scoped pipeline dirs; output list DB-backed; delete duplicated routes
  where the job/output-id routes cover them and move every frontend caller
  (`web/src/pages/Files.tsx`, `web/src/studio/StudioFiles.tsx`,
  `web/src/pipelines/hifi/HiFiPipelineBuilder.tsx`, others found by grep).
- P2-5 Webhook ownership (V20, CD-3): webhook jobs owned by the pipeline's current owner, re-read at
  claim and run.
- P2-6 Delete the confirmed-dead ungated engine callers (`v2_preview.run_v2_pipeline_preview`,
  `run_v2_node_preview`, `v2_runner.inspect_v2_plan`, `v2_orchestration.run_v2_pipeline_plan_phase`,
  `yaml_inspect.resolve_source_file_paths_for_run`) and rewrite the tests that monkeypatch them
  (the FIX branch counted ~42 references in 5 files), pre-GA §8.1.

P2 amendment (2026-10-02, after the dennis gate on the P2 build, verdict `504ede279550`; Opus, applied after the plan-gate cap):
- P2-1b STORM read and delete scope. Every STORM list, detail, mutate and delete route (`list_scans`, `get_scan`, `delete_scan`, the `/runs*`, `/scans/{id}/diff`, `/reports*`, samples and top-values routes, and both ends of `source_scan_id` and diff references) filters by one SQL predicate: the scan's `Job.owner_id` through `StormScan.job_id`, the rule `retry_scan` already uses, falling back to `started_by` for legacy rows; rows with no owner are admin-only; foreign and missing both answer 404. One V24-style denial test per route.
- P2-5b Webhook owner re-read on every run path: one run-start gate in `job_principal`, called from `runner.run_job`, the flag-on execve worker (`dispatch_worker_main`) and iteration parents and children; tests for flag-on and iteration.
- P2-7 Interim containment of the DuckDB data-table query (live arbitrary file read through `read_text`/`glob`, pre-existing on main; the full fix is E1/D8). `POST /api/data-tables/query` is disabled unless the operator sets `DECOY_DATA_TABLE_SQL_ENABLED=true` in deploy env (operator setting, never an API or settings write, per Guarantee 8). Disabled answers 503 with code `data_table_sql_disabled` and a message naming the setting; the route, its tests and the UI stay, and enabling restores today's behavior. A test proves the `read_text` exploit is refused while disabled, and one that enabling restores the query. Removed when E1 ships the D8 DuckDB adapter.
- P2-8 Upload by-id routes (`api/files/router.py` download, preview, delete, metadata and the other by-id handlers) answer 404 for foreign and missing alike, through `readable_upload_or_404`; one test per route.
- P2-4b Output list: one row per output path (newest job), rows keyed `${job_id}:${node_id}` in the web client, and `limit`/`offset` with an explicit truncation flag instead of a silent 1,000-row cap.
- Definition routes: deleted rather than a scoped list (pre-GA §8.1, no caller anywhere).
- Engine release number (owner decision, Cam 2026-10-02): the E1 file-access slice ships as engine 0.9.0, not 0.8.0. 0.8.0 (and 0.8.0rc1) is the B3 dispatcher release cut from engine main `02dc2827`. Every reference in this plan that named 0.8.0 for E1 or its paired release (the Guarantee, slice ordering, E1, P1's pin, the gates) now reads 0.9.0; the A8p note `<0.8.0` at the top is the unrelated supported 0.7.x pin range and is unchanged.
- P2-9 Preview post-validation (M5, owner decision Cam 2026-10-02): previews report post-validation results but never fail on them. The bounded preview path (`run_bounded_preview` -> `preview_child` -> `run_v2_pipeline(enforce_post_validation=False)`) keeps `post_validation_enabled` as the instance sets it and forces enforcement off, and returns `post_validation: {enabled, enforce: false, failed_checks}` beside the rows. Real job runs keep honoring `post_validation_enforce`. Tests: a sample that hard-fails a check (FK parent outside the sample) completes and carries the results while a real job on the same data fails closed.
- P2 round 2 (dennis, `1000bd7f`): `${storm.*}` resolves only against scans the run principal may see (`ResolutionContext.principal_user_id`); `POST /api/pipelines` refuses a `storm_run_group_id` the caller cannot see and `storm-suggestions` reads through the scan predicate (M-A, M-B).

### Slice P1: platform policy + engine 0.9.0 (`fix/file-access-authz-v2` build branch, after E1 release)

- P1-1 `api/file_access/` per D7 (`PrincipalFileAccess`, `BrokeredFileAccess`, `upload_scope`,
  root config, deny inodes, per-process self-test, denial scrubbing and telemetry, adapters),
  ≤ 600 LOC per module.
- P1-2 Pass `file_access=` at every engine call: run (flag-off `run_job`, flag-on execve worker,
  sequential, chunked, out-of-core), validate-yaml, create (`_service.create_job_core`, including
  graph mode), bounded preview (broker), nested preview, iteration children, workflow runs, webhook
  runs, STORM, discovery SQL, fixed-width layout preview and cloud staging `read_fixed_width` callers,
  unmask.
- P1-3 Platform pre-run touches through the policy: `admission._cheap_file_size` and footer reads,
  `admission_fk`, `worker_budget.price_job_at_claim`, `scheduler_claim._local_source_identity`,
  `dispatch_plan.pin_source` (now an `open_read` returning the fd that is pinned), `preview_sample`,
  `check_v2_sources_exist`, `_io_discovery.find_matched_local_pair`, `read_local_sample`, and B5's
  final pricing call sites. Unresolved templates are never stat'd (D3 step 2).
- P1-4 Platform readers, writers and the vault lifecycle: every surface in Appendix A2, each
  converted with its denial test and success control. Vault handling is not described as done until
  both persistence and unmask are converted and tested.
- P1-5 `forbid_ambient()`, the per-process self-test and the contract-version check in every
  engine-importing process (D7); pin `decoy-engine` to the exact 0.9.0 artifact in `pyproject.toml`,
  the `api/engine_compat.py` interval, CI engine checkouts (immutable ref, SHA asserted) and the
  image's version assertion, the same way A8p does.
- P1-6 Denial mapping, scrubbing, encoding and telemetry (D5); `${env.*}` value scrubbing in
  `variables.py` and job-log writers.
- P1-7 Delete `restrict_external` in `v2_config.resolve_v2_source_paths` and the platform's own path
  resolution for engine-read fields; the platform passes locators as configured and the policy
  decides (one system, §8.2).
- P1-8 Platform sentries (D9 platform half), whole-`api/` scan classified in the first commit.
- P1-9 Harvest from `fix/arbitrary-file-read-write` @ `0c5285d0`, not merge: the `_jail_under` /
  `resolve_v2_quarantine_path` tests as regression cases, the `binding_resolve` owner check and the
  owned-upload lookup with identical 404 (rebuilt over the D7 predicate), the generic `_deny` text,
  and the 41 tests in `tests/test_source_file_authz.py` and `tests/test_quarantine_output_jail.py`
  re-pointed at the new policy (they must stay red on main). `api/jobs/source_authz.py` is not
  ported. Close the FIX branch and the rev-6 plan branch with a pointer to this plan after P1 merges.

### Slice P3: per-owner output roots (`feat/per-owner-output-roots`, after P1, before S14)

Rev 6 A-12 as written: `api/jobs/output_paths.py` owns `owner_output_root(owner_id)` and
`output_locator(job, raw)`; the policy's write roots become `output_dir/u_<owner_id>/`; every producer
and consumer migrates (target resolution, `target_locks`, stream and materialize writers, Quality,
STORM post-mask, Synth, `_io_discovery`, evidence hydration, output routes, `routes_evidence`), and
consumers prefer the persisted `published_locator`. Pre-existing flat outputs are admin-only, no
migration (§8.1). Closes cross-user overwrite and cross-job report reads; host writes are already
closed by P1. Lands before S14 so S14 and S15 build on the final layout (CD-8).

## Acceptance tests (written first; each records its red-before on the named base)

**Preamble.** Every test that asserts a denial also asserts, through the D10 detector, zero
successful unauthorized reads or writes of the sentinel as defined in D10 (zero token syscalls for
policy-decided denials; only the policy's own confined open and `fstat` for kernel-decided ones). It
asserts that the API body, job log and job row are equal to the missing-file case after
normalization: compare status, error code, error message, encoded field key and log line text,
excluding job ids, timestamps, durations and request ids. Surfaces that create no job row
(validate-yaml, analytics, unmask) compare the API body and server log only. None of those outputs
may contain the sentinel token, a path, a basename, a `stored_name` or a secret value.

**Live-exploit regressions (one direct regression per historical exploit; red on platform main
unless noted, green after P1/P2).** Each runs as a non-admin on every route where the field can
appear: API run flag-off, flag-on execve worker, iteration child, webhook, workflow, bounded preview,
node preview, nested preview, validate-yaml, create. Routes where a field cannot appear are listed
with the reason.

- AT-V1 absolute `sources.<n>.path` to a sentinel and to the master key file: denied.
- AT-V2 relative `../.decoy_master_key` and `x/../../sentinel` source paths: denied; no CWD-relative
  syscall.
- AT-V3 `uploads/<another user's stored_name>`: denied, identical to AT-V4 output.
- AT-V4 `uploads/<unregistered file placed in upload_dir>`: denied.
- AT-V5 `quarantine.output_path` absolute, `..`, symlink-escape, and pointing at the user's own
  upload: rejected; sentinel content and mtime unchanged; a relative path lands under the output
  root.
- AT-V6 code-set exfiltration: `code_set.corpus_source: customer:<sentinel>` with the sentinel
  carrying a marker row: denied, and the job output, preview body and logs contain no marker (the
  original exfiltration check).
- AT-V7 `joint_mask.reference: customer:<sentinel parquet>`: denied, no column names in any body.
- AT-V8 `snapshot_file: <sentinel>` on `type: statistical`: denied.
- AT-V9 `mask_secret_ref: file:<sentinel>`: denied for non-admin; admin beneath an operator root
  allowed.
- AT-V10 denial for a foreign upload vs a nonexistent path: equal normalized bodies, logs, rows; no
  `stored_name`. By-id `LocalRef` foreign vs missing: identical 404.
- AT-V11 a pipeline using `${env.SECRET}` with a sentinel value, run to success and to a file
  denial: the value appears in no job log line, job row field, API body or captured server log.
- AT-V12 validate-yaml with `customer:<host parquet>`: 403 body has no column names.
- AT-V13 claim and admission pricing: a queued job whose source is a host sentinel; admission
  (`_cheap_file_size`, footer read, `admission_fk`) and claim (`price_job_at_claim`,
  `_local_source_identity`, `pin_source`, B5's pricing sites) each show zero sentinel syscalls in
  their process; the job fails with the generic denial.
- AT-V14 iteration over `[<own upload>, <sentinel>]`: the sentinel child is denied before any stat
  and its host path is not persisted into `yaml_snapshot`; the own-upload child runs.
- AT-V15 bare joint-mask traversal: `joint_mask.reference: ../../../<tmp>/x` and `/abs/x` with no
  `customer:` prefix: engine validation error in CLI mode (E1 unit) and platform denial with zero
  sentinel syscalls; shipped names still work.
- AT-V16 validate-yaml pre-gate: `POST /api/pipelines/validate-yaml` with every Appendix A read row
  pointing at a sentinel: zero sentinel syscalls; own uploads validate including content checks.
- AT-V17 graph pre-gate: graph-mode `POST /api/jobs` with a `customer:` reference to a sentinel:
  zero sentinel syscalls during validation and create.
- AT-V18 snapshot type confusion: `snapshot_file` on `type: faker` (and every other non-statistical
  generate type), pointing at a sentinel and at `/dev/zero`: denied with zero syscalls on the
  sentinel and no read of `/dev/zero`; on an own upload it is read through the policy (fingerprint
  unchanged vs main).
- AT-V19 templated locators, reads and writes: each read row supplied via `${var.x}`,
  `${trigger.x}`, `${iteration.value}` and a whole-mapping `${var.srcs}` is denied at open time; and
  `quarantine.output_path` and `targets.<n>.path` supplied via the same template forms with host or
  `..` values are refused with no file created or modified outside the output root.
- AT-V20 webhook principal: an admin creates a webhook on a pipeline owned by a distinct non-admin
  user; the trigger carries a `${trigger.path}` host path in `corpus_source`: denied, judged as the
  non-admin owner (the same config run directly by the admin beneath an operator root would be
  allowed, proving the principal is the owner, not the creator).
- AT-V21 `nested` column whose child (depth 1 and 2) uses `joint_mask` / `code_set` with
  `customer:` host and foreign-upload values, literal and templated, on run routes and
  `/api/nested/preview`: denied. Positive: `redact_with: "file:removed"`, categorical
  `["customer:retail"]`, `group_key.prefix: "file:"` are accepted (no lexical false positives,
  because nothing walks strings any more).
- AT-V22 DuckDB reads and writes, engine unit and platform route: reads via `read_text`,
  `read_blob`, `glob`, `read_csv`, `read_json_auto`, `sniff_csv`, `parquet_metadata`, `FROM '/p'`,
  `JOIN '/p'`, `FROM x, '/p'`; writes and escapes via `COPY (SELECT 1) TO '<sentinel dir>/x'`,
  `EXPORT DATABASE '<dir>'`, `ATTACH '<path>'` (read-only and read-write, existing and new file),
  `INSTALL`, `LOAD 'name'`, `LOAD '<path>.duckdb_extension'`, `SET enable_external_access = true`,
  `RESET enable_external_access`, `SET lock_configuration = false`, `PRAGMA`, `CALL`, `CREATE
  SECRET`, and `SELECT 1; COPY ...` multi-statement: each rejected; the sentinel is unchanged, no
  file is created at any target path, and there are zero sentinel syscalls. User B cannot list, get,
  query or delete user A's table; concurrent same-name materialization has one winner (rev 6 AT-A9
  a-f).
- AT-V23 legacy file routes: B's token for A's output answers 404, identical to a nonexistent
  token, with zero syscalls under A's root; `outputs_backup/x` sibling refused; definition list causes
  zero syscalls under A's definitions.
- AT-V24 STORM foreign `file_id` on create, batch tabs, rescan: 404; a foreign `file_id` written
  directly into a scan row fails in `run_storm_job` with zero syscalls.
- AT-V25 B's upload list contains none of A's `stored_name`s.

**Engine (E1; red on engine main where the behavior exists).**
- AT-E1 Sentry bites: every D9 negative fixture fails the sentry (direct call; `import pyarrow.parquet
  as x`; re-export through a package `__init__`; `reader = pq.read_table; reader(cfg)`; bound-method
  alias; `functools.partial`; literal and non-literal `getattr`; `importlib.import_module`;
  `__import__`; `p = Path(cfg); p.stat()` and `.iterdir()`; `HOST = os.getenv(...)` module constant;
  `pq.read_table(handle.library_path())` outside an adapter; `os.link`, `os.rename`, `os.scandir`
  in an execution module; a DuckDB connection outside the adapter; a `ThreadPoolExecutor.submit`
  outside `run_in_context`; an entry point returning a generator without `bind_iterator`; a registry
  entry without owner or construction rule; `open(req.locator)` and `pq.read_table(path)` in a new
  non-adapter module `fileaccess/helpers.py`; a third-party sink in `fileaccess/_output.py`; an
  `os.open` without `dir_fd` and one with a caller-traced name in `fileaccess/_rooted.py`; an
  adapter passing a `str` parameter to pyarrow). Every positive fixture passes.
- AT-E2 Fail closed: with `forbid_ambient()` set, every `ENTRY_POINTS` member called without a policy
  raises `FileAccessUnavailable` before any syscall; an internal function called with nothing
  installed raises; a nested entry point call does not downgrade a strict policy to local.
- AT-E3 Recording policy: run a corpus covering every Appendix A row through every execution route
  (full-frame, chunked, sequential, out-of-core, native dispatch, subset, generate) with a
  `RecordingFileAccess` over a strace'd process; every sentinel-token syscall is attributable to a
  policy-issued capability (no direct opens).
- AT-E4 Ambient parity: the full engine suite and golden/determinism tests pass unchanged with no
  policy argument; output bytes identical between `LocalFileAccess` and a `RootedFileAccess` over
  the same files.
- AT-E5 `RootedFileAccess` and `DirHandle`: `..`, absolute, symlink at any component, magic link
  (`/proc/self/fd/N` inside the root), FIFO, directory where a file is required, device, and a rename
  race (swap the leaf for a symlink between two requests, 1000 iterations) are all denied or read the
  originally opened inode.
- AT-E6 Output transactions: mid-write failure leaves no final leaf; `exclusive` refuses an existing
  leaf; `replace` swaps atomically, and only at call sites whose E1-0 characterization shows they
  replace today; zero-record behavior matches the E1-0 characterization; stage fds closed; a
  multi-file Parquet directory publishes atomically (a reader sees no tree or the whole new tree,
  never a partial one) and abort removes the whole stage. Parquet directory sink publication against
  three destinations, each compared with the E1-0 characterization of `os.replace` on main: absent
  (publishes), empty directory (publishes), non-empty directory (fails closed with nothing
  published; every occupant file keeps its inode, bytes and mtime; the stage stays intact until
  `abort()`). No call path uses `RENAME_EXCHANGE` (sentry plus a strace check for the flag).
  Component confinement: on every name-taking operation (`create_file`, `open_file`, `create_dir`,
  `remove`, `rename` source and destination, final leaf), the names `/abs/x`, `/`, `..`, `.`,
  `../outside`, `a/b`, `a/../b`, the empty string, `a\0b`, and a 256-byte encoded name each raise
  `FileAccessDenied` before any syscall, and a snapshot of `(st_dev, st_ino, size, mtime)` for every
  inode outside the stage directory (including a sentinel beside the output root and the root
  itself) is unchanged; a symlink planted inside the stage under a valid name is not followed.
- AT-E7 Thread and subprocess propagation: chunked and native routes that use pools read through the
  policy; `run_pipeline_isolated` under a strict policy either runs on preopened fds or raises
  `FileAccessUnavailable`.
- AT-E8 Message hygiene: run the corpus under a strict policy whose locators carry a random token;
  capture exceptions, warnings and log records; the token appears nowhere.
- AT-E9 Determinism: the snapshot fingerprint and every golden baseline are identical before and
  after the port.
- AT-E10 Chunked parity: `run_mask_chunked` and `run_mask_pipeline_chunked` raise the same error for
  the same denied resource before any chunk beyond the first.
- AT-E11 Lazy iteration: an iterator from `run_mask_pipeline_chunked` is consumed after the entry
  point returned, after the contextvar was reset, while a different policy is installed, nested
  inside consumption of a second iterator bound to another policy, and concurrently from two threads;
  every chunk's I/O is judged by the policy the iterator was created under, and a denied chunk raises
  the generic denial.
- AT-E12 Directory capabilities: custom-provider root enumeration and child opens, model-pack
  directory access, a symlinked child, a `..` child, and a directory renamed after open; children
  are opened only beneath the original fd, and no host path appears in any attribute, `repr` or
  error.
- AT-E13 Delayed writers: a `VaultWriter` built under policy A writes after the entry point returned
  and while policy B is installed; the write goes through A's `OutputDir` or is denied by A.
- AT-E14 Stage capability misuse: a forged transaction object, a transaction reused after
  `publish` and after `abort`, a transaction passed to another `OutputDir`, and a transaction
  issued for owner A used under owner B's policy each raise without touching the filesystem. The
  same absolute, `..`, separator, empty, NUL and oversized component set from AT-E6 is attempted
  through a nested transaction from `create_dir` and through a transaction received over
  `SCM_RIGHTS` in a child process; each is refused centrally by the transaction and no inode outside
  the stage changes.
- AT-E15 Per-task contexts: `run_in_context` creates a fresh `copy_context()` for each of many
  concurrent submissions; a task that sets a contextvar does not affect a sibling, and no
  `RuntimeError` from concurrent `Context.run` occurs.
- AT-E16 Denial object hygiene: for every denial kind, the raised exception has `__cause__ is None`,
  `__suppress_context__` true, no `__context__` that mentions the token, and after boundary
  scrubbing no `__traceback__`; walking any surviving traceback's frames finds no local holding the
  token; `repr`, `str` and `warnings` output carry no token.
- AT-E17 Field-key encoding: a table and column name containing newline, carriage return, ESC and a
  4000-character name produce an escaped, bounded field key in the exception, log record and
  `display`.
- AT-E18 DuckDB adapter: the AT-V22 write and escape cases at the engine level, plus a check that
  engine-internal connections never receive caller SQL text.
- AT-E19 CLI compatibility: with no policy, the default log file is still created at
  `logs/decoy_engine.log` under the CWD as on main; every name in the E1-0 accepted set produces the
  same output file names as on main; every name in the rejected set is still rejected.

**Platform (P1).**
- AT-P1 `forbid_ambient()` is active in every engine-importing process (API, queue worker,
  scheduler, execve worker, preview child, launcher, any other entry point found), and each listed
  process runs the self-test class D7 assigns it (full, rooted only, fd handoff only, none);
  startup-order tests extended, never loosened; mirror A8p's ownership-gate order tests.
- AT-P2 Platform sentries bite: a scratch module calling `decoy_engine.run_pipeline(cfg)` without
  `file_access=`, including via an alias and a re-export; one importing `LocalFileAccess`; one adding
  `os.stat(descriptor["path"])` anywhere under `api/`, including a child-only module; one using
  `str(p).startswith(str(root))`; one assigning to an operator root setting from a request handler;
  a direct `open(path)` and `pd.read_csv(path)` in a new non-adapter module inside
  `api/file_access/`; a raw primitive without `dir_fd` in `api/file_access/_staging.py`.
- AT-P3 Operator roots (CD-1): admin read beneath a configured root succeeds; outside every root,
  the key file, the key directory and the database file are denied; a root equal to, containing or
  inside the key directory or the database directory refuses startup; no API route, MCP tool or
  settings write changes `DECOY_SERVER_FILE_ROOTS` (route inventory test); the settings view, if
  any, is read-only.
- AT-P4 Fresh principal at claim and run (CD-3): a queued job of a user demoted after create, a user
  deactivated after create, and a webhook job whose pipeline changed owner after admission are each
  judged with the current state at claim and again at run start.
- AT-P5 Broker: the preview child's requests are judged by the parent; a child request for an
  unrelated sentinel is denied with zero syscalls in the child and the parent.
- AT-P6 Secret sentinel across every log sink (job log, server log, row fields, API, webhook
  notification body) for success, denial and engine-error runs.
- AT-P7 fd hygiene: `/proc/self/fd` before and after success, failure, cancel and preview timeout
  shows no leaked descriptors (reuse `tests/support/source_fd_broker_attest.py`).
- AT-P8 Own-upload positives on every route and role: the user's own upload works as a source,
  reference, corpus and snapshot, with output containing its marker row.
- AT-P9 Mutation proof per wiring site added in P1-2 to P1-4: delete the `file_access=` argument (the
  sentry fails) and swap the principal for an admin principal (a V-row or Appendix A2 test fails).
- AT-P10 Per-process self-test: with `openat2` made to return `ENOSYS`, `EPERM` and `EACCES` (seccomp
  profile in a test container, or a syscall shim in unit tests), with `/proc` unmounted or
  `/proc/self/fd` unreadable, and with a library adapter that cannot read the handoff, each listed
  process refuses to start with the named failed step for its self-test class (full, rooted only,
  fd handoff only); no process falls back to a weaker policy. The unlinked-after-open probe proves
  the fd lifetime per adapter each process loads. A test asserts each process's class matches the
  AT-P1 list, so a process that starts building `RootedFileAccess` or loading an adapter without
  moving to the matching class fails.
- AT-P11 Platform surfaces: for every Appendix A2 row, the denial test and the success control it
  names.
- AT-P12 Boundary hygiene: for a denial on each route class (sync API, queued job, webhook-triggered
  job, preview child, analytics, unmask), inspect the HTTP response, persisted job row, webhook and
  notification bodies, server and job logs, captured warnings, the `repr` of any stored exception,
  `__cause__`, `__context__`, formatted traceback text and reachable frame locals: no token, path,
  basename, env value, URI query or raw control character.
- AT-P14 Platform staging grants: for each of cloud staging, preview scratch and STORM pending,
  the Appendix A2 positive and foreign-owner tests; cloud staging runs in both scheduler modes
  (flag-off in-process, flag-on with the grant's fds transferred to the execve worker), and the
  post-success hooks re-read the staged file through the retained grant; a grant transferred to a
  worker after the job's owner changed is rejected; a released grant cannot be used; no registry
  entry other than the orphan sweep names the staging or pending roots. Run STORM initialization,
  route use and queue-worker use in different processes and after API-process restart. The owner's
  streaming scan succeeds before upload finalization. A foreign principal, mismatched job owner,
  forged UUID and locator-only request are denied before any syscall beneath the pending directory.
- AT-P13 Upload predicate: `readable_uploads_clause` and `upload_readable` agree on a matrix of
  owner, non-owner, admin, inactive user and inactive upload; every consumer listed in D7 calls the
  predicate (sentry: no other `UploadedFile.owner_id` comparison in `api/` outside `upload_scope`).
  The S1B disjunct is out of scope here; S1B's own plan adds its acceptance rows to this matrix.

**Per-owner outputs (P3).** Rev 6 AT-A15 a-f as written (two users, same target, no overwrite;
Studio-shaped target runs; Quality/STORM/Synth/evidence read their own job's file; no write into
another owner's root; same-owner rerun replaces; owner-filtered output list renders on every page).

**Final.** The scheduler verification matrix rerun against merged P1 and P3, both flag states.

## Known issues

- K1 The sentries are static, and the directory-capability contract is general, not a
  custom-provider special case: any future directory-valued input must be added as a directory role
  with a `DirHandle`, never as a path. Dynamic dispatch that the resolver cannot resolve at a sink is
  rejected (D9), but dispatch through runtime data that never names a sink statically (a callable
  built from config, `eval`) remains possible in principle. Mitigation: AT-E3 runtime strace
  attribution over a corpus covering every Appendix A row, and the engine's existing
  expression-safety sentry bans `eval`/`exec` in engine source.
- K2 Native libraries (pyarrow, polars, DuckDB) open whatever path they are handed. Adapters hand
  them `/proc/self/fd/<n>` of an authorized inode. A library that follows an embedded path from file
  contents (for example a Parquet `_metadata` sidecar or a dataset directory scan) is out of scope;
  read inputs are never handed out as directories, and directory capabilities exist only for the
  Appendix A directory roles, whose children adapters open one at a time.
- K3 `/proc` must be mounted for `library_path()`. The per-process self-test (D7) refuses startup
  when it is not.
- K4 Strict policy on a case-insensitive filesystem is unsupported. Server-generated lowercase names
  and inode checks make aliasing require a pre-existing collision, which the server cannot create.
- K5 Cloud sources and targets (`credentials_ref`, `default`, `endpoint_url`) are a separate class
  (SSRF, ambient credentials). Tracked as FA-2, a named pre-GA follow-up (CD-7).
- K6 `mask_secret_ref: env:NAME` lets a user pick any worker env var as an HMAC key. Not a file read
  and not directly disclosing; tracked in FA-2.
- K7 The denial reason code lands in the server log. If `/api/server-logs` is reachable by
  non-admins, the reason code becomes an oracle; the builder confirms it is admin-only, or drops the
  reason code from INFO and keeps it at DEBUG.
- K8 `snapshot_file` on non-statistical columns stays accepted (read through the policy, meaningless
  to generation). A schema deprecation is a follow-up (CD-4).
- K9 Platform modules already over 600 LOC (`preview_parent.py`, `_service.py`, `worker_budget.py`,
  `v2_orchestrator.py`, `runner.py`) must not grow; P1 call sites shrink to one policy argument.
- K10 Timing differences between "denied without a syscall" and "denied after openat2" exist. Out of
  scope for a single-org box (threat model).
- K11 Unsupported third-party libraries: a library that cannot consume a descriptor-backed path or a
  file object cannot be used under a strict policy; its route raises `FileAccessUnavailable` and is
  documented as a deployment limitation rather than given a path.
- K12 The vault root sits inside the upload directory (`<upload_dir>/vaults`). The uploads-form
  lexical filter and the separate vault root fd keep it out of upload reads; moving it out of the
  upload tree is a later cleanup, not needed for the guarantee. Unmask stays admin-only and is not
  owner-scoped among admins (current behavior, kept).
- K13 **S1B wiring follow-up.** Platform RBAC is user -> group -> project (`Project`, `Group`,
  `GroupMember`, `GroupProjectAccess` on platform main), but enforcement step S1B is dormant:
  `api/authz/projects.py::can_access_project` is not wired into artifact routes, and uploads carry no
  project link. Until S1B, upload reads are owner-only (admins excepted). When S1B lands it adds one
  disjunct to `readable_uploads_clause` (D7) and decides how an upload joins a project; it must not
  add a second readability rule anywhere else. S1B's plan carries the acceptance rows for
  group-shared reads (shared upload readable, unshared denied with the same generic denial,
  archived project denied, removal from the group honored at claim and run).
- K14 Rooted-only self-test scope. A process in the rooted-only self-test class (D7) uses no library
  handoff, so its self-test checks only `openat2` and confinement failures and does not require
  `/proc`. `/proc` remains mandatory for the full and fd-handoff classes.
- Non-blocking note from the round-1 gate: the acceptance preamble now separates control-path
  syscalls from unauthorized data access (D10), and `copy_context()` is created per submitted task
  (D2).

## Gates

- **This plan:** Codex PLAN gate, per `review-rounds-discipline`: exhaustive first pass, 3-round cap,
  then Cam. Round 1: REVISE (2 BLOCKER, 6 HIGH), folded into rev 2. Round 2: REVISE (2
  BLOCKER, 2 HIGH), patched in rev 3. Round 3: 0 BLOCKER, 2 HIGH, 1 LOW; cap reached, Codex's
  patches applied as rev 3.1; build-ready pending owner acknowledgement. Convergence rule: a
  finding is either a defect in a pinned guarantee, design rule or acceptance test (fixed in the next
  rev) or a new inventory row (added to Appendix A or A2 and routed to E1, P1 or P2; it does not
  reopen the design, because the policy covers rows it has not seen).
- **E1:** dennis (including live reproduction of V6 to V9, V15, V18, V22 against the engine under a
  strict policy), then Codex FINAL, coverage + mutation on changed units, engine CI gate mirror
  (`ruff format --check`, sphinx `-W`, no-extras env), the evidence-gated compatibility checklist in
  the PR body, Cam merges and cuts the named 0.9.0 artifact with its native component.
- **P2:** dennis + Codex FINAL; live reproduction of V20, V22 (platform half), V23 to V25.
- **P1:** dennis + Codex FINAL with live reproduction of every V-row and every Appendix A2 denial,
  run against the pinned 0.9.0 artifact, on both scheduler flag states; the strace detector cells
  that need cgroup delegation run where the S8 attestation tests run, and the build record lists
  which cells ran.
- **P3:** dennis + Codex FINAL.
- **Program end:** the scheduler verification matrix rerun against merged P1 and P3.

## Cam decisions (recommendation first)

- **CD-1 Admin host paths (decided by Cam, 2026-10-02: stands as recommended).** Admins read host
  files only beneath operator-set `DECOY_SERVER_FILE_ROOTS`, empty by default. The principle: the
  server operator, not an in-app admin, decides what the process may reach on the host. Anything
  that widens host access (server file roots, key material, the database file) is set in deploy
  config or env at startup, never from the web UI or API; in-app admins manage users, groups,
  projects and connections only; the UI may show these settings read-only. The master key file, the
  key directory and the database file are always denied by inode, even beneath a root, and a root
  that contains them refuses startup. Symlinks are never followed beneath a root (the rev 1 opt-in
  is removed, per the round-1 gate). Why: the master key decrypts every stored connector credential;
  an admin pipeline that copies it into a masked output turns a web-session compromise into a
  host-secret leak through a downloadable artifact; and "admin of the app" is not "root of the box"
  on a self-hosted install.
- **CD-2 Fate of the FIX branch and rev 6.** Recommend: do not merge `fix/arbitrary-file-read-write`;
  harvest per P1-9, then close it and mark rev 6 superseded. Codex round 1 agrees: harvest is
  discovery only, never authorization.
- **CD-3 Webhook job principal.** Recommend: the pipeline owner (who controls the config), not the
  webhook creator, re-read from the database at claim and at run start (and at iteration-child
  spawn), never taken only from admission-time state. User-visible. Codex round 1 agrees with the
  re-read requirement.
- **CD-4 `snapshot_file` on non-statistical columns.** Recommend: keep accepted, read through the
  policy (no determinism or compatibility break now); deprecate in a later engine minor. Codex
  round 1 agrees with bounded, tracked compatibility reads.
- **CD-5 Bare reference names restricted to the shipped registry in the CLI too.** Recommend: yes.
  A bare name was never documented as a path; `customer:` is the path form. Ambient engine paths
  that remain are listed item by item in the reviewed registry, with no broad module-constant
  exemption (Codex round 1).
- **CD-6 Non-admin `mask_secret_ref: file:`.** Recommend: deny; non-admins use runtime secrets or
  `env:`. File-backed secrets are denied in engine and platform execution paths unless converted into
  an explicit non-file secret capability (Codex round 1).
- **CD-7 Cloud credential scope (K5, K6).** Recommend: a named, owned follow-up, **FA-2 cloud
  credential and env-secret scope**, required before GA, not in this program. Owner: assigned by Cam
  when this plan reaches GO (default: the same build owner as P1). Acceptance gate for FA-2, pinned
  now: a non-admin job may use only `ConnectionRef` connections the principal may use (through the
  same project predicate once S1B lands); `credentials_ref: default` and arbitrary `endpoint_url` are
  admin-only or operator-allowlisted; an `endpoint_url` to a link-local or metadata address is
  refused; `mask_secret_ref: env:NAME` accepts only names on an operator-set allowlist; each rule has
  a denial test with the generic denial and a success control; Codex FINAL GO. GA is blocked until
  FA-2 merges or Cam records an explicit exception.
- **CD-8 P3 timing.** Recommend (amended per Codex round 1): P3 lands before S14, not merely before
  S15, so S14 and S15 are built against the final output layout. This is dependency ordering only;
  prodsim is on hold until Cam is present.

---

## Appendix A: engine file sinks (decoy-engine, inventoried at `25710b5a`; E1 reruns on its rebased base)

Function-level, from a 2026-10-02 AST probe of non-constant filesystem calls (78 functions). The
E1-4 sentry run on the rebased engine produces the authoritative list; new rows found there are
routed per the convergence rule. "Policy" rows go through `FileAccess` or a capability it issued;
"registry" rows are engine-owned locations listed item by item.

| Row | Config or caller input | Sites | E1 disposition |
|---|---|---|---|
| R1 | `sources.<n>.path` (csv, parquet, fixed_width) | `profile/_readers.py` (`to_frame`, `to_table`, `schema`, `sample_frame`, `row_count`, `num_rows`, `open_batches`, `iter_batches`), `profile/_fixed_width_reader._parse_records`, `internal/fs.get_file_size`, `is_path_exists` | policy, `SOURCE`; lazy readers retain the handle |
| R1s | subset inputs | `subset/_keys.load_key_frames`, `_preflight.run_subset_preflight`, `_materialize.materialize_subset`, `_api.run_subset` | policy, `SUBSET_INPUT` / `TARGET_OUTPUT` |
| R2 | cloud `bucket`/`key` | `profile/_cloud_readers.*`, `connectors/sftp.*` | network; FA-2 (local staged copies read through the policy) |
| R3 | `snapshot_file` (any generate type) | `plan/_generation.read_and_pin_snapshots`, `generation/statistical/_spec._load_snapshot`, `generators/derivation._snapshot_content_digest` | policy, `SNAPSHOT` |
| R4 | `joint_mask.reference` | `reference_tables/_loader.load_table` | registry for bare names; policy, `REFERENCE`, for `customer:` |
| R5 | `code_set.corpus_source` `customer:` | `transforms/_codeset_loader` (`_resolve_read_path`, `_get_corpus_record`, `_read_corpus_record`) | policy, `CORPUS` |
| R6 | `global_settings.mask_secret_ref` `file:` | `keyprovider.resolve_mask_secret_ref` | policy, `SECRET` |
| R7 | `quarantine.output_path` | `quarantine.write_jsonl_staged`, `publish_staged_jsonl`, full-frame writer | `OutputTxn`, `QUARANTINE_OUTPUT` (`exclusive`) |
| R8 | `targets.<n>.path` | `_transactional_sink.write`, `write_batches`, staged directory publish (~149), stream writers, `out_of_core/_spill_estimate._output_shares_temp_filesystem`, `_budget` | `OutputTxn` (file or multi-file dir), `TARGET_OUTPUT` (`replace`) / `SPILL_HINT` |
| R8l | lazy chunked results | `execution/_chunked.run_mask_pipeline_chunked` iterator (~358) | `bind_iterator` with retained policy |
| R9 | names as filename components | `_isolated_run`, `_isolated_worker`, `_isolated_commit`, `subset/_materialize`, `out_of_core/_runner`, `_relation`, `native/_pool_quality` | logical/filesystem decoupling (D8), final leaf via `OutputTxn` |
| R10 | vault | `vault.load_vault`, `VaultWriter.write`, `unmask_pipeline(vault_path=)` | policy, `VAULT`: `FileHandle` for reads, `OutputDir` for writes, retained by the writer |
| R11 | discovery SQL text | `data_discovery.run_discovery_sql` | DuckDB adapter, untrusted-SQL connection (D8) |
| R11d | engine-internal DuckDB | out-of-core relations and scans | DuckDB adapter, engine-internal connection |
| R12 | config file path (CLI) | `validation/_config._load_config` | policy, `CONFIG` |
| R13 | `nested` child config | `_strategies/_nested` forwards to R4/R5 handlers | covered by R4/R5 at the open |
| R14 | isolated-run dirs | `_isolated_run.*`, `_isolated_worker.*` | D6 |
| R15 | model packs, name hints | `storm/model_pack/*`, `storm/name_hints/loader` | package data via registry; caller roots via `DirHandle`, `MODEL_PACK_ROOT` |
| R15c | custom providers | `internal/faker_setup.load_custom_providers` | `DirHandle`, `CUSTOM_PROVIDER_ROOT`; `CUSTOM_PROVIDERS_DIR` is an operator-set root (registry entry with owner) |
| R16 | log file | `internal/logging.get_logger` | CLI default unchanged; strict: `LOG_OUTPUT` via `OutputDir` or stderr (D8) |
| registry | engine temp, spill, `/proc`, cgroup | `_external_sort*`, `_batch_join`, `_stream_join`, `_route_policy`, `_transactional_sink._stage_dest`/`_cleanup_staging`, `_governor_monitor`, `_isolated_common.peak_rss_mb`, `out_of_core/_budget` cgroup reads | one registry entry per site with construction rule and owner |

## Appendix A2: platform surfaces converted in P1 (decoy-platform `e314b4ae`)

Each row names the resource role, the principal whose rights decide, when authorization happens,
whether the I/O is immediate or delayed, the capability handed to engine or library code, and the
tests. The whole-`api/` sentry scan (P1-8) adds any row this table misses.

| Surface | Code | Role | Principal | Authorized at | I/O | Capability passed | Denial test | Success control |
|---|---|---|---|---|---|---|---|---|
| Analytics profile and preview | `api/analytics/router.py::_load_file` (~233, via `profile_source`, `fw_preview`), `_read_fw_bounded_bytes` | `SOURCE` read | caller | request, before any stat | immediate | `FileHandle` via pandas adapter | foreign and missing `file_id` both 404 (today foreign is 403, an existence oracle), zero sentinel syscalls | own upload profiles and previews |
| STORM scan read | `api/storm/router.py` blob read (~711) | `SOURCE` read | scan owner (route: caller; worker: fresh owner) | request and again in `run_storm_job` | immediate (route), delayed (worker) | `FileHandle` via the upload predicate | AT-V24 cases | own upload scans |
| STORM pending | `api/files/chunked_router.py` pending root, `api/storm/router.py` (~554) | `SOURCE` read via `StagingGrant(storm_pending, upload_id)` | chunked upload's initiating user | grant minted at upload init; checked at route and again in `run_storm_job` | immediate (route), delayed (worker) | `FileHandle` opened relative to the grant's dir fd | user B's scan naming user A's `upload_id` (and a forged `staging:storm_pending:<id>/x` locator): generic denial, zero syscalls under A's pending dir | own chunked upload scans while `upload_state=streaming`, before finalize, in a separate queue-worker process and after API-process restart |
| STORM post-mask | `api/storm/post_mask_hook._read_frame_with_cap` | `JOB_OUTPUT` read | job owner, fresh | hook start | delayed (post-success) | `FileHandle` over the persisted output locator | a job whose target locator resolves outside the owner's outputs gets no report and no read | own job's report computed |
| Quality | `api/quality/runner_hook._try_compute_and_persist_quality_report` (`_resolve_path`, `_resolve_output_path`, `_read_frame_with_cap`) | `SOURCE` and `JOB_OUTPUT` read | job owner, fresh | hook start | delayed | `FileHandle`s | source pointing at a sentinel, output locator outside the owner's outputs: no read, no report | own job's report computed |
| Synth | `api/synth/runner_hook` (`_read_csv_with_cap` for source and output, ~164; output root resolve ~289) | `SOURCE` and `JOB_OUTPUT` read | job owner, fresh | hook start | delayed | `FileHandle`s via pandas adapter | same as Quality | own job's synth report |
| Evidence | `api/evidence/hashing.source_file_fingerprint`, `output_file_hash` | `SOURCE` and `JOB_OUTPUT` read (hashing) | job owner, fresh | manifest build | delayed | `FileHandle` (hash over the fd) | sentinel source or foreign output: no hash, generic denial recorded | own job's manifest hashes match main |
| Vault persistence | `api/jobs/v2_vault._persist_job_vault` (`vault_dir`, `job_vault_path`, mkdir ~86), engine `VaultWriter.write` | `VAULT` write | job owner | writer construction at run start; writer retains the capability until persist | delayed | `OutputDir` over the vault root, leaf `job_<id>.dcyvault`, `exclusive` | a writer for job A cannot write job B's leaf; an existing leaf is refused | vault written, unmask round-trips |
| Unmask | `api/unmask/router._resolve_job_vault_path` (~68), `unmask_pipeline(vault_path=)` (~149) | `VAULT` read | caller (admin, `outputs.unmask`) | request | immediate | `FileHandle` via `unmask_pipeline(vault=)`; a string path under strict policy raises | non-admin denied; string-path call raises `FileAccessUnavailable`; no path in any 422 body | admin unmask of a vaulted job round-trips |
| Fixed-width layout preview | `read_fixed_width` callers on uploads | `FIXED_WIDTH_LAYOUT`, `SOURCE` read | caller | request | immediate | `FileHandle` | sentinel and foreign upload denied | own layout previews |
| Cloud staging | `api/jobs/v2_cloud_staging` (`_staging_dir_for`, `stage_cloud_sources`, `_stage_s3_source`, `_stage_gcs_source`), callers in `v2_orchestrator`, `v2_runner`, `v2_preview` | `SOURCE` / `FIXED_WIDTH_LAYOUT` read via `StagingGrant(cloud_staging, job id or scratch token)` | job owner (fresh at claim and run), or preview caller | grant minted at staging; registered at claim/run | delayed (run and post-success hooks) | `FileHandle` relative to the grant's dir fd; fds over `SCM_RIGHTS` in flag-on mode | user B's job whose config names `staging:cloud_staging:<A's job id>/x` or A's literal staging path: generic denial, zero syscalls under A's staging dir; worker rejects a transferred grant whose owner no longer matches | own non-admin cloud job stages, runs and its hooks re-read the staged file, both scheduler modes |
| Preview scratch | `api/jobs/preview_sample.py` (~313 to ~347 `scratch_dir`), preview child scratch (`api/jobs/preview_child.py` ~182 to ~200) | `SOURCE` / `FIXED_WIDTH_LAYOUT` read via `StagingGrant(preview_scratch, uuid4)` | preview caller | grant minted when the parent creates scratch | immediate (released when the response is sent or the child times out) | fds through `BrokeredFileAccess`; the child never receives a path | user B's preview naming user A's scratch token, and a child request for a scratch name outside its grant: generic denial, zero syscalls in child and parent | own fixed-width and sample previews |
| Data-table query | data-tables route -> `run_discovery_sql` | registered tables read; SQL untrusted | caller | request | immediate | Arrow tables from `FileHandle`s, DuckDB adapter | AT-V22 | own table query |
| Profile distribution | `api/profile/distribution.get_categorical_distribution` | none (reads `scan.profile_json` from the DB) | caller | n/a | n/a | none | sentry keeps it file-free | existing tests |

## Appendix B: sink registry both sentries resolve

Python built-ins and stdlib: `open`, `io.open`, `os.open`, `os.stat`/`lstat`/`access`,
`os.path.exists`/`getsize`/`isfile`/`isdir`/`realpath`, `os.listdir`, `os.scandir`, `os.walk`,
`glob.glob`/`iglob`, `os.link`, `os.symlink`, `os.unlink`, `os.remove`, `os.rmdir`,
`os.rename`/`replace`, `os.makedirs`/`mkdir`, `os.readlink`, `os.truncate`, `os.chmod`, `os.utime`,
`os.chdir`; `Path.open`/`read_*`/`write_*`/`stat`/`lstat`/`exists`/`is_file`/`is_dir`/`mkdir`/
`resolve`/`iterdir`/`glob`/`rglob`/`unlink`/`rename`/`replace`/`touch`/`chmod`/`rmdir`/`walk`;
`shutil.*`; `tempfile.mkstemp`/`mkdtemp`/`NamedTemporaryFile`/`TemporaryDirectory` with a `dir=`;
`zipfile.ZipFile`, `tarfile.open`, `gzip.open`, `bz2.open`, `lzma.open`; `sqlite3.connect`,
`shelve.open`, `dbm.open`; `mmap.mmap`; `json.load`/`yaml.*load`/`pickle.load`/`joblib.load`/
`np.load`/`np.save` of an object not issued by the policy; `logging.FileHandler` and subclasses.

Data libraries: pandas `read_*` and `DataFrame.to_*` with a path or buffer; PyArrow
`pq.read_table`/`ParquetFile`/`read_metadata`/`read_schema`/`write_table`/`ParquetWriter`/
`ParquetDataset`, `pa.OSFile`, `pa.memory_map`, `pa.ipc.open_*`/`new_file`, `pyarrow.csv.*`,
`pyarrow.json.*`, `pyarrow.fs.*`, `pyarrow.dataset.dataset`/`write_dataset`; Polars
`scan_*`/`read_*`/`sink_*`/`write_*`; DuckDB `duckdb.connect`, `duckdb.sql`, `duckdb.query`,
`duckdb.execute`, `duckdb.read_*`, `duckdb.from_*`, and on connections `execute`, `executemany`,
`sql`, `query`, `read_*`, `from_*`, `register` with a path, `install_extension`, `load_extension`.

Cloud and network path locators: boto3 `download_file`/`upload_file`/`download_fileobj` with a
`Filename=`; `fsspec.open`/`filesystem().open`/`get`/`put`; `gcsfs`/`s3fs`/`adlfs` file systems;
`smart_open.open`; `paramiko` `SFTPClient.get`/`put`/`open`.

Process creation: `subprocess.*`, `os.system`, `os.popen`, `os.exec*`, `os.spawn*`,
`multiprocessing.Process`, `concurrent.futures.ProcessPoolExecutor`, where an argument, `cwd=`,
`env=` value or `pass_fds` can cause file access.

A call passes only when it is inside one of the exact named adapter modules and its path argument
traces to a capability parameter (rule 1), inside one of the exact capability implementation
modules as an fd-relative primitive under rule 2, or when that exact site is in the item-specific
reviewed registry (rule 3). Location inside `fileaccess/` or `api/file_access/` alone grants
nothing. There is no literal or module-constant exemption.
