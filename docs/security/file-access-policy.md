Status: current

# File access policy

The engine reads and writes files named by a config or a caller: sources, snapshots, reference
tables, corpora, secrets, vaults, quarantine sidecars, target directories. This page is the
contract for an integrator (the platform, a service, a test harness) that must decide which of
those files an engine call may touch. Plan and build record:
[plan](https://github.com/louiskeep/decoy-engine/blob/main/docs/plans/2026-10-02-file-access-authz-engine-platform.md),
[E1 build record](../records/2026-10-07-file-access-e1-build.md).

## The guarantee

With a policy passed in, every file the engine opens, stats, lists, creates, links, renames or
deletes on behalf of a caller or a config value goes through that one object, or through a
narrower capability the policy issued. There is no field list to keep complete: a field the
engine adds tomorrow still has to open its file through the policy, and the sentries
(`tests/sentry/test_file_access_boundary.py`) fail CI if it does not.

With no policy argument and no `forbid_ambient()`, behavior is unchanged from before the policy
existed: paths are relative to the CWD, symlinks are followed, error text and output names are the
same.

## Using it

```python
from decoy_engine import fileaccess, run_pipeline

fileaccess.forbid_ambient()          # once, at process startup, in every engine-importing process
result = run_pipeline(config, sources, engine_version=v, file_access=policy)
```

Every callable in `fileaccess.ENTRY_POINTS` takes a keyword-only `file_access=`. The call installs
that policy for its duration and keeps it on any iterator or object it returns, so a lazy chunk
iterator, a `LazySource`, a `ParquetTransactionalSink` or a `VaultWriter` is judged by the policy it
was created under, whatever is installed when it is used later. A nested engine call never downgrades
the installed policy, even by naming one: under an installed policy an explicit `file_access=` (or
`scope`/`capture` argument) is used only if it is that same object or the installed one is the ambient
CLI policy; any other raises `FileAccessUnavailable`. After `forbid_ambient()`, a call with no policy
raises `FileAccessUnavailable` instead of falling back to ambient access. Every such refusal is raised
with no cause or context. `forbid_ambient()` is one way for the process. So is the
first use of any non-ambient policy: from then on a context with no policy installed (a raw thread, a
callback) raises `FileAccessUnavailable` rather than reading through the ambient CLI policy. Work a
thread pool does for a call must be submitted with `fileaccess.run_in_context` or `map_in_context`.
`fileaccess.at_position("tables.orders.columns.zip")` names the config position that the reads in a
block belong to; the engine sets it where it walks a config, and a host that validates one column at
a time can wrap its call so a denial names the field.

## Requests and capabilities

A policy decides `FileRequest(role, access, locator, field_key)`:

- `role` says what the file is for (`SOURCE`, `REFERENCE`, `CORPUS`, `SNAPSHOT`, `SECRET`, `VAULT`,
  `CONFIG`, `QUARANTINE_OUTPUT`, `TARGET_OUTPUT`, and the directory roles `CUSTOM_PROVIDER_ROOT`,
  `MODEL_PACK_ROOT` and `DISGUISE_ROOT`).
- `locator` is the bare string from the config, scheme already stripped (`customer:` and `file:`).
- `field_key` is the config position, for example `sources.orders.path`. It is the only thing a
  denial names.

The policy answers with a capability, never a path: a `FileHandle` (an open, authorized regular
file), a `DirHandle` (an open directory; children open only beneath its own descriptor), or an
`OutputDir` whose `begin()` returns an `OutputTxn`, an opaque stage that publishes once or aborts.
Libraries receive a capability's descriptor path or descriptor, and only inside the adapter modules
under `decoy_engine/fileaccess/adapters/`.

Shipped policies:

| Policy | Use |
|---|---|
| `LocalFileAccess` | Today's ambient CLI behavior. Never installed after `forbid_ambient()`. |
| `RootedFileAccess` | Deny by default. Each root is a directory descriptor opened once; a request is served only if its role and access match a root, and it is opened with `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)`, so there is no check-then-open window and no symlink option. Linux 5.6 or later; elsewhere it refuses to construct. |
| `PreopenedFileAccess` | Serves only descriptors opened in advance, for a child process. |

A caller can implement the `FileAccess` protocol itself (the platform does, with ownership and role
rules) and reuse `RootedFileAccess.open_beneath` for the kernel part.

## Denials

One exception type, `FileAccessDenied`, with one message, `file access denied: <field key>`. It is
raised `from None`, carries no locator, path, basename, resolved value or file content, and the field
key is escaped and bounded to 256 characters. A missing file, a foreign file, an escape, a symlink, a
non-regular file and a refused role all produce the same message. A policy that says yes does not
turn a library failure into a denial: corrupt Parquet, malformed YAML or an undecodable text file on
an authorized strict handle raises `FileAccessReadError` (`file could not be read: <field key>`),
raised `from None` with no library text, so no third-party message names a path or a value. Memory
failures and `ENOSPC`/`EDQUOT`/`ENOMEM` keep their own class (an `OSError` is rebuilt without its
filename), so OOM classification and disk-quota handling still see them.
`fileaccess.scrub(exc)` turns any exception chain that holds a denial into a fresh one with nothing
attached, for use at a process or API boundary.

## What an error may and may not contain

Never: a file locator (the config value, an absolute path, a basename), the descriptor path a
library was handed (`/proc/self/fd/<n>`), the content of a file the policy denied, or secret
material. Allowed: text derived from the content of a file the principal was allowed to read (a
parse error quoting a cell, say). Cell values in logs are a separate program.

- Engine messages name a file by `display`, the encoded config position, wherever a strict policy
  is installed (`fileaccess.shown`). Third-party text that names the file it was handed goes
  through `fileaccess.redact_locators`.
- A `file:` mask secret is a `FileRole.SECRET` read at `global_settings.mask_secret_ref` through
  `fileaccess.read_checked`: the owner-only check (`secret_file_permissions_unsafe`) runs on `fstat`
  of the policy-issued descriptor and the bytes come from that descriptor. The F13 admission boundary
  rebuilds every failure from a fixed code and raises it with cause and context cleared, so a decode
  error holding the file never rides along, even when the caller was already handling one. Under a
  strict policy exactly `FileAccessDenied`, `FileAccessUnavailable`, `MemoryError` and
  `ENOMEM`/`ENOSPC`/`EDQUOT` from that read keep their class, as fresh value-free instances; nothing
  else does, and no other secret provider can pass them. `env:` and `file:` secrets are never cached,
  so a secret admitted under one policy is never served to a run under another.
- Translating, typed, stored-error and fallback handlers re-raise what callers classify by
  type (`fileaccess.keeps_class`): the policy errors, memory failures and `ENOSPC`/`EDQUOT`.
- Scratch paths built from a table, column or edge name use opaque per-run ids. A shipped name
  (corpus, reference table) resolves only through its registry, before any file-system call.

### Traceback frame locals: what is covered now

The claim is exactly the secret-material census, and no broader. `scripts/secret_material_census.py`
derives it from the AST of `src/decoy_engine` (no hand-picked list); the committed result is
`tests/sentry/secret_material_census.json`, and `tests/sentry/test_secret_material_census.py`
re-derives it and fails on a binding the census lacks, a stale entry, or a verdict the code does
not bear out. The census lists, in any `decoy_engine` function (`def`, `async def`, nested
`def`, lambda closures):

- parameters whose name matches `key|secret|master|signing|password|token|seed|ikm|prk|okm|`
  `material|root` and whose annotation is `bytes`, `bytearray` or `memoryview` (or which are
  unannotated);
- locals with such a name bound to an environment read, a `FileRole.SECRET` read, the contents of
  an `OpaqueBox`, a package function annotated to return bytes, a bytes operation, a bytes class
  attribute, or a slice, sum or alias of tracked material;
- closure cells over those names; every environment read of a name holding KEY, SECRET or TOKEN
  (or a computed name); every call passing `FileRole.SECRET`; every `OpaqueBox` holding tracked
  material; and every class attribute with such a name and a bytes annotation.

Each entry has a verdict. Re-checked from the AST: `wiped` (the body is a `try` whose
`except BaseException:` deletes the name and re-raises; a closure cell is wiped by its enclosing
function and the nested function never escapes it), `boxed` (`decoy_engine._opaque.OpaqueBox`,
unboxed only in the consuming call), `unbound` (consumed inside an expression), `hidden` (a
`repr=False` field or a class `__repr__`, and no raising `__post_init__`: a dataclass that
validates a secret field uses `_opaque.validate_after_init`) and `contained` (run only under
the admission boundary `_call_closed`). Reviewed, with the reason in the census: `no-raise` and
`not-secret`. Runtime cells: `tests/fileaccess/test_secret_census_frames.py` calls every census
function that takes material with a canary key and bad inputs and walks every `decoy_engine`
frame on every chain link; `tests/fileaccess/test_secret_census_dedicated.py` covers the entries
a bad argument alone cannot make raise.

Not covered, and not claimed:

- paths, refs and configs held in entry frames (deferred to F31 by owner decision);
- material under a name outside the pattern or without a bytes annotation (`Any`, a `str` that
  is not an environment read), including generic helpers that receive it under another name
  (`fileaccess.read_checked` clears its read buffer on failure by review, not by the census);
- frames outside `decoy_engine` (the standard library, `cryptography`, the native companion);
- values derived from material that are not bytes (an `int` seed from `int.from_bytes`);
- value-bearing data that is not key material (source cells, decrypted vault entries).

Until F31 revision 3 owns the general construction, leave locals capture off (see
`error-reporting-and-data-exposure.md`).

## What changes for the CLI

Exactly two behavior changes, both listed in the CHANGELOG:

1. A bare `joint_mask.reference` name must be one of the shipped tables. `customer:<path>` is the only
   path form. A bare name that is not shipped raises the same `FileNotFoundError` as before and never
   touches the file system.
2. Discovery SQL (`run_discovery_sql`) runs on a locked in-memory DuckDB connection with external
   access off, accepts exactly one `SELECT`, and reads only the tables it was given.

Everything else is preserved: output file names, accepted and rejected table names, publish and
failure semantics of the table sink, error text. The one exception is the engine log: with no `file`
in the logging config, no log file is written any more (project-layout E2).

## Routes a strict policy does not serve

- `run_pipeline_isolated` spawns a child interpreter, which inherits no policy, and which files the
  child's config-named reads open cannot be listed in advance. Under any policy but the ambient one it
  raises `FileAccessUnavailable` before any I/O if it would spawn a child or is given a `staging_dir`
  or `output_dir`; `isolate=False` without those runs in process under the policy.
- `run_fk_out_of_core(temp_dir=...)` with a caller-named scratch directory needs the ambient policy for
  the same reason. Without `temp_dir` the engine uses its own scratch space. The F06 typed staging copy
  of a declared source lives in that same engine-made root, under engine-generated names (`s0000`),
  and is read back only through a reader issued over that root.
- `LocalProtectedQuarantineSink(root)` (F06) opens files beneath a caller-named root directly, so it
  serves only the ambient policy: under any other its `capability` is unqualified
  (`arena_needs_ambient_policy`) and the run refuses with `protected_quarantine_unavailable` before
  reading a source. A strict-policy deployment supplies its own `ProtectedQuarantineSink`.
- `load_vault(path)`, `VaultWriter.write(path)` and `unmask_pipeline(vault_path=)` accept a path string
  only under the ambient policy; under any other, pass a `FileHandle` or `OutputDir`.
- The log file setting is ignored unless the ambient policy is installed.
- The Vault KV secret provider's TLS material (`VAULT_CACERT`, `VAULT_CLIENT_CERT`,
  `VAULT_CLIENT_KEY`) is a path the HTTP library opens from operator-set environment, outside the
  policy. It is a documented exception (`operator_environment` in
  `tests/sentry/file_access_modules.toml`, kept current by the boundary sentry), never a job-config
  value; it goes away when the adapter takes TLS material through policy handles.
- The probe-recovery step of routing (`resolve_probe_recovery`) runs the job in a child process; under
  a strict policy it answers "inconclusive" and routing falls back to the static estimate. The
  isolated and governor routes themselves fail closed with `FileAccessUnavailable`.

## Operator requirements

`RootedFileAccess` needs `openat2` and a mounted `/proc` (a handle's library path is
`/proc/self/fd/<n>`). A process that builds a rooted policy should check both at startup and refuse to
start otherwise; there is no weaker fallback. The master key file, its directory and the database
file belong in the policy's deny set. Roots come from deploy configuration read at startup, never from
an API or a config.

## How the boundary is enforced

- `tests/sentry/test_file_access_boundary.py` scans every engine module with alias, re-export,
  `functools.partial`, `getattr`, `importlib` and `Path`-variable resolution. A sink call passes only
  as an adapter handing a library a capability's descriptor, a capability module using raw
  descriptor-relative primitives on a held descriptor, or one reviewed entry in
  `tests/sentry/file_access_registry.toml` that names the call site, how its path is built, an owner
  and a reason. Being inside `decoy_engine/fileaccess/` grants nothing.
- `tests/sentry/test_file_access_entry_points.py` fails on a public callable that can reach the policy
  without being in `ENTRY_POINTS`.
- `tests/fileaccess/` runs the current-main route corpus under `LocalFileAccess` and under a real
  `RootedFileAccess` and compares both to a golden record, and traces child interpreters with
  `strace -f` to show a denial makes no syscall naming the target.
