# HC-5 CLI `decoy fit` wiring — deferred follow-up

Status: **DEFERRED**, engine-only slice merged 2026-07-17. This is the CLI-sibling
half of HC-5 (high-cardinality categorical fidelity), same engine-vs-CLI split as
HC-2's `verify_corpus` primitive.

## What the engine slice ships

`quality/snapshot.compute_distribution_snapshot(df, ..., high_cardinality_columns=())`
takes a collection of source column names to fit with full vocabulary retention
(bypasses the 30-distinct cardinality cliff and the `categorical_top_k` collapse;
`other_count` stays 0). Restricted to string/object/category source dtype and to
<=100,000 distinct values / <=16 MiB combined UTF-8 label bytes, both enforced as
typed `DistributionSnapshotError` fit errors (never a silent truncation).

`generation/statistical/_spec.load_spec` validates the config-side
`high_cardinality: true` flag on a `type: statistical` generate column: it must be
a bool, requires `allow_real_categories: true` on the same column, and only
applies against a categorical snapshot kind. `plan/_checks.check_statistical_columns`
rejects a `high_cardinality` key on any non-statistical column type. Both surface
today whether or not the CLI has fit the corresponding snapshot with
`high_cardinality_columns` set — a config can be validated (`decoy validate`)
before a long fit+run cycle.

`quality/_retention_gate.warn_on_low_categorical_retention(snapshot, threshold=...)`
scores a fitted snapshot's columns and joint tables for retained mass and logs a
warning for anything below `global_settings.categorical_retention_warn_threshold`
(default 0.8; 0.0 disables it). Warn-only: never raises, never mutates the
snapshot.

## What the CLI must still wire (out of scope here)

1. **Collect the high-cardinality column list.** `decoy fit` reads the job config's
   `generate_columns[].high_cardinality` entries and passes the corresponding
   SOURCE column names into `compute_distribution_snapshot(df,
   high_cardinality_columns=...)`.
2. **Enforce consent at fit time.** Refuse to fit a `high_cardinality: true` column
   that lacks `allow_real_categories: true` on the same generate-column entry —
   the engine's `load_spec`/`check_statistical_columns` catch this at
   config-validate and generate time, but the CLI fit step reads `generate_columns`
   independently and should fail before spending fit time on a config the engine
   will reject downstream.
3. **Invoke the warn-gate.** After computing the snapshot, call
   `warn_on_low_categorical_retention(snapshot,
   threshold=categorical_retention_warn_threshold(config))` and surface the
   warnings the way `decoy fit` already surfaces other fit-time diagnostics.
4. **Propagate `DistributionSnapshotError`.** A safety-limit or wrong-dtype fit
   error from `compute_distribution_snapshot` should exit non-zero with the typed
   code and message, not an opaque traceback.

## Why this split

Same rationale as every engine/CLI split in this codebase: the engine ships pure,
importable primitives; the CLI owns the single argv/config parsing call site that
invokes them. `compute_distribution_snapshot` is shared by reporting and
generation callers that must NOT get warn-gate side effects, so the gate is a
separate, explicitly-invoked entry point rather than baked into the snapshot
function itself (HC-5 D3).
