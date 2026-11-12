# Output publication

How the engine creates, stages and binds an output directory, and what it promises about the
result. This page grows with the F22 publication work (plan
`docs/plans/2026-10-30-f22-publication-replan.md`); each section says what is in place today.
The reference sink commits through `publish_unit` on every route that uses it, and so does
the isolated route (`run_pipeline_isolated`, both modes; below). A later part adds
vault-before-output ordering; until that section exists here, the engine does not claim it.

Platforms: Linux and macOS, on both the ambient policy (`LocalFileAccess`, the CLI default) and
the strict policies (`RootedFileAccess`, `PreopenedFileAccess`). Windows keeps its plain path
calls until the Windows arm is built.

## Directories the engine creates

Every directory the engine creates on an output chain comes from one creation core with two
entry points (`src/decoy_engine/fileaccess/_output.py`):

| Entry point | Creates | Mode |
|---|---|---|
| `make_parent_dir_at` | missing parents of an output target, and the target of `ensure_dir`, at their real names | the born permission bits with the owner bits forced on: `(born & 0o777) \| 0o700`, plus an inherited set-group-ID bit |
| `make_stage_dir_at` | the private stage beside the target, and every subdirectory made inside a stage (`create_dir`) | exactly `0700` |

"Born" is the mode the filesystem gave the new directory: the umask, or a default ACL, decides
it. The engine observes it and repairs only the owner bits, so:

- under the ordinary umask `022`, a new parent is `0755`, as before;
- under a umask that strips owner bits (for example `0477`), a new parent is `0700` and the
  first attempt still publishes, where a plain `mkdir` would leave a directory the engine
  itself cannot open;
- in a set-group-ID parent, a new parent keeps the bit on Linux (a stage never does); on macOS it
  takes the parent's group without the bit.

The engine never reads or sets the process umask.

A parent is opened as it is created: each missing component is made under an open descriptor
of the directory above it, never by a path walk after the fact. When a parent appears between
the engine's check and its own `mkdir` (another attempt or another process made it), the engine
continues through the existing directory. It never changes the mode of a directory it did not
create.

On the ambient policy the user's path keeps its usual meaning: components with spaces, a
leading `.` or `_`, or non-ASCII characters are created as written, and an existing symlinked
ancestor is followed. On the strict policies every component is validated and no link is
followed.

## Durable publish: the `publication` label

An output transaction begun with `label="publication"` stages a directory (a file stage is
refused) beside the leaf under the name `._decoy_publication_pending_<token>`, which it keeps for
its whole life. Files created in it are `0600` and its directories `0700` on both POSIX
policies, whatever the umask: owner bits the umask stripped are restored before anything else
touches the file.

`publish()` is durable. In order:

1. every regular file and every directory in the stage is fsynced, deepest first, without
   following a link;
2. the stage is renamed onto the leaf, which must be absent or an empty directory;
3. the leaf's parent is fsynced: this is the commit point;
4. each directory above the parent is fsynced, deepest first, up to and including the anchor,
   whoever created it and whether or not it existed before.

The anchor is the capability root on the strict policies. On the ambient policy it is the mount
boundary: the walk goes up from the leaf's parent and stops below the first directory on another
device (`/` ends it). A failure in steps 1 or 2 raises an `OSError` with the errno only and leaves
the stage for `abort()`. A failure in steps 3 or 4 raises `CommitUncertain` (errno name only):
the unit is bound and visible, never removed, but not confirmed durable.

Unreadable directories on the chain: the leaf's parent, every parent this attempt created and
the directory above the topmost one must open and sync, or the publish fails. Above those, on the
ambient policy, a directory the engine cannot open ends the walk only when another uid owns it
(a home directory reached through `0711`, for example); one the engine's own uid owns but cannot
read is refused, because it may be the remains of an earlier attempt (named limit L1u). The
operator restores its owner bits.

Other labels (`table_sink`, `hold`, `quarantine` and the rest) publish as before.

## Destination lock and reading a bound unit

`OutputDir.lock(timeout_s)` is a context manager holding an exclusive `flock(2)` on
`.<leaf>.decoy-lock` in the leaf's parent (created `0600`, opened read-write because NFS emulates
`flock` with byte-range locks, and never unlinked). It polls until the deadline and then raises
`LockTimeout`. Where `flock` is not supported, or the lock file cannot be opened, it raises
`LockUnavailable`; there is no lock-free fallback. The kernel releases the lock when the holder
exits for any reason, SIGKILL included. The parent must exist: the lock creates nothing.

`OutputDir.read_member(component, limit=...)` reads one regular file inside a bound directory
leaf through the same capability, at most `limit` bytes (`LeafTooLarge` beyond), and returns
`None` when the parent, the leaf or the member is absent. No link is followed at the leaf or the
member, and a FIFO or other special file is refused without blocking.

`OutputDir.sync_chain()` re-runs steps 1, 3 and 4 over a bound leaf (its whole tree, then its
parent and each directory up to the anchor), so state found on disk can be made durable before
it is trusted. On the ambient policy only the leaf and its parent are strict here: nothing was
created by this call.

On the strict policies all three go through descriptors opened from the capability root, so a
symlink on the way to the leaf is refused, never followed out of the root. Windows has no arm yet:
`lock` raises `LockUnavailable`, the other two raise `FileAccessUnavailable`, and a `publication`
publish there keeps the plain rename until the Windows arm is built.

All errors these paths raise carry an errno name and no path, file name or operating-system text.

## The `decoy-publication/v1` descriptor

Package `decoy_engine.publication` (`_descriptor.py`) defines the descriptor a published unit
carries as `_decoy_publication.json` at its root; `publish_unit` (below) writes it. This section
is the format.

The bytes are canonical JSON: keys sorted, separators `,` and `:` with no whitespace, ASCII only
(other characters as `\u` escapes), no NaN or infinity. Equal content therefore has equal bytes,
and `descriptor_digest` is the SHA-256 of the bytes.

| Field | Type |
|---|---|
| `schema` | exactly `"decoy-publication/v1"` |
| `unit` | `"immutable_directory/v1"` |
| `operation_id` | the caller's id, `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}` |
| `result_id` | 64 lowercase hex digits |
| `artifacts` | array sorted by `path`, paths unique; each `{path, role, rows, sha256, size}` |
| `vault` | `null`, or `{sha256, size, entry_count}` |
| `engine_version` | the writing engine's version |

An artifact `path` is relative to the unit root, `/`-separated; no component is empty, `.` or
`..` or holds NUL, and the path is never the descriptor's own name. `role` is `"table"`. `rows`,
`size` and `entry_count` are JSON integers from 0 to 2^63 - 1. Digests are 64 lowercase hex
digits.

`result_id` is the SHA-256 of `decoy-publication-result/v1`, a NUL byte, and the canonical JSON
of the sorted artifact array. It names the output content only: it leaves out `operation_id` and
the vault, so two attempts that produced the same files have the same `result_id`, and a vault is
judged by its rows, not by a digest stored here.

`operation_id` contract: one `operation_id` names one intended result for one destination.
Reusing it for a different result is an `operation_conflict`, and a matching `operation_id` is
never taken as evidence of an equal result; only an equal `result_id` is.

One field declaration drives the writer and the reader, so the engine cannot write a descriptor
it would refuse to read. The reader refuses a stored descriptor when any of these holds: over
1 MiB; not UTF-8 JSON; a duplicate key; a non-finite number; not an object; `schema` missing or
not exactly `decoy-publication/v1`; a field missing or unknown, at any level; a value of the wrong
JSON type (a `true` is never a count, `"640"` is never a number); a count out of range; a token
outside its closed set; a digest, `operation_id` or version that does not match its shape in full
(a trailing newline included); an unsafe path; artifacts out of order or repeated; a `result_id`
that does not recompute; bytes that are not the canonical encoding. The refusal is a `ValueError`
whose text is `descriptor_refused:<check>`, a closed token, with nothing from the bytes.
`publish_unit` turns any refusal into `publication_target_occupied`, so a malformed record never
drives a judge or an adoption.

The request and receipt types (`_types.py`) are checked when built: `PublicationRequest`
(`operation_id`, `unit`, `overwrite` = `"refuse_different"`, `vault`, `lock_timeout_s`, default
30 seconds), and `CommitReceipt` (`outcome`, `operation_id`, `reason_code`, `result_id`,
`descriptor_digest`, `vault_digest`, `artifacts`, `visible`, `durable`, `os_error`), whose
`ArtifactSummary` entries hold `role`, `size`, `rows` and `sha256`, never a name or a path.

## Judge and bind: `publish_unit`

`publish_unit(out, txn, request, tables=[...])` binds one `immutable_directory/v1` unit: `txn` is
a `publication` stage of `out` holding exactly the Parquet tables named in `tables`, each a file
directly in the stage. `publish_tables(out, {name: table}, request)` is the resident entry: it
stages each table under its name and calls `publish_unit`. The caller names the files; the
publisher measures every one itself through the stage (size, SHA-256, row count from the Parquet
footer), never taking a number from the producer. A file left in the stage but not named is
bound without being described, so a producer stages nothing else (the engine's own callers
stage only the tables they name).

Under the destination lock, held from the first read to the bind:

1. the bound descriptor is read (at most 1 MiB) and this attempt's tables are measured into a
   new descriptor and its `result_id`;
2. the two are judged:

| At the leaf | Same `result_id` | Same `operation_id` | Outcome |
|---|---|---|---|
| nothing, or an empty directory | - | - | bind |
| a valid descriptor | yes | any | `already_committed`, after the unit tree and its chain to the anchor are fsynced |
| a valid descriptor | no | yes | `operation_conflict` |
| a valid descriptor | no | no | refused, `publication_target_occupied` |
| anything else: files without a descriptor, a descriptor that is unreadable, over 1 MiB or refused by the reader, a file or link at the leaf | - | - | refused, `publication_target_occupied` |

3. only a bind verdict writes the descriptor into the stage and publishes it durably (the steps
   of the `publication` label above).

The engine never replaces or merges into a bound unit: a different result is refused and the
unit, its bytes and its inode numbers are unchanged. A zero-table publish binds a unit holding
only its descriptor, so an empty unit is never silently replaced either. Every exit but a bind
aborts the stage.

Every exit is a `CommitReceipt`:

| Exit | `outcome` | `reason_code` | `visible` | `durable` |
|---|---|---|---|---|
| bound | `published` | - | yes | yes |
| the same result already bound, chain fsynced | `already_committed` | - | yes | yes |
| the same result already bound, an fsync of its chain failed | `commit_uncertain` | `publication_commit_uncertain` | yes | no |
| bound, an fsync of the chain after the rename failed | `commit_uncertain` | `publication_commit_uncertain` | yes | no |
| same `operation_id`, different result | `operation_conflict` | `publication_operation_conflict` | no | no |
| a different result, or anything else at the leaf | `refused` | `publication_target_occupied` | no | no |
| the same result bound with a vault, this request without one | `refused` | `publication_vault_mismatch` | no | no |
| `flock` unsupported or the lock file cannot be opened | `refused` | `publication_lock_unavailable` | no | no |
| the lock held past `lock_timeout_s` | `refused` | `publication_lock_timeout` | no | no |
| a staged table unreadable or not Parquet, the descriptor write, the stage fsync walk or the rename failed | `failed` | `publication_failed` | no | no |

`os_error` carries the errno name where an operating-system call failed. `result_id`,
`descriptor_digest` and `artifacts` describe the bound unit for `published`, `already_committed`
and `commit_uncertain`, and this attempt's measured result otherwise. A request that carries a
vault is not accepted yet (vault-before-output ordering is a later part); the engine never binds
an output whose vault is not durable.

A caller that wants an exception passes the receipt to `raise_for_outcome`, which returns it for
`published` and `already_committed` and otherwise raises `PublicationRefused` (`refused`,
`operation_conflict`), `PublicationFailed` (`failed`) or `CommitUncertain`. Each maps to its
registered reason through `reason_from_exception`. Receipts, error text and reason records carry
codes, errno names, digests and counts only: never a path, a file or table name, or
operating-system message text. Table names live only in the descriptor, which is data.

The reason codes are in the `decoy` namespace of the reason registry (`docs/reasons.md`).

## The reference sink's state table

`ParquetTransactionalSink` (`execution/_transactional_sink.py`) is two parts:
`StagedParquetWriter`, its write half, which only stages each table as `<table>.parquet` in an
output stage (it never publishes, locks or writes a descriptor), and the sink itself, which owns
the stage and moves every call through one state table (`publication/_sink_state.py`). The
table is literal data: one cell per state and call, one per in-flight state and exit. A
transition is taken under one lock; the write, rename or check it admits runs outside the lock,
so a call that arrives while another is in flight answers at once.

| State | `write` | `commit` | `reconcile` | `abort` |
|---|---|---|---|---|
| open | writing | committing | nothing, returns `None` | aborted, stage removed |
| writing | busy, `publication_write_in_progress` | busy | busy | pending |
| committing | busy, `publication_commit_in_progress` | busy | busy | pending |
| recovering | busy, `publication_recovery_in_progress` | busy | busy | pending |
| sealed | refused, `publication_attempt_sealed` | returns the sealed result | returns the sealed result | nothing |
| uncertain | refused, `publication_commit_uncertain` | refused, `publication_commit_uncertain` | recovering | nothing |
| refused, failed | refused with the code the attempt ended with | refused | refused | aborted, stage removed |
| aborted | refused, `publication_attempt_aborted` | refused | refused | nothing |

| In flight | Ends | State after |
|---|---|---|
| writing | normally | open |
| writing | with an exception | failed (`publication_failed`) |
| committing | published or already committed | sealed |
| committing | operation conflict or refused | refused (the refusal's code) |
| committing | commit uncertain | uncertain |
| committing | any other error before the rename | failed (`publication_failed`) |
| recovering | the destination holds this result and its chain is fsynced | sealed |
| recovering | the destination holds this result, an fsync failed | uncertain |
| recovering | absent, or a different result | refused, `publication_commit_not_visible` |

A pending abort is applied when the in-flight call ends, unless it ends sealed or uncertain:
then the result is bound and the abort does nothing. Busy and refused calls raise
`PublicationRefused` with the registered code and a value-free receipt; a refusal changes
nothing. `abort()` never raises. A table name that is not one path component is refused with
`ValueError` before the state moves.

What the sink does: every call goes through the table above. Its commit is `publish_unit`
(next section), so it ends sealed (`published`, `already_committed`), refused, failed or
uncertain, and a second `commit()` on a sealed sink returns the same receipt without any I/O.
After a failed write or commit, or a refusal, every call but `abort()` is refused, and after
an abort every call is.

## The reference sink on `publish_unit`

`ParquetTransactionalSink(target, request=PublicationRequest(...))` stages each table as
`<table>.parquet` in a `publication` stage beside the target and, on `commit()`, hands the stage
and its own full staged set, `staged_tables()`, to `publish_unit`; no caller picks a subset, so
the bound descriptor describes every file the sink staged. A streamed route's publish session
(`OutputPublish` in `execution/_chunked_output_sink.py`) reports the same set through its own
`staged_tables()`. Without a `request` the sink is its own
attempt: a fresh random `operation_id`, still judged by its measured result. A request carrying
a vault is refused with `ValueError` when the sink is built: an engine sink never binds a vault
(vault jobs publish through `publish_tables`).

`commit()` returns the `CommitReceipt` for `published` and `already_committed` and raises the
typed error for every other outcome (`raise_for_outcome`); the receipt is also on
`sink.receipt`. With no `write()` the unit holds only its descriptor.

| Commit ends | `commit()` | State after |
|---|---|---|
| bound | returns the receipt (`published`) | sealed |
| the same result already bound, chain fsynced | returns the receipt (`already_committed`) | sealed |
| bound, or found bound, and a chain fsync failed | raises `CommitUncertain` | uncertain |
| the same `operation_id` bound with another result | raises `PublicationRefused` (`publication_operation_conflict`) | refused |
| another result bound, or anything else at the target, or the lock refused | raises `PublicationRefused` (its code) | refused |
| a staged table unreadable, the descriptor write, the stage fsync walk or the rename failed | raises `PublicationFailed` (`publication_failed`) | failed |

On every outcome but a bind the stage is already removed when `commit()` returns or raises.

**Restart.** A job rerun after a crash builds a fresh sink with the same `operation_id`,
writes its tables again and calls `commit()`. The judge compares measured results: the same
result is `already_committed` once the bound unit and its chain to the anchor are fsynced; a
different result under the same `operation_id` is `operation_conflict` and the bound unit is
untouched. A matching `operation_id` alone never seals anything.

**`reconcile()`.** On a sink that has not committed it does nothing and returns `None`. On an
uncertain sink it reads the bound descriptor: this sink's own measured `result_id` there, with
the unit tree and every directory up to the anchor fsynced, seals the sink and returns a
receipt with `outcome` `already_committed` and `durable` true; an fsync failure, or a read
that cannot tell, leaves it uncertain (`CommitUncertain` again); an absent unit or a different
result refuses it (`publication_commit_not_visible`). On a sealed sink it returns the sealed
receipt. Routes do not call it; it is for a caller holding an uncertain sink.

**Evidence.** The sink's F29 publication evidence follows its state: sealed is `published`,
uncertain `unknown` (bound, not confirmed durable), refused, failed and aborted
`not_published`. A bound unit needs no cleanup; after a refusal or failure the cleanup state
is what the sink observed of its removed stage (`confirmed` on the ambient policy, `attempted`
on the strict ones).

**Routes.** Every engine route that publishes through the sink returns its receipt:
`run_sequential` and `run_fk_out_of_core` on `ExecutionResult.publication_receipt`,
`run_pipeline`'s full-frame streamed and mixed routes on the same field, and streamed
generation on `GenerationResult.publication_receipt`. On `run_pipeline`'s sequential and
out-of-core routes the pipeline rebuilds the route's result and the field is `None`; the
receipt is on the sink the caller passed (`sink.receipt`). A route whose commit raises fails
the run with that error, and nothing of this attempt is bound.

**On disk.** Every unit the reference sink publishes holds `_decoy_publication.json` beside
its Parquet files, and the target's parent holds the empty lock file `.<leaf>.decoy-lock`
(never removed). A consumer that lists every file of an output directory sees both. Unit
files are `0600` and unit directories `0700`.

## The isolated route on `publish_unit`

`run_pipeline_isolated(..., output_dir=..., request=PublicationRequest(...))` publishes through
`publish_unit` in both modes, and the receipt is `IsolatedRunResult.publication_receipt`.
Without a `request` the run is its own attempt (a fresh `operation_id`); a request carrying a
vault is refused with `ValueError` before anything runs.

- **Isolated (`isolate=True`).** The parent opens a `publication` stage beside `output_dir`
  before it spawns the child and hands the child that stage's path. The child stages plain
  Parquet files there, `<table>.parquet`, owner-only from creation, and nothing else: no
  descriptor, no lock file, no rename, no nested unit. Its row errors travel in its result
  envelope, not as a file. When the child reports `completed`, the parent, not the child,
  decides what is staged: the envelope's table names must each be one path component (no
  separator, no `..`, no repeat), and the stage must hold exactly those files, each a regular
  file. A child-written `_decoy_publication.json`, an extra file, a directory, a link or a
  missing table is refused (`failed`, `publication_failed`) and nothing is bound. The parent
  sets the stage to `0700` and each admitted file to `0600` through the entry's own descriptor
  (no link followed) before reading it; a file its owner cannot read is refused, not repaired.
  `publish_unit` then measures every size, SHA-256 and row count itself and writes the
  descriptor. The payload's source copies are written owner-only from creation, so the route
  works under a umask that strips owner bits (`0o477`).
- **In-process fallback (`isolate=False`).** The tables are staged as `<table>.parquet` in a
  `publication` stage beside `output_dir` and bound by `publish_unit`, as `publish_tables` does.

| Publication ends | `outcome` | `committed_output_dir` | `reason` |
|---|---|---|---|
| `published` or `already_committed` | `completed` | `output_dir` | none |
| refused (another result, anything else at the target, the lock), `operation_conflict` | `crashed` | none | the receipt's code |
| the stage refused, a staging write, the descriptor write or the rename failed | `crashed` | none | `publication_failed` |
| bound, but a chain fsync failed (`commit_uncertain`) | `crashed` | none | `publication_commit_uncertain`, publication state `unknown` |

`error` is then `publication <outcome>: <code>`, with ` (errno=<name>)` when an operating-system
call failed: never a path, table name or operating-system message text. On every outcome but a
bind the stage is gone when the call returns, and a killed child leaves nothing at
`output_dir`. Without an `output_dir` the child stages into the run's own scratch, the parent
admits and reads the tables back the same way, and nothing is published.

## Named limits

- **L1u, a crash during parent creation under a narrowing umask.** If the process is killed
  between creating a parent and repairing its owner bits, under a umask that strips one, the
  parent is left under its real name with a mode the engine cannot open. A retry refuses it with
  the errno only and binds nothing; the operator restores the owner bits (`chmod u+rwx`) and the
  next retry publishes. Under umask `022` a new parent is born `0755` and there is no window.
- **macOS, owner-unreadable new parents.** Linux observes the born mode of a directory that the
  umask left unreadable through an `O_PATH` descriptor. macOS has no such descriptor, so there
  the repair sets such a directory to `0700` (owner only) and drops any group and other bits
  the umask had left. Only a umask that strips owner read reaches this.
- **L1, the filesystem and the anchor.** Durability holds where the filesystem honours
  `fsync(2)` and `flock(2)`, and the anchor itself (the strict capability root, the ambient mount
  root) is durable by deployment, as is a directory another uid owns and left unsynced. No macOS
  CI row runs these paths yet.
- **L2, stages left by a killed attempt.** A SIGKILL leaves the attempt's
  `._decoy_publication_pending_<token>` stage beside the destination. Nothing reclaims it: it is
  never read as output and never bound, and each attempt uses a fresh token, so it blocks nothing.
- **Errors.** A failure while creating or opening a directory on an output chain carries no path
  and no operating-system message text: the strict policies refuse with their usual denial, and
  the ambient policy raises an `OSError` with the errno only.
