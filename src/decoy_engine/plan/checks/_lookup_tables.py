"""Lookup columns at plan time: pin each `file` once, parse it, and check every column against it.

`type: lookup` names a file (or a shipped code set). Compile reads each distinct file once
through `read_pinned_artifact` (the read snapshots use) and keeps the bytes in the plan; every
later step, run and plan load included, parses those bytes and never the path.
`check_lookup_tables` is the one checker: compile, config-only checks and plan load all call it,
so the three cannot disagree about what a valid lookup is. Parsing itself is
`generation.kinds.parse_lookup_bytes`.

Every refusal is a `PlanCompileError` whose text is the reason's fixed summary plus the table and
column names. No cell, weight or path reaches it.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from decoy_engine import reasons
from decoy_engine.fileaccess import FileRole
from decoy_engine.generation.kinds import LookupList, lookup_from_codeset, parse_lookup_bytes
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._generation import (
    EMBEDDED_SNAPSHOT_MAX_BYTES,
    pinned_digest_message,
    read_pinned_artifact,
    verified_pinned_bytes,
)
from decoy_engine.plan._types import PinnedSnapshot

# The wire identity `parse_lookup_bytes` reports, back to its registry symbol.
_PARSE_REFUSALS = {
    "lookup_column_missing": reasons.plan.LOOKUP_COLUMN_MISSING,
    "lookup_file_empty": reasons.plan.LOOKUP_FILE_EMPTY,
    "lookup_file_format_unsupported": reasons.plan.LOOKUP_FILE_FORMAT_UNSUPPORTED,
    "lookup_file_unreadable": reasons.plan.LOOKUP_FILE_UNREADABLE,
    "lookup_too_many_entries": reasons.plan.LOOKUP_TOO_MANY_ENTRIES,
    "lookup_weight_invalid": reasons.plan.LOOKUP_WEIGHT_INVALID,
}


@dataclass(frozen=True)
class ReadLookup:
    """One lookup file's pinned bytes, read once at compile (or decoded from a stored plan)."""

    path: str
    sha256: str
    raw: bytes

    def to_pinned(self) -> PinnedSnapshot:
        return PinnedSnapshot(
            source_path=self.path,
            sha256=self.sha256,
            payload_b64=base64.b64encode(self.raw).decode("ascii"),
        )


def lookup_columns(config: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """`(table name, column)` for each `type: lookup` generate column, in declared order."""
    found: list[tuple[str, dict[str, Any]]] = []
    tables = config.get("tables")
    for table in tables if isinstance(tables, list) else []:
        if not isinstance(table, dict):
            continue
        for col in table.get("generate_columns") or []:
            if isinstance(col, dict) and col.get("type") == "lookup":
                found.append((str(table.get("name")), col))
    return found


def lookup_position(table: str, col: dict[str, Any], key: str) -> str:
    return f"tables.{table}.generate_columns.{col.get('name')}.{key}"


def lookup_file_paths(config: dict[str, Any]) -> dict[str, str]:
    """`{declared path: config position}` for each distinct lookup `file`, first position kept."""
    seen: dict[str, str] = {}
    for table, col in lookup_columns(config):
        path = col.get("file")
        if isinstance(path, str) and path:
            seen.setdefault(path, lookup_position(table, col, "file"))
    return seen


def _lookup_refusal(code: str, position: str, table: str, col: dict[str, Any]) -> PlanCompileError:
    """The one refusal constructor of the lookup checks: the reason's fixed summary plus the
    table and column names. No cell, weight or path reaches the text."""
    summary = reasons.lookup(code)[0].summary
    return PlanCompileError(
        code=code,
        path=position,
        message=f"{summary} At table {table!r}, column {col.get('name')!r}.",
    )


def read_and_pin_lookups(
    config: dict[str, Any], *, base_dir: str | Path | None = None
) -> tuple[dict[str, ReadLookup], dict[str, PlanCompileError]]:
    """Open each distinct lookup `file` once and pin its bytes; the lookup twin of
    `read_and_pin_snapshots`, through the same `read_pinned_artifact`.

    An unreadable file is recorded in the failure map under `lookup_file_unreadable` and raised
    by `check_lookup_tables`; an oversize file raises at once. Both maps are keyed by the declared
    string. A failure names the error class only, never the OS text.
    """
    pinned: dict[str, ReadLookup] = {}
    failures: dict[str, PlanCompileError] = {}
    first_column: dict[str, tuple[str, dict[str, Any]]] = {}
    for table, col in lookup_columns(config):
        if isinstance(col.get("file"), str):
            first_column.setdefault(col["file"], (table, col))
    for path, field_key in lookup_file_paths(config).items():
        read = read_pinned_artifact(
            FileRole.LOOKUP, path, field_key, base_dir, EMBEDDED_SNAPSHOT_MAX_BYTES
        )
        table, col = first_column[path]
        if read.oversize:
            raise _lookup_refusal(reasons.plan.LOOKUP_FILE_TOO_LARGE, field_key, table, col)
        if read.raw is None:
            failure = _lookup_refusal(reasons.plan.LOOKUP_FILE_UNREADABLE, field_key, table, col)
            failure.message += f" ({read.cause_type})"
            failures[path] = failure
            continue
        pinned[path] = ReadLookup(path=path, sha256=read.sha256, raw=read.raw)
    return pinned, failures


def _lookup_option(table: str, col: dict[str, Any], key: str, value: Any) -> str | None:
    """A lookup option's string value (`value` is `col.get(key)`), `None` when not given."""
    if value is None:
        return None
    if isinstance(value, str) and value:
        return value
    raise _lookup_refusal(
        reasons.config.GENERATE_OPTION_TYPE, lookup_position(table, col, key), table, col
    )


def checked_lookup(
    table: str,
    col: dict[str, Any],
    pins: Mapping[str, ReadLookup],
    failures: Mapping[str, PlanCompileError] | None,
) -> tuple[LookupList, ReadLookup | None]:
    """Parse and check one lookup column against its pinned bytes (or its shipped code set).

    Returns the list and the pin it came from (`None` for a code set). Raises `PlanCompileError`
    with the `lookup_*` code that refuses it. A file absent from `pins` is a recorded read failure
    or `lookup_not_pinned`; it is never read from disk here.
    """
    from decoy_engine.transforms.code_set import SHIPPED_CODE_SETS

    file_path = _lookup_option(table, col, "file", col.get("file"))
    codeset = _lookup_option(table, col, "codeset", col.get("codeset"))
    value_column = _lookup_option(table, col, "value_column", col.get("value_column"))
    weight_column = _lookup_option(table, col, "weight_column", col.get("weight_column"))
    if (file_path is None) == (codeset is None):
        # Neither or both: config validation refuses it; a raw dict reaches this check unvalidated.
        key = "file" if file_path is None else "codeset"
        raise _lookup_refusal(
            reasons.config.GENERATE_OPTION_VALUE_UNKNOWN,
            lookup_position(table, col, key),
            table,
            col,
        )
    if codeset is not None:
        if codeset not in SHIPPED_CODE_SETS:
            raise _lookup_refusal(
                reasons.config.GENERATE_OPTION_VALUE_UNKNOWN,
                lookup_position(table, col, "codeset"),
                table,
                col,
            )
        outcome = lookup_from_codeset(codeset, value_column, weight_column)
        pin = None
        position = lookup_position(table, col, "codeset")
    else:
        assert file_path is not None  # noqa: S101 - narrowed by the either/or check above
        position = lookup_position(table, col, "file")
        pin = pins.get(file_path)
        if pin is None:
            recorded = (failures or {}).get(file_path)
            if recorded is not None:
                raise recorded
            raise _lookup_refusal(reasons.plan.LOOKUP_NOT_PINNED, position, table, col)
        outcome = parse_lookup_bytes(
            pin.raw,
            file_path,
            value_column,
            weight_column,
            digest=f"sha256:{pin.sha256}",
            key=position,
        )
    if outcome.lookup is None:
        raise _lookup_refusal(
            _PARSE_REFUSALS[outcome.reason or "lookup_file_unreadable"],
            position,
            table,
            col,
        )
    return outcome.lookup, pin


def check_lookup_tables(
    config: dict[str, Any],
    pins: Mapping[str, ReadLookup],
    failures: Mapping[str, PlanCompileError] | None,
) -> tuple[PinnedSnapshot, ...]:
    """The one lookup check for compile, config-only checks and plan load.

    Each `type: lookup` column is parsed from its pinned bytes by the shared `checked_lookup`;
    the first refusal raises. Returns the pins in declared order, one per distinct path, for
    `build_generation_plan(..., lookups=...)`.
    """
    ordered: dict[str, PinnedSnapshot] = {}
    for table, col in lookup_columns(config):
        _list, pin = checked_lookup(table, col, pins, failures)
        if pin is not None:
            ordered.setdefault(pin.path, pin.to_pinned())
    return tuple(ordered.values())


def pin_and_check_lookups(
    config: dict[str, Any], *, base_dir: str | Path | None = None
) -> tuple[PinnedSnapshot, ...]:
    """Read and pin every lookup file once, then check every lookup column against the bytes.

    The compile-time pass: `compile_plan` and `run_config_only_checks` reach it through
    `pin_and_check_snapshots`, so the two cannot drift. Returns the pins for the plan.
    """
    pins, failures = read_and_pin_lookups(config, base_dir=base_dir)
    return check_lookup_tables(config, pins, failures)


def pinned_lookup_from_dict(data: dict[str, Any], index: int) -> tuple[PinnedSnapshot, ReadLookup]:
    """Decode one stored lookup pin, verifying its digest first (`lookup_pinned_digest_mismatch`
    at `generation.lookups[<index>]`)."""
    position = f"generation.lookups[{index}]"
    raw = verified_pinned_bytes(data)
    if raw is None:
        raise PlanCompileError(
            code=reasons.plan.LOOKUP_PINNED_DIGEST_MISMATCH,
            path=position,
            message=pinned_digest_message(position),
        )
    pinned = PinnedSnapshot(
        source_path=data["source_path"], sha256=data["sha256"], payload_b64=data["payload_b64"]
    )
    return pinned, ReadLookup(path=data["source_path"], sha256=data["sha256"], raw=raw)


def require_lookups_pinned(config: dict[str, Any], pins: Mapping[str, ReadLookup]) -> None:
    """Refuse a stored plan whose config names a lookup `file` its pinned set does not cover.

    Plan load runs this before `check_lookup_tables`, so an untrusted manifest can never make the
    checker or a later run fall back to the path. Raises `lookup_not_pinned` at `generation.lookups`.
    """
    for table, col in lookup_columns(config):
        path = col.get("file")
        if isinstance(path, str) and path not in pins:
            raise _lookup_refusal(reasons.plan.LOOKUP_NOT_PINNED, "generation.lookups", table, col)
