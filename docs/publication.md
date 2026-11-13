# Output publication

How the engine creates, stages and binds an output directory, and what it promises about the
result. This page grows with the F22 publication work (plan
`docs/plans/2026-10-30-f22-publication-replan.md`); each section says what is in place today.
Later parts add vault-before-output ordering and route every engine sink through
`publish_unit` (the sink's state table is in place, below); until those sections exist here,
the engine does not claim them.

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

A publication destination leaf is at most 243 bytes, so that `.<leaf>.decoy-lock` fits one
255-byte name component (see the named limit below). `lock()` and `begin(...,
label="publication")` refuse a longer leaf with the policy's denial before any I/O: no parent is
created, no lock file and no stage. Other labels keep the 255-byte component limit.

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

What the sink does today: a commit still publishes the stage with the plain `table_sink` rename
(the published unit holds only the Parquet files), so it ends sealed or failed, and a second
`commit()` on a sealed sink returns without any I/O. After a failed write or commit every call
but `abort()` is refused, and after an abort every call is. Routing the sink's commit through
`publish_unit` (receipts, `already_committed` on a restart), `reconcile()` on the sink and
the routes' receipts are a later part; until this section says so, the sink does not claim them.

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
- **The destination lock.** The lock name adds 12 bytes to the leaf (`.` before it,
  `.decoy-lock` after it), so a publication destination leaf longer than 243 bytes
  (`MAX_PUBLICATION_LEAF_BYTES` in `fileaccess/_component.py`, 255 less those 12) is refused up
  front with a value-free denial rather than failing at the lock with `ENAMETOOLONG`. The lock is
  an advisory `flock(2)`: it binds only writers that take it. On NFS it does not exclude two
  holders in one process. A forked child keeps the lock its parent took.
- **Errors.** A failure while creating or opening a directory on an output chain carries no path
  and no operating-system message text: the strict policies refuse with their usual denial, and
  the ambient policy raises an `OSError` with the errno only.
