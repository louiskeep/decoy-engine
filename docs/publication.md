# Output publication

How the engine creates, stages and binds an output directory, and what it promises about the
result. This page grows with the F22 publication work (plan
`docs/plans/2026-10-30-f22-publication-replan.md`); each section says what is in place today.
Later parts add the `decoy-publication/v1` descriptor, the `already_committed` and
`operation_conflict` outcomes, and vault-before-output ordering. Until those sections exist here,
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
