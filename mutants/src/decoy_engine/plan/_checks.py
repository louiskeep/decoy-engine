"""Plan-compile checks: the foundational validation set.

Each check is a pure function taking `(config, profile)` (and sometimes
additional precomputed state) and either returning silently on pass or
raising `PlanCompileError` on fail. The full check map lives in the
compile-check ownership table (S1 spec §plan-yaml-shape).

S2 relocated two relationship-related checks into
`decoy_engine.relationships`: `namespace_ambiguity` (now performed by
`build_namespace_registry`) and `fk_plan_ordering` (now performed by
`build_relationship_graph`). The check names still appear in
`PlanCompileResult.checks_passed` to preserve the S1 -> S2 regression
contract (per S2 spec B1: `checks_passed` equals S1's list plus exactly
one new entry, `orphan_fk_policy_completeness`).

`orphan_fk_policy_completeness` (new in S2, row 6) lives in
`decoy_engine.relationships._graph.check_orphan_fk_policy_completeness`
alongside the graph builder that consumes its lookup output.
"""

from __future__ import annotations

import importlib.util
import re
from typing import Any

from decoy_engine.generation.pool import unique_capacity_ok
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._pool_size import resolve_pool_size
from decoy_engine.profile._types import Profile

# Strategies under which a null-bearing integer source column diverges across the
# pandas oracle and the polars-native path (to_pandas widens int+null to float64,
# which the deterministic remap then either reshapes or hard-errors on, while the
# polars path keeps the integer). B1, PO-settled 2026-05-28: reject at validation.
_INT_NULL_REJECTED_STRATEGIES = frozenset({"truncate", "hash", "categorical"})


def _is_integer_dtype(dtype: str) -> bool:
    """True for pandas/numpy/arrow/DB integer dtype strings.

    `ColumnProfile.dtype` is `str(series.dtype)`, so it can be `int64`,
    `int64[pyarrow]`, `Int64` (nullable), `uint32`, or a DB-source label like
    `integer` / `bigint` / `smallint`. Floats, object, datetime, interval, and
    boolean are excluded.
    """
    base = dtype.lower().split("[", 1)[0].strip()
    if base in {"integer", "bigint", "smallint", "tinyint", "int", "intp", "uintp"}:
        return True
    return bool(re.fullmatch(r"u?int(8|16|32|64)?", base))


def check_unknown_provider(config: dict[str, Any]) -> None:
    """Reject configs that reference a provider not in the registry.

    Compile-check ownership table row #2. S1 shipped this against
    `S1_STUB_REGISTRY`; S4 swapped to `get_default_registry().known_providers()`
    (the real registry from `decoy_engine.providers_v2`). Behavior contract is
    preserved: same configs accepted, same configs rejected against the
    registered set; the test signature shape changed (per S4 spec §9 + cold-
    read M4).

    The registry import is deferred inside the function. The real motivation
    is import-cycle prevention: `decoy_engine.providers_v2` and the planner
    sit on the same dependency tier, and a module-level import here can
    surface a cycle as the package grows. Faker eagerness is not the issue
    (faker is already loaded by other engine modules at package import time);
    cycle prevention is. Dennis Session 22 L1.
    """
    from decoy_engine.providers_v2 import get_default_registry

    known = get_default_registry().known_providers()
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            provider = col_entry.get("provider")
            if provider is None:
                continue
            if provider not in known:
                col_name = col_entry.get("name", "?")
                raise PlanCompileError(
                    code="unknown_provider",
                    path=f"tables.{table_name}.columns.{col_name}.provider",
                    message=(
                        f"Provider {provider!r} is not in the default registry. "
                        f"Known providers: {sorted(known)!r}. Custom providers "
                        "land via `register_faker_provider_v2` (V2) or "
                        "`register_faker_provider` (V1; until S9)."
                    ),
                )


def check_non_poolable_provider_with_pool_backend(config: dict[str, Any]) -> None:
    """Reject pool-routed columns whose provider declares poolable=False.

    Compile-check ownership table row #11 (audit H5, 2026-06-12). Pool
    routing is structural: `strategy: faker` ALWAYS builds a pool
    (FakerStrategyHandler -> PoolBuilder), and PoolBuilder.build raises
    PoolCapacityError[provider_not_poolable] at runtime for any provider
    with `poolable: False` -- so a faker column on uuid/lorem-style
    providers is guaranteed dead at `run` while passing schema
    validation. This check moves that failure to compile time. The
    capacity pre-flight (row 7) deliberately SKIPS non-poolable
    providers, so nothing else catches the combination.

    Config + registry only (no profile): safe to run in --no-profile
    mode and in config-only validation paths.
    """
    from decoy_engine.providers_v2 import get_default_registry

    registry = get_default_registry()
    known = registry.known_providers()
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("strategy") != "faker":
                continue
            provider = col_entry.get("provider")
            if provider is None or provider not in known:
                continue  # unknown_provider (row 2) owns missing/unknown
            if not registry.get_capabilities(provider).poolable:
                col_name = col_entry.get("name", "?")
                raise PlanCompileError(
                    code="non_poolable_provider_with_pool_backend",
                    path=f"tables.{table_name}.columns.{col_name}.provider",
                    message=(
                        f"Provider {provider!r} declares poolable=False but column "
                        f"{table_name}.{col_name} uses strategy 'faker', which always "
                        "routes through the pool backend and fails at runtime with "
                        "provider_not_poolable. Use a poolable provider, a keyed "
                        "strategy (hash / fpe) for deterministic identifiers, or "
                        "redact."
                    ),
                )


def check_basic_uniqueness_pre_flight(config: dict[str, Any], profile: Profile) -> None:
    """Reject pool-backed `unique` configs whose non-null output-row count
    exceeds the pool capacity hint. If no pool_size is declared the check
    passes and the runtime discovers any failure later.

    DE-11: UNIQUE capacity is the non-null output-row count (row_count -
    null_count), shared with the runtime sampler via `unique_capacity_ok`;
    pool_size is resolved from both declaration sites via `resolve_pool_size`.
    Compile-check ownership table row #4.
    """
    nonnull_lookup: dict[tuple[str, str], int] = {}
    for table in profile.tables:
        for col in table.columns:
            nonnull_lookup[(table.name, col.name)] = col.row_count - col.null_count

    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("cardinality_mode") != "unique":
                continue
            if col_entry.get("backend_type") != "pool":
                continue
            col_name = col_entry.get("name", "?")
            # Resolve from both sites so a provider_config-only pool_size is
            # checked here, not silently deferred to a late runtime failure.
            pool_size = resolve_pool_size(col_entry, table_name=table_name, col_name=col_name)
            if pool_size is None:
                continue
            nonnull_rows = nonnull_lookup.get((table_name, col_name))
            if nonnull_rows is None:
                continue
            if not unique_capacity_ok(pool_size, nonnull_rows):
                raise PlanCompileError(
                    code="pool_capacity_pre_flight_unique",
                    path=f"tables.{table_name}.columns.{col_name}",
                    message=(
                        f"Column {table_name}.{col_name} uses cardinality_mode=unique "
                        f"with pool_size={pool_size}, but the pool must supply one unique "
                        f"value per non-null output row ({nonnull_rows}). The pool "
                        "cannot supply enough unique values; raise pool_size or pick "
                        "a different cardinality_mode."
                    ),
                )


def check_null_bearing_int_unsupported(config: dict[str, Any], profile: Profile) -> None:
    """Reject integer + null-bearing source columns under truncate/hash/categorical.

    Compile-check ownership table row #10 (B1, S13). PO-settled 2026-05-28: a
    column that is integer-typed AND null-bearing is REJECTED at plan-compile when
    masked under truncate / hash / categorical, because its masked value is
    ambiguous across execution substrates (`to_pandas()` widens int+null to
    float64; the polars-native path keeps the integer). This is the same class of
    "ambiguous numeric source" the S5 float-canonicalization hard error already
    rejects. Remediation: stringify or bin the column upstream.

    Profile-dependent (reads `dtype` + `null_count`), so under `no_profile=True`
    it lands in `checks_skipped`; the execution-time guard
    (`decoy_engine.execution` `reject_null_bearing_int`) is the backstop there.
    """
    null_int_lookup: dict[tuple[str, str], bool] = {}
    for table in profile.tables:
        for col in table.columns:
            null_int_lookup[(table.name, col.name)] = (
                _is_integer_dtype(col.dtype) and col.null_count > 0
            )

    # FK-child columns are EXEMPT: they are resolved through the relationship edge
    # (not masked by the strategy), and an FK job runs via the pandas oracle on
    # both substrates, so the int+null divergence cannot arise for them. Matches
    # the execution-time guard's FK exemption.
    fk_child_columns: set[tuple[str, str]] = {
        (rel.child_table, child_col)
        for rel in profile.relationships
        for child_col in rel.child_columns
    }

    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("strategy") not in _INT_NULL_REJECTED_STRATEGIES:
                continue
            col_name = col_entry.get("name", "?")
            if (table_name, col_name) in fk_child_columns:
                continue
            if not null_int_lookup.get((table_name, col_name), False):
                continue
            raise PlanCompileError(
                code="null_bearing_int_unsupported",
                path=f"tables.{table_name}.columns.{col_name}",
                message=(
                    f"Column {table_name}.{col_name} is an integer column with nulls "
                    f"masked under {col_entry.get('strategy')!r}. Integer-with-null is "
                    "not supported under truncate/hash/categorical: the masked value is "
                    "ambiguous across execution substrates (int widens to float on one "
                    "path, stays integer on the other). Stringify or bin this column "
                    "upstream. This mirrors the float-canonicalization hard error."
                ),
            )


def check_composite_columns_length_match(profile: Profile) -> None:
    """Every relationship's parent.columns and each child.columns must
    have the same length.

    The Profile-layer `Relationship` dataclass enforces this at construction
    time; this check exists at the planner layer too so a Profile that was
    hand-constructed via dict (e.g. through deserialization without going
    through `Relationship.__post_init__`) gets caught here.

    Compile-check ownership table row #5.
    """
    for rel in profile.relationships:
        parent_len = len(rel.parent_columns)
        child_len = len(rel.child_columns)
        if parent_len != child_len:
            raise PlanCompileError(
                code="composite_columns_length_mismatch",
                path=(
                    f"relationships[{rel.parent_table}.{rel.parent_columns}->"
                    f"{rel.child_table}.{rel.child_columns}]"
                ),
                message=(
                    f"Relationship {rel.parent_table}.{rel.parent_columns} -> "
                    f"{rel.child_table}.{rel.child_columns}: parent columns length "
                    f"{parent_len} != child columns length {child_len}."
                ),
            )


def check_statistical_columns(
    config: dict[str, Any],
    pinned: dict[str, Any] | None = None,
    dp_verified_columns: frozenset[tuple[str, str]] = frozenset(),
    failures: dict[str, Any] | None = None,
) -> list[tuple[str, str, str, dict[str, Any]]]:
    """Validate `type: statistical` generate columns against their snapshots.

    Compile-check ownership table row #12 (capability-gaps WS3,
    2026-06-12). A statistical column is guaranteed dead at run when its
    snapshot_file is unreadable, the source column is absent from the
    artifact, the snapshot kind has no sampler (an all-null "empty"
    column; freetext is admitted since deferred follow-up 4), a
    categorical column lacks the `allow_real_categories: true`
    disclosure opt-in (unless the compiler has DP-verified it, guide
    section 5), or `condition_on` names a pair the snapshot has no joint
    table for. `generation/statistical.load_spec` owns those verdicts
    (one set of error codes for compile time and generation time); this
    check adds the declared-order rule load_spec cannot see: the
    condition_on column must be generated BEFORE its dependent in the
    same table.

    `pinned` is the compiler's `{path: ReadSnapshot}` read-once map
    (`plan._generation.read_and_pin_snapshots`). `failures` is that same
    read-once pass's `{path: StatisticalSpecError}` classification of
    every path it could not open or parse (C-M1, round-3 remediation): a
    path absent from `pinned` raises the CLASSIFIED failure from
    `failures` rather than reopening it, so a compile that ran the read-
    once pass never calls `open()` a second time for any path. `failures
    =None` means no read-once pass ran at all (a direct/legacy caller,
    e.g. a test building a spec by hand); only then does this function
    fall back to a direct read via `plan._generation.resolve_pinned_
    snapshot`. `dp_verified_columns` is the `(table, column)` set
    `plan._checks_dp.verify_dp_snapshots` certified as an OpenDP release.
    Returns `(table_name, column_name,
    snapshot_path, spec_dict)` per validated statistical column, in
    declared order, fed to `plan._generation.build_generation_plan`.

    Config + snapshot artifact only (no profile, no source data): the
    snapshot is a config-referenced fitted-model file, so config-only
    callers (decoy validate) catch a bad artifact before a long run.

    HC-5: `high_cardinality` (full-vocabulary retention opt-in) is meaningful
    only for `type: statistical` columns; `load_spec` validates its
    dependency on `allow_real_categories` and its snapshot-kind requirement,
    but it never sees non-statistical columns, so the "wrong type entirely"
    case (a `faker`/`sequence`/... column carrying a stray `high_cardinality`
    key) is caught here instead.
    """
    from decoy_engine.generation.statistical import load_spec, spec_to_dict
    from decoy_engine.generation.statistical._spec import StatisticalSpecError
    from decoy_engine.plan._generation import resolve_pinned_snapshot

    pinned = pinned or {}
    column_specs: list[tuple[str, str, str, dict[str, Any]]] = []
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        seen: list[str] = []
        for col_entry in table_entry.get("generate_columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            col_name = col_entry.get("name", "?")
            col_type = col_entry.get("type")
            if col_type != "statistical" and "high_cardinality" in col_entry:
                raise PlanCompileError(
                    code="statistical_high_cardinality_wrong_type",
                    path=f"tables.{table_name}.generate_columns.{col_name}.high_cardinality",
                    message=(
                        f"generate column {col_name!r}: high_cardinality is only valid "
                        f"for `type: statistical` columns (got type {col_type!r})."
                    ),
                )
            if col_type == "statistical":
                snapshot_file = col_entry.get("snapshot_file")
                if not snapshot_file:
                    raise PlanCompileError(
                        code="statistical_snapshot_file_required",
                        path=f"tables.{table_name}.generate_columns.{col_name}",
                        message=f"statistical column {col_name!r} requires `snapshot_file`.",
                    )
                snapshot_file = str(snapshot_file)
                read = pinned.get(snapshot_file)
                try:
                    snap = resolve_pinned_snapshot(snapshot_file, pinned, failures)
                    dp_verified = (table_name, col_name) in dp_verified_columns
                    # Thread the read-once pass's own digest onto the spec
                    # (guide section 4.7/4.8, defect F4) so generation-time
                    # seed derivation reuses it instead of reopening
                    # snapshot_file to rehash it later.
                    spec = load_spec(
                        col_entry,
                        snapshot=snap,
                        dp_verified=dp_verified,
                        snapshot_digest=read.sha256 if read is not None else None,
                    )
                except StatisticalSpecError as exc:
                    raise PlanCompileError(
                        code=exc.code,
                        path=f"tables.{table_name}.generate_columns.{col_name}",
                        message=exc.message,
                    ) from exc
                if spec.condition_on is not None and spec.condition_on not in seen:
                    raise PlanCompileError(
                        code="statistical_condition_column_unavailable",
                        path=f"tables.{table_name}.generate_columns.{col_name}.condition_on",
                        message=(
                            f"statistical column {col_name!r} conditions on "
                            f"{spec.condition_on!r}, which is not declared earlier in "
                            f"table {table_name!r}'s generate_columns. Sequential "
                            f"conditional sampling needs the parent first."
                        ),
                    )
                column_specs.append((table_name, col_name, snapshot_file, spec_to_dict(spec)))
            seen.append(str(col_name))
    return column_specs


def _check_ner_available_for_strategy(config: dict[str, Any], strategy: str) -> None:
    """Shared body: reject `strategy` columns whose `ner` config cannot run here.

    Extracted for TX-2 (2026-07-20) so `check_text_redact_ner_available` and
    `check_text_mask_ner_available` (row #13 / row #30) stay byte-identical in
    shape without duplicating the walk. NER is an optional capability (the
    `ner` extra plus a separately-downloaded spaCy model); a column that opts
    in while either piece is missing is guaranteed dead at run. Config +
    installed packages only (no model load, no profile): safe for config-only
    callers (decoy validate).
    """
    from decoy_engine.storm.ner import DEFAULT_NER_MODEL, NerUnavailableError, ensure_ner_available

    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("strategy") != strategy:
                continue
            provider_config = col_entry.get("provider_config") or {}
            ner_cfg = provider_config.get("ner") if isinstance(provider_config, dict) else None
            if not ner_cfg:
                continue
            model = DEFAULT_NER_MODEL
            if isinstance(ner_cfg, dict) and ner_cfg.get("model"):
                model = str(ner_cfg["model"])
            col_name = col_entry.get("name", "?")
            try:
                ensure_ner_available(model)
            except NerUnavailableError as exc:
                raise PlanCompileError(
                    code=exc.code,
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.ner",
                    message=exc.message,
                ) from exc


def check_text_redact_ner_available(config: dict[str, Any]) -> None:
    """Reject `text_redact` columns whose `ner` config cannot run here.

    Compile-check ownership table row #13 (capability-gaps WS2,
    2026-06-12). NER is an optional capability (the `ner` extra plus a
    separately-downloaded spaCy model); a column that opts in while
    either piece is missing is guaranteed dead at run. Config + installed
    packages only (no model load, no profile): safe for config-only
    callers (decoy validate).
    """
    _check_ner_available_for_strategy(config, "text_redact")


def check_text_mask_ner_available(config: dict[str, Any]) -> None:
    """Reject `text_mask` columns whose `ner` config cannot run here (TX-2).

    Compile-check ownership table row #30 (TX-2, 2026-07-20): the text_mask
    analog of row #13 above, now that text_mask has its own `ner` opt-in
    (`_strategies/_text_mask.py`). Same optional-dependency reasoning, same
    fail mode (dead at run without this check).
    """
    _check_ner_available_for_strategy(config, "text_mask")


def check_vault_columns(config: dict[str, Any]) -> None:
    """Reject `vault: true` columns whose vault entries could not work.

    Compile-check ownership table row #14 (deferred follow-up 1,
    2026-06-12). Rules:

    - the `cryptography` package (the optional `vault` extra) must be
      installed: the vault is Fernet-encrypted, so without it the run
      would fail only at vault-write time, potentially hours in (F14a,
      2026-06-26);
    - a vaulted column needs a `namespace`: the vault's lookup key is
      `(namespace, masked_value)`, so without one the entry could never
      be found at unmask time;
    - `vault: true` on `strategy: fpe` is rejected: fpe is already
      algebraically reversible under the config's seed, so a vault there
      stores a second copy of the source values for zero capability,
      pure disclosure liability.

    Config-only (no profile, no source data), so it runs in both
    compile branches and in `run_config_only_checks`.
    """
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    crypto_ok: bool | None = None  # per-call memo; resolved lazily on the first vaulted column
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict) or not col_entry.get("vault"):
                continue
            col_name = col_entry.get("name", "?")
            # F14a: vault is Fernet-encrypted via the `cryptography` package. If
            # it is absent the run would fail only at vault-write time; fail at
            # compile instead. Checked once, reported on the first vaulted column.
            if crypto_ok is None:
                crypto_ok = importlib.util.find_spec("cryptography") is not None
            if not crypto_ok:
                raise PlanCompileError(
                    code="vault_requires_cryptography",
                    path=f"tables.{table_name}.columns.{col_name}.vault",
                    message=(
                        f"column {col_name!r} in table {table_name!r} declares "
                        "vault: true but the `cryptography` package is not installed. "
                        "The token vault is Fernet-encrypted; install it with "
                        "`pip install 'decoy-engine[vault]'` and re-run."
                    ),
                )
            if col_entry.get("strategy") == "fpe":
                raise PlanCompileError(
                    code="vault_strategy_reversible",
                    path=f"tables.{table_name}.columns.{col_name}.vault",
                    message=(
                        f"column {col_name!r} in table {table_name!r} declares "
                        "vault: true on strategy fpe, which `decoy unmask` already "
                        "reverses from the config alone. A vault there duplicates "
                        "the source values for no capability; remove the flag."
                    ),
                )
            if not col_entry.get("namespace"):
                raise PlanCompileError(
                    code="vault_requires_namespace",
                    path=f"tables.{table_name}.columns.{col_name}.vault",
                    message=(
                        f"column {col_name!r} in table {table_name!r} declares "
                        "vault: true but has no namespace; vault entries are keyed "
                        "by (namespace, masked_value), so add a namespace."
                    ),
                )


def check_fpe_checksum_scheme(config: dict[str, Any]) -> None:
    """Reject FPE columns whose ``checksum`` param is unknown, unsupported, or charset-incompatible.

    Compile-check ownership table row #15 (SP-04 / P5.INFRA.1 remediation).

    Three failure modes caught here:

    1. Unknown scheme (typo, e.g. ``checksum: ibna``): Decoy forbids silent
       misconfiguration passthrough.  Any value not in
       ``checksums._KNOWN_SCHEMES`` raises with code
       ``fpe_checksum_unknown_scheme``.

    2. IBAN (``checksum: iban``): per-country BBAN structure enforced by
       ``stdnum.iban.validate`` cannot be satisfied by a free Feistel
       permutation.  Raises with code ``fpe_checksum_iban_unsupported``.
       IBAN columns may still use ``checksums.validate('iban', ...)`` for
       validation-only use cases; only the FPE mode is rejected here.

    3. Charset incompatible with scheme (e.g. ``checksum: vin`` with a
       digits-only charset): a charset that cannot represent the scheme's
       required alphabet causes values to pass through unmasked at runtime.
       With ``preserve_separators=True`` (the default), missing characters are
       treated as separators; the extracted body falls below the scheme's L1
       min-length guard and the value is returned verbatim.  Raises with code
       ``fpe_checksum_charset_incompatible``.  A charset that is a strict
       superset of the required alphabet is accepted: the runtime already
       constrains the permutation to the scheme's own alphabet.

    Config-only (no profile, no source data): safe to run in --no-profile
    mode and in ``run_config_only_checks``.
    """
    from decoy_engine.checksums import _KNOWN_SCHEMES, _SCHEME_REQUIRED_CHARSET
    from decoy_engine.transforms.fpe import _CHARSETS as _FPE_CHARSETS

    _FPE_UNSUPPORTED_SCHEMES = frozenset({"iban"})

    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            if col_entry.get("strategy") != "fpe":
                continue
            provider_config = col_entry.get("provider_config") or {}
            checksum = (
                provider_config.get("checksum") if isinstance(provider_config, dict) else None
            )
            if not checksum:
                continue
            col_name = col_entry.get("name", "?")
            if checksum not in _KNOWN_SCHEMES:
                raise PlanCompileError(
                    code="fpe_checksum_unknown_scheme",
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.checksum",
                    message=(
                        f"column {col_name!r} in table {table_name!r} uses "
                        f"strategy fpe with unknown checksum scheme {checksum!r}. "
                        f"Known schemes: {sorted(_KNOWN_SCHEMES)}. "
                        "Check for typos; valid fpe checksum schemes are "
                        "luhn, npi, vin, isbn13, ean13, gtin "
                        "(iban is not supported for FPE mode)."
                    ),
                )
            if checksum in _FPE_UNSUPPORTED_SCHEMES:
                raise PlanCompileError(
                    code="fpe_checksum_iban_unsupported",
                    path=f"tables.{table_name}.columns.{col_name}.provider_config.checksum",
                    message=(
                        f"column {col_name!r} in table {table_name!r} uses "
                        "strategy fpe with checksum='iban'. FPE checksum mode "
                        "does not support 'iban': per-country BBAN structure "
                        "(enforced by stdnum.iban.validate) cannot be satisfied "
                        "by a format-preservation permutation. "
                        "Use validate-only or a different strategy for IBAN columns."
                    ),
                )
            # Charset-vs-scheme compatibility: a charset missing characters
            # required by the scheme causes values to pass through unmasked
            # (silent no-op).  Fail closed at compile.
            charset_spec = (
                provider_config.get("charset", "digits")
                if isinstance(provider_config, dict)
                else "digits"
            )
            charset_str = _FPE_CHARSETS.get(str(charset_spec), str(charset_spec))
            charset_set = frozenset(charset_str)
            required = _SCHEME_REQUIRED_CHARSET.get(checksum)
            if required is not None:
                missing = required - charset_set
                if missing:
                    raise PlanCompileError(
                        code="fpe_checksum_charset_incompatible",
                        path=(f"tables.{table_name}.columns.{col_name}.provider_config.charset"),
                        message=(
                            f"column {col_name!r} in table {table_name!r} uses "
                            f"strategy fpe with checksum={checksum!r} but the "
                            f"configured charset {charset_spec!r} is missing "
                            f"characters required by the {checksum!r} scheme: "
                            f"{sorted(missing)!r}. A charset that cannot represent "
                            "the scheme alphabet causes source values to pass through "
                            "unmasked (silent no-op). Use a charset that is a superset "
                            "of the scheme alphabet (e.g. 'ALPHANUM' for vin, "
                            "'digits' for luhn/npi/ean13/isbn13/gtin)."
                        ),
                    )


def check_derived_column_refs(config: dict[str, Any]) -> None:
    """Reject derived columns whose expression refs are missing or cyclic.

    Compile-check ownership table row #16 (SP-10 / P5.S.derived, 2026-06-28).
    Two failure modes caught here (plan-compile time, before any execution):

    1. Missing column ref: an expression references a column name that is not
       present in the same table's ``columns`` or ``generate_columns``. A
       missing ref is guaranteed to raise KeyError at row-evaluation time;
       rejecting it here surfaces the error with a clear message and the exact
       missing name.

    2. Cyclic reference: a derived column's expression depends (directly or
       transitively) on itself. Direct self-reference (``b: expression: b + 1``)
       and transitive cycles (``a -> b -> a``) are both detected via DFS over
       the dependency graph of derived columns in the same table.

    Validation never mutates (per engine rule). Config-only (no profile, no
    source data), so it runs in both compile branches and in
    ``run_config_only_checks``.

    Args:
        config: Raw pipeline config dict.

    Raises:
        PlanCompileError: A missing column ref or cyclic dependency is found.
    """
    from decoy_engine.expressions import compile_expr
    from decoy_engine.transforms.derived import _get_column_refs

    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")

        # Build the full set of column names known in this table.
        all_col_names: set[str] = set()
        for col_entry in table_entry.get("columns", []) or []:
            if isinstance(col_entry, dict) and col_entry.get("name"):
                all_col_names.add(str(col_entry["name"]))
        for col_entry in table_entry.get("generate_columns", []) or []:
            if isinstance(col_entry, dict) and col_entry.get("name"):
                all_col_names.add(str(col_entry["name"]))

        # Collect derived column definitions: {col_name -> frozenset of refs}.
        # Scans both mask-kind columns (strategy: derived, provider_config.expression)
        # and generate-kind columns (type: derived, flat expression key). The
        # generate_columns union in all_col_names is LIVE once derived works in
        # generate tables.
        derived_refs: dict[str, frozenset[str]] = {}

        def _derived_col_entries(t: dict[str, Any]) -> Any:
            """Yield (col_name, expr, col_kind) per derived column. t passed
            explicitly to avoid capturing the loop variable (ruff B023)."""
            for e in t.get("columns") or []:
                if isinstance(e, dict) and e.get("strategy") == "derived":
                    yield (
                        e.get("name", "?"),
                        (e.get("provider_config") or {}).get("expression"),
                        "columns",
                    )
            for e in t.get("generate_columns") or []:
                if isinstance(e, dict) and e.get("type") == "derived":
                    yield e.get("name", "?"), e.get("expression"), "generate_columns"

        for col_name, expr, col_kind in _derived_col_entries(table_entry):
            if not expr:
                continue  # missing expression: DerivedConfig.from_dict catches at execution time
            try:
                compiled = compile_expr(str(expr))
            except Exception:
                continue  # invalid syntax: ValidationError at execution time; skip double-report
            refs = _get_column_refs(compiled)
            missing = refs - all_col_names
            if missing:
                raise PlanCompileError(
                    code="derived_missing_column_ref",
                    path=f"tables.{table_name}.{col_kind}.{col_name}.expression",
                    message=(
                        f"derived column {col_name!r} in table {table_name!r} "
                        f"references column(s) {sorted(missing)!r} that are not "
                        f"defined in the same table. Available columns: "
                        f"{sorted(all_col_names)!r}. "
                        f"Column references must be bare identifiers matching a "
                        f"column name in the same table."
                    ),
                )

            derived_refs[str(col_name)] = refs

        # Check 2: cyclic dependencies among derived columns.
        # DFS from each derived column; detect back-edges.
        # all_derived is passed explicitly so the function does not capture
        # a loop-scoped variable (avoids ruff B023).
        def _has_cycle(
            start: str,
            visited: set[str],
            path: set[str],
            all_derived: dict[str, frozenset[str]],
        ) -> bool:
            if start in path:
                return True
            if start in visited:
                return False
            if start not in all_derived:
                return False
            visited.add(start)
            path.add(start)
            for dep in all_derived[start]:
                if dep in all_derived and _has_cycle(dep, visited, path, all_derived):
                    return True
            path.discard(start)
            return False

        visited: set[str] = set()
        for col_name in derived_refs:
            path: set[str] = {col_name}
            for dep in derived_refs[col_name]:
                if dep in derived_refs and _has_cycle(dep, visited, path, derived_refs):
                    raise PlanCompileError(
                        code="derived_cyclic_reference",
                        path=f"tables.{table_name}.columns.{col_name}.provider_config.expression",
                        message=(
                            f"derived column {col_name!r} in table {table_name!r} "
                            f"has a cyclic dependency: the expression dependency graph "
                            f"contains a cycle involving {col_name!r}. "
                            f"Cyclic derived columns cannot be evaluated. "
                            f"Refactor to remove the cycle."
                        ),
                    )
            visited.add(col_name)
