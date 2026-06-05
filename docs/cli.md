# Command-line interface

The `decoy` command (distributed on PyPI as `decoy-cli`) is the recommended way
to drive the engine. It is a thin terminal wrapper: all data logic lives in
`decoy-engine`; the CLI handles config parsing, terminal output, and forwarding
work to the engine.

```
pip install decoy-engine decoy-cli
```

## Top-level commands

| Command | What it does |
|---|---|
| `decoy run <config.yaml>` | Run a masking or generation pipeline. `--mode mask` (default) or `--mode generate`. |
| `decoy validate <config.yaml>` | Check a pipeline config against the engine schema before running. |
| `decoy plan <config.yaml>` | Compile and inspect the frozen plan for a config. |
| `decoy replan ...` | Recompile a plan (see `decoy replan --help`). |
| `decoy init` | Scaffold a starter pipeline interactively. |
| `decoy demo` | Short end-to-end walkthrough. |
| `decoy explain <topic>` | Plain-English topic help. Run `decoy explain` to list topics. |
| `decoy info` | Branded splash plus quick-start hints. |

`decoy --version` prints the CLI version; `decoy --install-completion` enables
shell tab completion.

## `decoy run`

The end-to-end driver. Profiles the source, compiles a plan, runs it on the
default execution adapter, and writes the targets named in the config.

Key options:

- `--mode mask | generate` (`-m`): mask existing data (default) or generate
  synthetic data.
- `--json`: emit a structured JSON result on stdout (progress goes to stderr).
- `--quiet` (`-q`): suppress stdout; the exit code carries success.
- `--verbose` (`-v`): debug-level CLI logs on stderr.
- `--master-key` (env `DECOY_MASTER_KEY`): 64-char hex key for portable,
  machine-stable deterministic masking. See [determinism](determinism.md).
- `--key-label`: stable namespace string for the key hierarchy; required when
  `--master-key` is set (or read from the YAML's top-level `key_label` field).

## `storm` subcommands

`storm` profiles a dataset for PII, format signals, and re-identification risk.

| Command | What it does |
|---|---|
| `decoy storm analyze <data.csv>` | Profile a dataset. This is the canonical scan command. |
| `decoy storm scan <data.csv>` | Deprecated alias for `analyze` (still works). |
| `decoy storm integrity ...` | Integrity checks over a masked output. |
| `decoy storm fields <scan> <field>` | Inspect one field from a scan. |
| `decoy storm show ...` | Display a saved scan. |
| `decoy storm diff <old> <new>` | Compare two scans. |
| `decoy storm test ...` | Detector test helper. |

Common `storm analyze` options: `--rows N` caps rows scanned, `--strategy`
selects the sampling strategy, `--json` / `--out <path>` writes a structured
profile.

## `templates` subcommands

| Command | What it does |
|---|---|
| `decoy templates list` | Browse the bundled pipeline templates. |

Bundled templates include `minimal`, `generate`, and the compliance-oriented
`hipaa`, `gdpr`, and `pci` starters.

## Convenience one-liners

There are no `decoy.mask(...)` or `decoy.scan(...)` convenience functions in the
`decoy` package today: the package exposes only the CLI app (`decoy.__main__:app`),
not a Python-callable shortcut surface. To mask from Python, use the engine
library directly (validate a `PipelineConfig`, profile, `compile_plan`, run an
execution adapter; see [recipes](recipes.md) recipe (a)). To scan from Python,
call `decoy_engine.run_storm(df, source_label)` directly.

<!-- VERIFY: that the `decoy` package exposes no public mask/scan one-liner.
Confirmed by reading decoy/src/decoy/__init__.py (only __version__) and
decoy/src/decoy/__main__.py (only the Typer `app`). If a convenience module is
added later, document it here. -->
