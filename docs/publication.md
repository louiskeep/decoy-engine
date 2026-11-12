# Output publication

How the engine creates, stages and binds an output directory, and what it promises about the
result. This page grows with the F22 publication work (plan
`docs/plans/2026-10-30-f22-publication-replan.md`); each section says what is in place today.
Later parts add the durable publish, the destination lock, the `decoy-publication/v1`
descriptor, the `already_committed` and `operation_conflict` outcomes, and vault-before-output
ordering. Until those sections exist here, the engine does not claim them.

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
- **Errors.** A failure while creating or opening a directory on an output chain carries no path
  and no operating-system message text: the strict policies refuse with their usual denial, and
  the ambient policy raises an `OSError` with the errno only.
