"""Correctness-class compile checks, split from `_checks.py` to keep it from growing."""

from __future__ import annotations

import importlib.util
import re
from typing import Any

import pyarrow as pa

from decoy_engine.plan._checks import _is_integer_dtype
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile._types import Profile

# §WHEN-NUMERIC: under a `when:` row gate, a strategy whose output is a string
# makes matched rows strings while unmatched rows keep their numbers, and one
# Arrow column cannot hold both -- the pandas route crashes at reconstruction.
# Whether a strategy emits a string is NOT listed here: it is read from the
# engine's own per-strategy output typing (`masked_output_type`, which delegates
# to `group_b_output_type` / `group_c_output_type`), so it cannot drift from what
# the masks actually emit. `hash` has dedicated execution guards
# (`gated_hash_on_integer_unsupported`, `float_canonicalization_unsupported`) and
# `faker` has no declared output type at compile time (its provider return type
# is only known by running it), and `bucketize` is not typed by
# `masked_output_type`; both, and any other pandas-only string emitter, are caught
# at execution by `gated_string_output_on_numeric_unsupported`.
_WHEN_UNTYPED_HERE = frozenset({"hash", "faker", "passthrough"})


def _is_float_dtype(dtype: str) -> bool:
    """True for pandas/numpy/arrow/DB floating-point and decimal dtype strings.

    Like `_is_integer_dtype`, `ColumnProfile.dtype` is `str(series.dtype)`, so it
    can be `float64`, `Float32` (nullable), `float64[pyarrow]`, `decimal128(10, 2)`
    or a DB-source label like `double precision` / `real` / `numeric`. Integers,
    object, datetime and boolean are excluded.
    """
    base = dtype.lower().split("[", 1)[0].strip()
    if base in {"double", "double precision", "real", "numeric", "decimal", "float"}:
        return True
    if base.startswith(("decimal128", "decimal256")):
        return True
    return bool(re.fullmatch(r"float(16|32|64)?", base))


def _strategy_emits_string(strategy: object, col_entry: dict[str, Any]) -> bool:
    """Whether `strategy`, configured as `col_entry` says, emits a string column.

    Reuses `masked_output_type` (execution/out_of_core/_mask.py) on a seed built
    from the config, so the answer is the engine's own: truncate and hash are
    string, redact is typed from its replacement scalar (read from the nested
    `provider_config`, the only place the engine reads it), and the Group (b)/(c)
    strategies resolve through `group_b_output_type` / `group_c_output_type`.
    A strategy the output typing does not cover, or a config it rejects, is left
    alone here: the latter has its own config check and the former is not a
    statically-typed string producer.
    """
    if not isinstance(strategy, str) or strategy in _WHEN_UNTYPED_HERE:
        return False
    from decoy_engine.execution._errors import ExecutionError, StrategyError
    from decoy_engine.execution.out_of_core._mask import masked_output_type
    from decoy_engine.plan._types import ColumnSeed

    raw_cfg = col_entry.get("provider_config")
    seed = ColumnSeed(
        namespace=None,
        strategy=strategy,
        provider=None,
        backend_type="faker",
        backend_version="",
        cardinality_mode="reuse",
        provider_config=tuple(raw_cfg.items()) if isinstance(raw_cfg, dict) else (),
    )
    try:
        out_type = masked_output_type(seed, None)
    except (ExecutionError, StrategyError, ValueError, TypeError):
        return False
    return bool(pa.types.is_string(out_type) or pa.types.is_large_string(out_type))


def check_when_string_output_on_numeric_unsupported(
    config: dict[str, Any], profile: Profile
) -> None:
    """§WHEN-NUMERIC: reject a `when:`-gated string-output mask on a numeric column.

    A `when:` row gate plus a string-output strategy on an integer or float
    column leaves matched rows holding a string and unmatched rows holding their
    numbers; one Arrow column cannot hold both, so the pandas route crashes at
    reconstruction (`pyarrow.ArrowInvalid`). Reject it at compile with a clear
    coded error instead. Every statically-typed string-output strategy is covered
    (truncate, redact with a string replacement, fpe, categorical with string
    categories, text_redact, text_mask, code_set, bucket_perturb), resolved through
    `masked_output_type`. `hash` and `faker` are left to their execution guards. An FK-child column
    is exempt, mirroring
    `check_null_bearing_int_unsupported`: its values are resolved through the
    relationship edge, not written by this strategy.

    Profile-dependent (reads `dtype`), so under `no_profile=True` it lands in
    `checks_skipped`; the crash it prevents is rare and the config is still
    caught at run time there.
    """
    numeric_lookup: dict[tuple[str, str], bool] = {}
    for table in profile.tables:
        for col in table.columns:
            numeric_lookup[(table.name, col.name)] = _is_integer_dtype(
                col.dtype
            ) or _is_float_dtype(col.dtype)

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
            when = col_entry.get("when")
            if not (isinstance(when, str) and when.strip()):
                continue
            strategy = col_entry.get("strategy")
            if not _strategy_emits_string(strategy, col_entry):
                continue
            col_name = col_entry.get("name", "?")
            if (table_name, col_name) in fk_child_columns:
                continue
            if not numeric_lookup.get((table_name, col_name), False):
                continue
            raise PlanCompileError(
                code="when_string_output_on_numeric_unsupported",
                path=f"tables.{table_name}.columns.{col_name}.when",
                message=(
                    f"Column {table_name}.{col_name} is a numeric column masked under "
                    f"{strategy!r} with a `when:` row gate. A string-output strategy "
                    "gated by `when:` would leave matched rows as strings and unmatched "
                    "rows as numbers in one column, which has no single Arrow type and "
                    "fails at reconstruction. Mask the whole column (drop `when:`), "
                    "stringify the column upstream, or choose a strategy whose output stays numeric."
                ),
            )


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
    - `vault: true` on `strategy: passthrough` is rejected: the column is
      never masked, so the vault entry would map a value to itself (no
      reversal capability), and the entry build (`to_pylist`) raises on
      far dates and the -2^63 marker and silently truncates time64[ns].

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
            if col_entry.get("strategy") == "passthrough":
                # A passthrough column is never masked, so its vault entry would
                # map a value to itself -- no reversal capability -- while the
                # vault build (`to_pylist` over the source values) raises on far
                # dates and the -2^63 marker and silently truncates time64[ns].
                # Reject at compile rather than crash at vault-write time.
                raise PlanCompileError(
                    code="vault_strategy_unmasked",
                    path=f"tables.{table_name}.columns.{col_name}.vault",
                    message=(
                        f"column {col_name!r} in table {table_name!r} declares "
                        "vault: true on strategy passthrough, which leaves the "
                        "value unmasked, so the vault would map it to itself for "
                        "no capability; remove the flag or mask the column."
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
