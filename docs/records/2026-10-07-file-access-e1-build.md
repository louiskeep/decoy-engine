Status: record

# File access E1 (engine `FileAccess` policy): build record

Plan: [2026-10-02-file-access-authz-engine-platform.md](../plans/2026-10-02-file-access-authz-engine-platform.md),
rev 3.2 and its 2026-10-07 delta. Contract page for integrators:
[file-access-policy.md](../security/file-access-policy.md). Base: engine `main` `2ed9eb4c`
(plan commits `37d80405`, `ce267ec3`). Branch `feat/file-access-policy`. Nothing was pushed, tagged
or published; the version is still 0.7.0 and the 0.9.0 release needs Cam's go.

## What was built

- **E1-0** `tests/fileaccess/test_char_*.py`, `route_corpus.py` and its golden: error text of every
  Appendix A read row, the table sink's publish, failure and zero-record semantics (absent, empty and
  non-empty destination), accepted and rejected output names, the default log destination, quarantine
  and vault writers, and a corpus of 11 current-main routes (full frame, auto-chunk, streamed to a
  sink, lazy source, hold-back spill, multi-table dispatch, transform-bearing lazy source, native
  `when`, sequential FK, out-of-core FK, generate-only) with route evidence and output digests, with
  and without the native companion. All green on the untouched base on Python 3.11 and 3.10.
- **E1-1** `src/decoy_engine/fileaccess/`: protocols and roles, `LocalFileAccess`,
  `RootedFileAccess` (`openat2` with a descriptor-walk fallback), `PreopenedFileAccess`, output
  transactions, `entry_point`, `forbid_ambient`, `bind_iterator`, `run_in_context`, denial hygiene, and
  the adapters (`arrow`, `pandas`, `polars`, `duckdb`, `serial`).
- **E1-2 / E1-3** every Appendix A row is routed through the policy: sources and lazy readers, fixed
  width, subset (inputs, preflight, staged output), snapshots (three sites), code-set corpus,
  reference tables with the shipped-table registry, mask secret, vault read and write, config file,
  quarantine writers, `ParquetTransactionalSink` and the hold-back spill on output transactions,
  discovery SQL on a locked DuckDB connection, custom providers, model packs and name hints through
  directory capabilities, the log file, the isolated route (fail closed under a strict policy), and
  the thread-context helpers in the governor monitor and the FPE pool.
- **E1-4** sentries: `tests/sentry/test_file_access_boundary.py` (sink scan with a registry of 163
  reviewed call sites), `test_file_access_entry_points.py`, 33 negative and 2 positive fixtures,
  and the runtime detector (`tests/fileaccess/strace_support.py`, `test_policy_strace.py`).
- **E1-5** CHANGELOG, `docs/security/file-access-policy.md`, CODEMAP and docs index, this record.

## Judgment calls

1. **No `beniget`.** The sentries use the stdlib `ast` with their own alias, re-export, partial,
   `getattr`, `importlib`, `__import__`, `Path`-variable and sink-as-value resolution. This keeps the
   dependency set and the certified DP profile's pinned fingerprint unchanged.
2. **`current()` falls back to the ambient policy unless `forbid_ambient()` was called.** The plan has
   internal code raise when nothing is installed. Existing code and the CLI call many internals
   outside any entry point, and every one would have needed to become an entry point. The fail-closed
   property the guarantee needs holds where it matters: after `forbid_ambient()`, nothing installed
   raises, and every `ENTRY_POINTS` member refuses with no policy (tested in a child process).
3. **`ENTRY_POINTS` is wider than the plan's list**: it also holds every function the platform or CLI
   imports that can reach the policy (`strategy_config_fingerprint`, `apply_quarantine`,
   `plan_from_yaml`, `run_subset_preflight`, `load_corpus`, ...). Directory-valued loaders that already
   take a caller path (`ModelPackLoader`, `load_name_hint_terms`, `load_disguises`, `classify_fields`)
   take `file_access=` and `capture()` it instead of installing it for a call.
4. **Isolated route (D6 fallback taken).** A child interpreter inherits no policy and its config-named
   reads cannot be listed in advance without the field walker the plan rejects, so under a strict
   policy `run_pipeline_isolated` raises `FileAccessUnavailable` before any I/O when it would spawn or
   names a `staging_dir` or `output_dir`; `run_fk_out_of_core(temp_dir=...)` likewise. The ambient
   path keeps its existing code, whose caller-derived sites are registry entries marked
   `ambient_only`, valid only because the guard (`require_ambient`) sits in front. This extends the
   plan's "caller paths are never registrable" rule beyond `_local.py`; the sentry asserts the guard
   exists. **Finding for the plan:** D6 and delta item 4 cannot "carry capabilities through both
   stages" without a broker for the child, which is platform-side scope.
5. **Module lists.** The plan's capability list is kept exact (`_rooted`, `_output`, `_preopened`,
   `_openat2`): the handles live in `_rooted.py`. The log handler lives in `_local.py` instead of an
   `adapters/logging.py`, and there is no `adapters/archive.py` (nothing in the engine zips or tars).
   `joblib` and in-memory `yaml` parsing are registry entries over `BytesIO`.
6. **Quarantine stage is opaque**: `write_jsonl_staged` returns the stage, not a `Path`. One E1-0
   characterization test was changed from reading the returned path to observing the stage on disk.
   A `quarantine_direct` stage label keeps the un-staged writer's replace semantics and writes through
   `/dev/null` and symlinks.
7. **Stage names come from a closed label set** (`table_sink`, `quarantine`, `isolated`, `hold`,
   `subset`, `vault`, `pack`), which fixes the prefix, so a caller never supplies a stage name and the
   existing `_decoy_*` prefixes tests rely on are kept.
8. **Behavior preserved on purpose** where existing tests pin internals: `Path.stat` and
   `Path.resolve` for the corpus cache key, `os.replace(src, dst)` with two arguments for the
   directory publish, a module-level `open` seam in the fixed-width reader, `pq.read_table` and
   `pq.ParquetFile` by attribute, the `_decoy_hold_` spill prefix.
9. **Lexical instead of file-system** for three comparisons that had no business touching the disk:
   the capacity estimate's absolute path, the quarantine alias guard, the default model-pack check.
10. **Field keys** are the config position where one is available; helpers that receive only a path
    (snapshot digest, code-set corpus) use a generic key (`snapshot_file`, `provider_config.corpus_source`).
11. **Dead code removed**: `get_file_size`, `is_path_exists`, `create_directory_for_file`
    (`internal/fs.py`), unused anywhere.
12. **Portable fallback** confines with a descriptor walk (`O_NOFOLLOW` per component), which is
    stronger than the plan's `realpath` + `commonpath`.
13. **strace** is not installed on the devbox. The binary was extracted from the Debian package into
    the scratch directory (no system change) and passed through `STRACE`. CI installs it (`ci.yml`).
14. **Census and ratchets**: dense-module bumps are in `test_module_size.py` and
    `docs/decisions/module-size-census-history.md`; `_pipeline.py` stays at exactly 684 lines (three
    earlier bookkeeping tests pin it); three over-max legacy files shrank back under their recorded
    sizes (`_compile.py`, `trainer.py`, `_runner.py`, the last by moving scratch-root creation into
    `out_of_core/_scratch.py`).
15. The plan's `RecordingFileAccess` lives in `tests/fileaccess/support.py`, not the package.
16. **Config and CI changes outside the package.** `.github/workflows/ci.yml` installs `strace`
    (the D10 runtime detector fails rather than skips without it) and adds `tests/fileaccess` to the
    routing guard so the new directory cannot fall out of CI. `pyproject.toml` excludes
    `tests/sentry/fa_fixtures` from ruff: those files are deliberately bad code (negative sentry
    fixtures with unused imports and raw sinks) that the sentries must scan, so they cannot be linted
    clean or formatted.
17. **Public API diff** (signature comparison of 1239 base symbols against head, `apisurf.py`): every
    change adds a keyword-only `file_access=` or an optional trailing parameter, except two that are
    not additive and are noted for the compatibility checklist: `quarantine.write_jsonl_staged`
    now returns an opaque `OutputTxn` and `publish_staged_jsonl` / `discard_staged_jsonl` take it
    (they took and returned a `Path`; nothing in this repo or the platform source outside the
    quarantine module calls them), and six private-module attributes that were stdlib re-exports
    (`pq`, `shutil`, `tempfile`, `logging` imported into `_transactional_sink`, `quarantine`,
    `_codeset_loader`) are gone. `read_fixed_width` and `run_discovery_sql` keep their old calls and
    gain optional arguments.

## Red before

Probe `redbefore.py` run on the untouched base (`ce267ec3`), no policy:

```
V15 bare traversal read           READ ['id', 'col_secret']
V7  customer: reference           READ ['id', 'col_secret']
V6  corpus_source                 READ {'code': 'B'}
V9  mask_secret_ref file:         READ 32 bytes
V18 snapshot_file digest          READ (digest computed)
V22 discovery read_text           READ SENTINEL-...
V22 discovery COPY TO             refused: DiscoverySqlError   (already blocked by the keyword filter)
V5  quarantine output, any path   stage created beside q.jsonl
V1  source path, absolute         READ schema ['id', 'col_secret']
```

The same probe on the E1 head with no policy (the CLI): V15 and V22 read_text are refused, everything
else reads as before, which is exactly the two documented behavior changes. Under a strict policy
(`tests/fileaccess/test_policy_denials.py`) each of these is the one denial. The tests that need the
new API (`file_access=`, `decoy_engine.fileaccess`) are red on the base by `ImportError` or
`TypeError`; the behavior-level red is the table above.

## Sink inventory (E1-4 sentry)

The scan resolves 163 call sites that need a registry entry, plus the adapter and capability modules
that pass by rule. By category: 39 ambient `LocalFileAccess` opens in `_local.py`; 33 in the isolated
route and the caller-named scratch root (ambient only, behind `require_ambient`); 60 engine scratch,
`/proc`, cgroup and temp-filesystem sites (out-of-core spill, pool quality, governor, disk probes);
9 in the cloud readers and SFTP (in-memory buffers and the known-hosts file, FA-2); 22 package data,
in-memory buffers and engine-owned handle constructions. The full list, with each call site's
construction rule, owner and reason, is `tests/sentry/file_access_registry.toml`.

## Results

RESULTS_PLACEHOLDER

## Not verified

- The platform half (P1): `forbid_ambient()` wiring, `PrincipalFileAccess`, the platform sentries.
- Windows and macOS (strict policies are Linux 5.6 or later; the descriptor-walk fallback is tested on
  Linux only with the walk forced).
- Coverage and mutation on the changed units, dennis and Codex gates, engine CI mirror steps other than
  ruff, mypy and the tests named above (sphinx `-W`, the no-extras environment).
- A strict policy over a real `/proc`-less container.

## PR body draft: compatibility contract section 9 checklist

PRBODY_PLACEHOLDER
