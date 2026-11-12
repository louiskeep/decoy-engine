"""The `lookup` generate type: a weighted list from a pinned file or a shipped code set.

Two jobs live here, both free of file access. `parse_lookup_bytes` turns the bytes the plan pinned
into a `LookupList`, or says which `lookup_*` reason refuses them; plan compile and plan load both
call it through `check_lookup_tables`, and execution calls it again on the same verified bytes, so
no caller owns a second parser. `lookup_values` draws from a list with the same
`random.Random(...).choices` call `CategoricalStream` makes.

The bytes are parsed through `profile.read_csv_bytes` (the engine's one CSV reader) or the Arrow
adapter over a `MemoryHandle` (Parquet), so a malformed file raises the one value-free read error
and no path is opened here. Nothing in a refusal carries a cell, a weight or a path: the outcome
holds a reason code and nothing else.

CSV cells are exact text (no NA conversion). A blank value cell is an entry whose value is the
empty string; a blank weight cell is a refusal, not zero.
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine.errors import PipelineValidationError
from decoy_engine.fileaccess import FileAccessReadError
from decoy_engine.fileaccess.adapters import arrow as arrow_adapter
from decoy_engine.fileaccess.adapters.memory import MemoryHandle
from decoy_engine.generators.derivation import GenDeriveContext
from decoy_engine.profile import read_csv_bytes

MAX_LOOKUP_ENTRIES = 1_000_000
# The default value column of a code set: every shipped corpus has `code`.
CODESET_VALUE_COLUMN = "code"

_UNREADABLE = str(reasons.plan.LOOKUP_FILE_UNREADABLE)
_FORMAT = str(reasons.plan.LOOKUP_FILE_FORMAT_UNSUPPORTED)
_COLUMN = str(reasons.plan.LOOKUP_COLUMN_MISSING)
_EMPTY = str(reasons.plan.LOOKUP_FILE_EMPTY)
_TOO_MANY = str(reasons.plan.LOOKUP_TOO_MANY_ENTRIES)
_WEIGHT = str(reasons.plan.LOOKUP_WEIGHT_INVALID)

# Plain decimal text for a CSV weight. `float()` alone would also take `nan`, `inf`, `1_0` and
# surrounding spaces, so the text is checked whole first (ASCII digits only).
_WEIGHT_TEXT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", re.ASCII)
# Blank-only content (and a UTF-8 byte order mark) is an empty file, not an unreadable one.
_BLANK = b"\xef\xbb\xbf \t\r\n"


@dataclass(frozen=True)
class LookupList:
    """The entries of one lookup column: values, optional weights, and the file's content digest.

    `digest` is `sha256:<hex>` of the pinned bytes for a file (what the seed fingerprint hashes
    instead of the path) and `None` for a shipped code set, which has no file.
    """

    values: tuple[Any, ...]
    weights: tuple[float, ...] | None
    digest: str | None


@dataclass(frozen=True)
class LookupOutcome:
    """A parsed list, or the wire identity of the `lookup_*` reason that refuses the file.

    Never both, never a value. The identity is mapped back to its registry symbol by the plan
    check that raises it.
    """

    lookup: LookupList | None = None
    reason: str | None = None


def _refused(code: str) -> LookupOutcome:
    return LookupOutcome(reason=code)


def lookup_file_format(declared: str) -> str | None:
    """`csv` or `parquet` from the declared name's extension, else `None`."""
    lowered = declared.lower()
    if lowered.endswith(".csv"):
        return "csv"
    if lowered.endswith(".parquet"):
        return "parquet"
    return None


def _weight(value: Any) -> float | None:
    """A finite weight of at least zero from one cell, else `None` (a bool is not a number)."""
    number: float | None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        number = float(value) if _WEIGHT_TEXT.fullmatch(value) else None
    elif isinstance(value, (int, float, Decimal)):
        try:
            number = float(value)
        except (OverflowError, ValueError):
            number = None
    else:
        number = None
    if number is None or not math.isfinite(number) or number < 0:
        return None
    return number


def _weights(cells: list[Any]) -> tuple[float, ...] | None:
    """All weights, or `None` when any cell is invalid or the total is not positive and finite."""
    parsed = [_weight(c) for c in cells]
    weights = tuple(w for w in parsed if w is not None)
    if len(weights) != len(parsed):
        return None
    try:
        total = math.fsum(weights)
    except OverflowError:
        return None
    return weights if math.isfinite(total) and total > 0 else None


def _finish(values: list[Any], weight_cells: list[Any] | None, digest: str | None) -> LookupOutcome:
    """The shared tail of every source: empty, entry cap, then weights."""
    if not values:
        return _refused(_EMPTY)
    if len(values) > MAX_LOOKUP_ENTRIES:
        return _refused(_TOO_MANY)
    if weight_cells is None:
        return LookupOutcome(LookupList(tuple(values), None, digest))
    weights = _weights(weight_cells)
    if weights is None:
        return _refused(_WEIGHT)
    return LookupOutcome(LookupList(tuple(values), weights, digest))


def _columns_ok(
    names: list[str], value_column: str | None, weight_column: str | None
) -> str | None:
    """The value column to read (the first when none is named), or `None` when a name is absent."""
    value_name = value_column if value_column is not None else (names[0] if names else None)
    if value_name not in names or (weight_column is not None and weight_column not in names):
        return None
    return value_name


def _from_csv(
    raw: bytes, value_column: str | None, weight_column: str | None, digest: str, key: str
) -> LookupOutcome:
    if not raw.strip(_BLANK):
        return _refused(_EMPTY)
    failed = False
    frame = None
    try:
        frame = read_csv_bytes(raw, table="lookup", field_key=key)
    except (PipelineValidationError, FileAccessReadError):
        failed = True  # refused below, outside the handler, so nothing is chained
    if failed or frame is None:
        return _refused(_UNREADABLE)
    value_name = _columns_ok([str(c) for c in frame.columns], value_column, weight_column)
    if value_name is None:
        return _refused(_COLUMN)
    weight_cells = frame[weight_column].tolist() if weight_column is not None else None
    return _finish(frame[value_name].tolist(), weight_cells, digest)


def _value_type_ok(arrow_type: pa.DataType) -> bool:
    return bool(
        pa.types.is_string(arrow_type)
        or pa.types.is_large_string(arrow_type)
        or pa.types.is_integer(arrow_type)
        or pa.types.is_floating(arrow_type)
        or pa.types.is_boolean(arrow_type)
    )


def _from_parquet(
    raw: bytes, value_column: str | None, weight_column: str | None, digest: str, key: str
) -> LookupOutcome:
    failed = False
    table = None
    try:
        table = arrow_adapter.read_table(MemoryHandle(raw, key))
    except FileAccessReadError:
        failed = True
    if failed or table is None:
        return _refused(_UNREADABLE)
    value_name = _columns_ok(list(table.column_names), value_column, weight_column)
    if value_name is None:
        return _refused(_COLUMN)
    if not _value_type_ok(table.schema.field(value_name).type):
        return _refused(_FORMAT)
    weight_cells = table.column(weight_column).to_pylist() if weight_column is not None else None
    return _finish(table.column(value_name).to_pylist(), weight_cells, digest)


def parse_lookup_bytes(
    raw: bytes,
    declared: str,
    value_column: str | None,
    weight_column: str | None,
    *,
    digest: str,
    key: str,
) -> LookupOutcome:
    """Parse a pinned lookup file.

    `declared` is the path as written in the config (only its extension is read), `digest` the
    `sha256:<hex>` of `raw`, and `key` the config position that names the file in a read error.
    """
    file_format = lookup_file_format(declared)
    if file_format == "csv":
        return _from_csv(raw, value_column, weight_column, digest, key)
    if file_format == "parquet":
        return _from_parquet(raw, value_column, weight_column, digest, key)
    return _refused(_FORMAT)


def lookup_from_codeset(
    name: str, value_column: str | None, weight_column: str | None
) -> LookupOutcome:
    """The list a shipped code set gives. `name` must already be a shipped name.

    The corpus loads through the public `load_corpus` (sorted by code, so the order is a function
    of the corpus, not of storage). A corpus has text columns only, so a `weight_column` that names
    one is refused as an invalid weight and one that names nothing as a missing column.
    """
    from decoy_engine.transforms._codeset_loader import load_corpus

    rows = load_corpus(name)
    names = list(rows[0]) if rows else []
    value_name = value_column if value_column is not None else CODESET_VALUE_COLUMN
    if rows and (
        value_name not in names or (weight_column is not None and weight_column not in names)
    ):
        return _refused(_COLUMN)
    weight_cells = [r[weight_column] for r in rows] if weight_column is not None else None
    return _finish([r[value_name] for r in rows], weight_cells, None)


def lookup_values(
    col: dict[str, Any], n: int, ctx: GenDeriveContext, lookup: LookupList
) -> list[Any]:
    """`n` draws from `lookup`.

    The `random.Random(base_int("py")).choices(values, weights=..., k=n)` call `CategoricalStream`
    makes. Equal values, weights and seed give the same draws through either; the two columns
    still seed differently, because the seed fingerprint holds the column type and its keys.
    """
    return random.Random(ctx.base_int("py")).choices(lookup.values, weights=lookup.weights, k=n)


def lookup_digests(
    col: dict[str, Any], lookup: LookupList | None
) -> dict[tuple[str, str], str] | None:
    """The fingerprint digest map of a lookup column: its pinned file's digest.

    A code set has no file and so no entry. A file column with no list yet returns `None`, and
    `strategy_config_fingerprint` then refuses to derive a seed rather than read the path.
    """
    if lookup is None or lookup.digest is None or "file" not in col:
        return None
    return {("lookup", "file"): lookup.digest}


def lookup_column_values(
    col: dict[str, Any],
    n: int,
    seed: int,
    derive_key: Any,
    table_name: str,
    lookup_lists: Mapping[tuple[str, str], LookupList] | None,
) -> list[Any]:
    """The values of one resident `lookup` column, drawn from the list the plan pinned for it."""
    lookup = (lookup_lists or {}).get((table_name, str(col.get("name"))))
    if lookup is None:
        raise ValueError(f"lookup column {col.get('name')!r} has no pinned lookup list")
    ctx = GenDeriveContext.for_column(
        derive_key=derive_key,
        column_config=col,
        fallback_seed=seed,
        content_digests=lookup_digests(col, lookup),
    )
    return lookup_values(col, n, ctx, lookup)
