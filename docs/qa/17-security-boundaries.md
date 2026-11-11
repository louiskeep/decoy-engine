# 17. Security boundaries

Status: plan

## What it covers

The edges where the engine meets untrusted input or sensitive material: the
`FileAccess` policy, secret providers, what error messages and logs may contain, the
expression evaluators, and SQL surfaces.

## Expected behaviour

- **File access.** With a policy passed in, every file the engine opens, lists, creates,
  renames or deletes goes through it. After `forbid_ambient()`, a call with no policy
  raises `FileAccessUnavailable`. With no policy at all, behaviour is the ordinary CLI
  behaviour (paths relative to the working directory). A nested call cannot downgrade
  the installed policy.
- **Secrets.** References are `env:NAME`, `file:/PATH` or `vault://mount/path?version=N&field=f&encoding=e`.
  A HashiCorp Vault reference must pin a version; `latest` and `0` are rejected.
  Material rules are per algorithm (mask root at least 32 bytes; Fernet exactly a 32-byte
  URL-safe base64 key). Errors never repeat the reference, path or field.
- **Errors and logs.** Bad-data errors from the CSV and fixed-width readers, config
  validation errors, row errors and reason records name positions, names and counts,
  never a cell value. Failures that are not about bad input (disk, memory, bugs) are out
  of this promise.
- **Expressions.** `derived` and `when:` are held to closed grammars. `formula` runs
  under `simpleeval`. `DataFrame.eval` is pinned to the `numexpr` engine with empty
  local and global dicts.
- **Scratch files** holding rows are created in protected directories (mode `0700`,
  files `0600`).

## Checks

| Id | Run this | What should happen |
|---|---|---|
| SEC-01 | `forbid_ambient()`, then `run_pipeline` with no `file_access`. | `FileAccessUnavailable`. No file is opened. |
| SEC-02 | A rooted policy, with a source `path` of `../../etc/passwd` and with a symlink pointing outside the root. | Both denied. The denial names the config position, not the resolved path. |
| SEC-03 | A rooted policy with `column_names_file`, a `code_set` customer corpus path, a snapshot path and a quarantine `output_path` each pointing outside the root. | Every one denied. No path field bypasses the policy. |
| SEC-04 | Under an installed policy, call a nested engine entry point with a different, wider policy. | `FileAccessUnavailable`. The installed policy is not replaced. |
| SEC-05 | `vault://` reference with `version=latest`, with a duplicated query key, and with a `..` path segment. | Each rejected with `bad_secret_ref`. The message contains no part of the reference. |
| SEC-06 | `file:` secret reference to a group- or world-readable file. | Refused with `secret_file_permissions_unsafe`. |
| SEC-07 | A run that fails on a bad CSV row holding a recognisable marker value, with debug logging on. | The marker appears in no error message, exception chain, log line, progress event or evidence file. |
| SEC-08 | A run that fails validation on a value holding the marker; a row error on a cell holding the marker. | Same as SEC-07 for both. |
| SEC-09 | Run with a mask secret set and debug logging on, then search every artifact (plan, logs, evidence, run result, vault header) for the secret. | Absent everywhere. |
| SEC-10 | Injection attempts through each expression field: `derived`, `formula`, `when:`, a generate `formula`. | None executes code, reads a file or touches the network. Each is refused or evaluates safely. |
| SEC-11 | During a large out-of-core run, list the scratch directory and its permissions; after the run, and after a forced failure, list it again. | Directory `0700`, files `0600` while running. Nothing holding rows is left behind afterwards in either case. |
| SEC-12 | A table or column name containing SQL metacharacters (`"; DROP TABLE x; --`) on the out-of-core route. | Handled as an identifier or refused. Never executed as SQL. |

## Open points

- `docs/security/file-access-policy.md` has a section "Routes a strict policy does not
  serve". QA needs that list turned into checks: each such route should refuse under a
  strict policy, not fall back to ambient access.
- The local protected-quarantine adapter serves only the ambient policy and reports
  itself unqualified under a strict one. Confirm the resulting refusal is clear.
- `sql-surfaces.md` describes `source.db`, `filter`, `if_router` and `sql_run` surfaces
  that look like platform or earlier-engine features. Confirm which exist in the V1
  engine before writing checks; SEC-12 covers only the DuckDB route.
- The error-text promise explicitly excludes non-input failures, and warns that an
  Arrow or parser error can quote the cell it choked on. SEC-07 and SEC-08 may find
  such cases; each is a finding to triage, not automatically a broken promise.
- Operators must keep local-variable capture off in error trackers. That is guidance
  only; nothing in the engine can check it.
