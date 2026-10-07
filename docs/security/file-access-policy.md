Status: current

# File access policy

The engine reads and writes files named by a config or a caller: sources, snapshots, reference
tables, corpora, secrets, vaults, quarantine sidecars, target directories. This page is the
contract for an integrator (the platform, a service, a test harness) that must decide which of
those files an engine call may touch. Plan and build record:
[plan](../plans/2026-10-02-file-access-authz-engine-platform.md),
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
the installed policy. After `forbid_ambient()`, a call with no policy raises `FileAccessUnavailable`
instead of falling back to ambient access. `forbid_ambient()` is one way for the process. So is the
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

## What changes for the CLI

Exactly two behavior changes, both listed in the CHANGELOG:

1. A bare `joint_mask.reference` name must be one of the shipped tables. `customer:<path>` is the only
   path form. A bare name that is not shipped raises the same `FileNotFoundError` as before and never
   touches the file system.
2. Discovery SQL (`run_discovery_sql`) runs on a locked in-memory DuckDB connection with external
   access off, accepts exactly one `SELECT`, and reads only the tables it was given.

Everything else is preserved: the default log file, output file names, accepted and rejected table
names, publish and failure semantics of the table sink, error text.

## Routes a strict policy does not serve

- `run_pipeline_isolated` spawns a child interpreter, which inherits no policy, and which files the
  child's config-named reads open cannot be listed in advance. Under any policy but the ambient one it
  raises `FileAccessUnavailable` before any I/O if it would spawn a child or is given a `staging_dir`
  or `output_dir`; `isolate=False` without those runs in process under the policy.
- `run_fk_out_of_core(temp_dir=...)` with a caller-named scratch directory needs the ambient policy for
  the same reason. Without `temp_dir` the engine uses its own scratch space.
- `load_vault(path)`, `VaultWriter.write(path)` and `unmask_pipeline(vault_path=)` accept a path string
  only under the ambient policy; under any other, pass a `FileHandle` or `OutputDir`.
- The log file setting is ignored unless the ambient policy is installed.
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
