"""Carried passthrough columns for `run_mask_chunked` (B1 revision 9).

A passthrough column is unchanged by definition, so on the engine-owned stock
adapter path `run_mask_chunked` never converts it to pandas: the profile and the
adapter see an all-null placeholder, and the output takes the source column
itself. A value the pandas round trip refuses or alters therefore comes back
exactly as the source held it, on either route and in any chunk.

A passthrough column that pandas code does read goes to pandas as before (the
"read set"): a `when:` predicate evaluated by `DataFrame.eval`, or a sibling
reference such as a `group_by` or `anchor`. If pandas then refuses a value in
such a column, `ExecutionError(code="chunked_passthrough_value_unrepresentable")`
names it, with the exception the public oracle raises as its cause.

The public oracle `run_mask_pipeline_chunked` and the legacy
`run_native_or_oracle_chunked` never build a `CarryPlan`.
"""

from __future__ import annotations

import ast
import tokenize
import unicodedata
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError

PASSTHROUGH_UNREPRESENTABLE_CODE = "chunked_passthrough_value_unrepresentable"

# Exceptions a pandas conversion raises for a value it refuses. A built-in
# `TypeError` is eligible for the profile walk only (an unhashable nested value
# fails `nunique`); `pa.ArrowTypeError` is eligible everywhere.
_CONVERSION_ERRORS: tuple[type[BaseException], ...] = (pa.ArrowInvalid, pa.ArrowTypeError)


def _conversion_errors() -> tuple[type[BaseException], ...]:
    from pandas.errors import OutOfBoundsDatetime

    return (*_CONVERSION_ERRORS, ValueError, OverflowError, OutOfBoundsDatetime)


def has_when(col: Mapping[str, Any]) -> bool:
    """Same normalization the plan compiler uses: a blank predicate has no effect."""
    when = col.get("when")
    return isinstance(when, str) and bool(when.strip())


def is_stock_adapter(adapter: Any) -> bool:
    """Carrying is enabled only for the engine-owned pandas adapter, exactly: a custom
    or subclass adapter receives the whole source table and may read any column."""
    from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter

    return adapter is None or type(adapter) is PandasExecutionAdapter


def _nfkc(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def _predicate_names(expr: str) -> set[str] | None:
    """Every name `DataFrame.eval` could resolve in `expr`, or None when the predicate
    cannot be tokenized (the caller then treats every passthrough column as read).

    Uses pandas' own tokenizer. Python's `ast` NFKC-normalizes identifiers, so a
    fullwidth `x` and a `file` spelled with the U+FB01 ligature read the columns `x` and `file`: every collected
    name is NFKC-normalized, and `read_set` compares normalized forms. Names,
    backtick-quoted spans and the value of every string literal are collected:
    over-approximating only restores the oracle's behavior for that one column."""
    try:
        from pandas.core.computation.parsing import BACKTICK_QUOTED_STRING, tokenize_string
    except ImportError:  # pragma: no cover - a pandas without the tokenizer
        return None
    names: set[str] = set()
    try:
        for kind, value in tokenize_string(expr):
            if kind == tokenize.NAME or kind == BACKTICK_QUOTED_STRING:
                names.add(_nfkc(value))
            elif kind == tokenize.STRING:
                literal = ast.literal_eval(value)
                if isinstance(literal, str):
                    names.add(_nfkc(literal))
    except Exception:
        return None
    return names


def _string_values(value: Any, out: set[str]) -> None:
    if isinstance(value, str):
        out.add(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            _string_values(item, out)
    elif isinstance(value, list | tuple):
        for item in value:
            _string_values(item, out)


def read_set(columns: Iterable[Mapping[str, Any]], passthrough: Iterable[str]) -> frozenset[str]:
    """The passthrough columns pandas code may read (rule R2).

    A column is read when (a) its name is collected from any nonblank `when:`
    predicate of the table, or (b) its name equals, as a whole string, any string
    value anywhere in another configured column's entry (the entry's own `name`
    excluded), which covers `group_by`, `anchor`, `coherent_with` and any later key
    that names a sibling. If any predicate cannot be tokenized, every passthrough
    column is read."""
    candidates = frozenset(passthrough)
    if not candidates:
        return frozenset()
    mentioned: set[str] = set()
    normalized: set[str] = set()
    for col in columns:
        if has_when(col):
            names = _predicate_names(col["when"])
            if names is None:
                return candidates
            normalized |= names
        for key, value in col.items():
            if key != "name":
                _string_values(value, mentioned)
    return frozenset(c for c in candidates if c in mentioned or _nfkc(c) in normalized)


@dataclass(frozen=True)
class CarryPlan:
    """Which passthrough columns of one table are carried and which pandas reads."""

    stock: bool
    carried: frozenset[str]
    read: tuple[str, ...]  # sorted; every passthrough column when `stock` is False

    def adapter_input(self, chunk: pa.Table) -> pa.Table:
        """The chunk the adapter ingests: carried columns as all-null placeholders."""
        from decoy_engine.execution._chunked_profile import profile_input

        return profile_input(chunk, self.carried)

    def reattach(self, produced: pa.Table, source: pa.Table) -> pa.Table:
        """Put the real source column back for every carried column `produced` holds."""
        for name in self.carried:
            if name in produced.column_names and name in source.column_names:
                index = produced.schema.get_field_index(name)
                produced = produced.set_column(
                    index, source.schema.field(name), source.column(name)
                )
        return produced

    def diagnose_profile(self, exc: Exception, supplied: pa.Table, *, table: str) -> None:
        """After the chunk-0 profile walk failed on `supplied`: raise the coded error when
        the first column (source order) whose one-column profile reproduces `exc` is a
        read passthrough column; otherwise return, and the caller re-raises `exc`."""
        from decoy_engine.execution._chunked_profile import walk_one_table

        if not self.stock or not isinstance(exc, (*_conversion_errors(), TypeError)):
            return
        for name in supplied.column_names:
            try:
                walk_one_table(supplied.select([name]), table_name=table)
            except Exception as again:
                if _same_failure(again, exc):
                    self._raise_if_read(name, exc, table=table, chunk_index=0)
                    return

    def diagnose_adapter(
        self,
        exc: Exception,
        chunk: pa.Table,
        *,
        table: str,
        chunk_index: int,
        fk_safe: Callable[[], Collection[str]],
    ) -> None:
        """After `adapter.run` failed on a chunk: same attribution as the profile, over the
        read passthrough columns only, each converted alone by the adapter's own call."""
        if not self.stock or not isinstance(exc, _conversion_errors()):
            return
        from decoy_engine.execution._fk_keys import to_pandas_fk_safe

        safe = set(fk_safe())
        for name in chunk.column_names:
            if name not in self.read:
                continue
            try:
                to_pandas_fk_safe(chunk.select([name]), {name} & safe)
            except Exception as again:
                if _same_failure(again, exc):
                    self._raise_if_read(name, exc, table=table, chunk_index=chunk_index)
                    return

    def _raise_if_read(self, name: str, exc: Exception, *, table: str, chunk_index: int) -> None:
        if name not in self.read:
            return
        raise ExecutionError(
            code=PASSTHROUGH_UNREPRESENTABLE_CODE,
            message=(
                f"{table!r} chunk {chunk_index}: passthrough column {name!r} holds a value "
                f"pandas cannot represent ({type(exc).__name__}: {exc}). A when: predicate or "
                "a sibling-reading strategy reads this column, so it must go through pandas; "
                "a passthrough column nothing reads is returned as the source holds it."
            ),
        ) from exc


def _same_failure(a: BaseException, b: BaseException) -> bool:
    return type(a) is type(b) and str(a) == str(b)


def passthrough_columns(
    config: Mapping[str, Any], *, table: str, names: Sequence[str]
) -> list[str]:
    """The schema rule's passthrough columns, in source order: configured passthrough
    without a `when:`, and unconfigured columns kept under the passthrough policy."""
    configured = {
        c["name"]: c for c in _table_columns(config, table) if isinstance(c.get("name"), str)
    }
    return [
        n
        for n in names
        if n not in configured
        or (configured[n].get("strategy") == "passthrough" and not has_when(configured[n]))
    ]


def _table_columns(config: Mapping[str, Any], table: str) -> list[dict[str, Any]]:
    table_cfg = next(
        (t for t in config.get("tables") or [] if isinstance(t, dict) and t.get("name") == table),
        {},
    )
    return [c for c in table_cfg.get("columns") or [] if isinstance(c, dict)]


def plan_carry(
    config: Mapping[str, Any], *, table: str, first_schema: pa.Schema, adapter: Any
) -> CarryPlan:
    """Decide the carried and read sets once, right after the first-chunk pull."""
    stock = is_stock_adapter(adapter)
    passthrough = passthrough_columns(config, table=table, names=first_schema.names)
    if not stock or len(set(first_schema.names)) != len(first_schema.names):
        # Carrying addresses columns by name. With duplicate names it carries nothing,
        # so the profile walk raises its own duplicate-name refusal, as on the oracle.
        return CarryPlan(stock=False, carried=frozenset(), read=tuple(sorted(passthrough)))
    read = read_set(_table_columns(config, table), passthrough)
    return CarryPlan(stock=True, carried=frozenset(passthrough) - read, read=tuple(sorted(read)))


def adapter_fk_safe_columns(plan: Any, registry: Any, graph: Any, table: str) -> set[str]:
    """The columns the stock adapter ingests losslessly (the set it passes to
    `to_pandas_fk_safe`), so a diagnosis converts a column exactly as the adapter did."""
    from decoy_engine.execution._fk_keys import fk_columns_for_table
    from decoy_engine.execution._runner import (
        date_shift_group_columns,
        group_key_group_by_columns,
        top_code_columns,
    )

    return (
        set(fk_columns_for_table(graph.edges, table))
        | date_shift_group_columns(plan, registry).get(table, set())
        | top_code_columns(plan, registry).get(table, set())
        | group_key_group_by_columns(plan, registry).get(table, set())
    )


__all__ = [
    "PASSTHROUGH_UNREPRESENTABLE_CODE",
    "CarryPlan",
    "adapter_fk_safe_columns",
    "has_when",
    "is_stock_adapter",
    "passthrough_columns",
    "plan_carry",
    "read_set",
]
