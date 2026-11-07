"""Avro writer-schema admission (read side) and the engine's rewrite of the schema Polars writes.

Source patterns: the Avro specification's schema grammar (primitive types, `record`, `enum`,
`array`, `map`, `fixed`, unions, named types with namespaces, logical types: "a logical type
the implementation does not know is ignored and the underlying type is used"), and Spark's
`spark-avro` `SchemaConverters.toAvroType` for what the writer emits (top-level record named
`topLevelRecord`; a time-zone-aware timestamp as `timestamp-micros`, a naive one as
`local-timestamp-micros`; decimals as the `decimal` logical type).

Admission runs before Polars sees a file because Polars aborts the interpreter thread with a
panic on several valid schemas (`map`, `duration`, decimal precision above 38) and silently
drops `timestamp-nanos`. The walk refuses those, refuses what is unverified rather than guess
(a known logical type on the wrong base type, `uuid` on `fixed`), refuses every named-type
reference (Polars does not resolve them; so a recursive schema cannot loop the walk either),
and is depth-capped.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, cast

from decoy_engine.io.format_errors import AvroMalformed, AvroSchemaUnsupported

MAX_DEPTH = 64
_PRIMITIVES = frozenset({"boolean", "int", "long", "float", "double", "bytes", "string"})
# Arrow bytes per value after decoding, plus one validity byte. Strings and bytes add their
# payload, which is never larger than its encoded bytes (counted separately by the caller).
_FIXED_COST = {"boolean": 2, "int": 5, "long": 9, "float": 5, "double": 9, "bytes": 5, "string": 5}
_LOGICAL_BASES: dict[str, frozenset[str]] = {
    "date": frozenset({"int"}),
    "time-millis": frozenset({"int"}),
    "time-micros": frozenset({"long"}),
    "timestamp-millis": frozenset({"long"}),
    "timestamp-micros": frozenset({"long"}),
    "local-timestamp-millis": frozenset({"long"}),
    "local-timestamp-micros": frozenset({"long"}),
    "decimal": frozenset({"bytes", "fixed"}),
    "uuid": frozenset({"string"}),
}
_REFUSED_LOGICAL = frozenset({"duration", "timestamp-nanos", "local-timestamp-nanos"})
_LOGICAL_COST = {
    "date": 5,
    "time-millis": 9,  # read as time64[ns]
    "time-micros": 9,
    "decimal": 17,
}
_MAX_DECIMAL_PRECISION = 38
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
TOP_LEVEL_NAME = "topLevelRecord"
_LOCAL_TO_UTC = {
    "local-timestamp-millis": "timestamp-millis",
    "local-timestamp-micros": "timestamp-micros",
}


@dataclass(frozen=True)
class Admission:
    """What the reader needs from an admitted schema: the per-row decoded-cost estimate and
    whether any top-level field is nested (refused on the chunked route)."""

    fixed_row_cost: int
    nested: bool


class _Refused(AvroSchemaUnsupported):
    """Internal: carries the field path and type word of a refusal, never a value."""

    def __init__(self, path: str, what: str) -> None:
        super().__init__()
        self.path = path
        self.what = what


def _full_name(node: dict[str, Any], namespace: str) -> tuple[str, str]:
    # Polars writes records named "" (invalid per the spec); a name only matters to a
    # reference, and every reference is refused, so an anonymous type is read as it is.
    name = node.get("name")
    if not isinstance(name, str) or not name:
        return "", namespace
    if "." in name:
        return name, name.rsplit(".", 1)[0]
    ns = node.get("namespace", namespace)
    ns = ns if isinstance(ns, str) else namespace
    return (f"{ns}.{name}" if ns else name), ns


class _Walk:
    def __init__(self) -> None:
        self.defined: dict[str, tuple[int, bool]] = {}  # full name -> (cost, nested)
        self.defining: set[str] = set()

    def ref(self, name: str, namespace: str, path: str) -> tuple[int, bool]:
        """Refuse every reference: recursive, undefined or (unsupported by Polars) defined."""
        full = name if "." in name or not namespace else f"{namespace}.{name}"
        for candidate in (full, name):
            if candidate in self.defining:
                raise _Refused(path, "recursive")
            if candidate in self.defined:
                # Polars 1.42 does not resolve named-type references (probed: OutOfSpec).
                raise _Refused(path, "named_reference")
        raise _Refused(path, "undefined_name")

    def type_(self, node: Any, path: str, namespace: str, depth: int) -> tuple[int, bool]:
        """(fixed decoded cost per row, nested?) of one schema node."""
        if depth > MAX_DEPTH:
            raise _Refused(path, "depth")
        if isinstance(node, str):
            if node == "null":
                raise _Refused(path, "null")
            if node in _PRIMITIVES:
                return _FIXED_COST[node], False
            return self.ref(node, namespace, path)
        if isinstance(node, list):
            return self.union(node, path, namespace, depth)
        if not isinstance(node, dict):
            raise _Refused(path, "type")
        kind = node.get("type")
        if kind in _PRIMITIVES:
            return self.logical(node, str(kind), path)
        if kind == "record":
            return self.record(node, path, namespace, depth), True
        if kind == "enum":
            return self.enum(node, path, namespace), False
        if kind == "array":
            self.type_(node.get("items"), f"{path}[]", namespace, depth + 1)
            return 0, True
        if kind == "fixed":
            return self.fixed(node, path, namespace), False
        if kind == "map":
            raise _Refused(path, "map")
        if isinstance(kind, (str, list, dict)) and kind != "null":
            # `{"type": <named or nested type>}` wraps another schema.
            return self.type_(kind, path, namespace, depth + 1)
        raise _Refused(path, "type")

    def union(self, node: list[Any], path: str, namespace: str, depth: int) -> tuple[int, bool]:
        if len(node) != 2 or node.count("null") != 1:
            raise _Refused(path, "union")
        other = node[1] if node[0] == "null" else node[0]
        return self.type_(other, path, namespace, depth + 1)

    def logical(self, node: dict[str, Any], base: str, path: str) -> tuple[int, bool]:
        logical = node.get("logicalType")
        if logical is None:
            return _FIXED_COST[base], False
        if logical in _REFUSED_LOGICAL:
            raise _Refused(path, str(logical))
        bases = _LOGICAL_BASES.get(str(logical))
        if bases is None:
            return _FIXED_COST[base], False  # unknown logical type: ignored, per the spec
        if base not in bases:
            raise _Refused(path, "logical_type_base")
        if logical == "decimal":
            _decimal(node, path, None)
        return _LOGICAL_COST.get(str(logical), _FIXED_COST[base]), False

    def record(self, node: dict[str, Any], path: str, namespace: str, depth: int) -> int:
        full, ns = _full_name(node, namespace)
        fields = node.get("fields")
        if not isinstance(fields, list) or not fields:
            raise _Refused(path, "record_fields")
        if full:
            self.defining.add(full)
        cost = 0
        for i, field in enumerate(fields):
            if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                raise _Refused(f"{path}.{i}", "field")
            sub, _ = self.type_(field.get("type"), f"{path}.{field['name']}", ns, depth + 1)
            cost += sub
        self.defining.discard(full)
        if full:
            self.defined[full] = (cost, True)
        return cost

    def enum(self, node: dict[str, Any], path: str, namespace: str) -> int:
        full, _ = _full_name(node, namespace)
        symbols = node.get("symbols")
        if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
            raise _Refused(path, "enum")
        cost = max((len(s.encode("utf-8")) for s in symbols), default=0) + 5
        self.defined[full] = (cost, False)
        return cost

    def fixed(self, node: dict[str, Any], path: str, namespace: str) -> int:
        full, _ = _full_name(node, namespace)
        size = node.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise _Refused(path, "fixed_size")
        logical = node.get("logicalType")
        if logical in ("duration", "uuid"):
            raise _Refused(path, str(logical))
        if logical == "decimal":
            _decimal(node, path, size)
            self.defined[full] = (17, False)
            return 17
        if logical in _LOGICAL_BASES:
            raise _Refused(path, "logical_type_base")
        self.defined[full] = (5, False)
        return 5


def _strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _decimal(node: dict[str, Any], path: str, fixed_size: int | None) -> None:
    precision = node.get("precision")
    scale = node.get("scale", 0)
    if not _strict_int(precision) or not _strict_int(scale):
        raise _Refused(path, "decimal")
    precision, scale = cast(int, precision), cast(int, scale)
    if not 1 <= precision <= _MAX_DECIMAL_PRECISION or not 0 <= scale <= precision:
        raise _Refused(path, "decimal")
    if fixed_size is not None:
        digits = math.floor(math.log10(2 ** (8 * fixed_size - 1) - 1))
        if precision > digits:
            raise _Refused(path, "decimal")


def parse_schema(schema_json: bytes, *, table: str) -> Any:
    """`json.loads` of the header schema; a deep or invalid document is a malformed file."""
    failed = False
    parsed: Any = None
    try:
        parsed = json.loads(schema_json.decode("utf-8"))
    except (ValueError, RecursionError):
        failed = True
    if failed:
        raise AvroMalformed(table=table, reason="schema_json")
    return parsed


def admit(schema_json: bytes, *, table: str) -> Admission:
    """Admit the writer schema or refuse it with `AvroSchemaUnsupported` naming the field."""
    schema = parse_schema(schema_json, table=table)
    refused: tuple[str, str] | None = None
    admission = Admission(0, False)
    try:
        if not isinstance(schema, dict) or schema.get("type") != "record":
            raise _Refused("", "top_level")
        walk = _Walk()
        full, ns = _full_name(schema, "")
        fields = schema.get("fields")
        if not isinstance(fields, list) or not fields:
            raise _Refused("", "record_fields")
        if full:
            walk.defining.add(full)
        cost = 0
        nested = False
        for i, field in enumerate(fields):
            if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                raise _Refused(str(i), "field")
            sub, is_nested = walk.type_(field.get("type"), field["name"], ns, 1)
            cost += sub
            nested = nested or is_nested
        admission = Admission(fixed_row_cost=cost, nested=nested)
    except _Refused as exc:
        refused = (exc.path[:128], exc.what)
    if refused is not None:
        raise AvroSchemaUnsupported(table=table, field=refused[0], reason=refused[1])
    return admission


def valid_name(name: str) -> bool:
    return _NAME.fullmatch(name) is not None


def rewrite_for_write(polars_schema: bytes, utc_fields: frozenset[str]) -> bytes:
    """Polars' schema with the record named `topLevelRecord` and each UTC field's
    `local-timestamp-*` relabelled `timestamp-*` (same `long` encoding), nothing else."""
    schema = json.loads(polars_schema.decode("utf-8"))
    schema["name"] = TOP_LEVEL_NAME
    for field in schema["fields"]:
        if field["name"] in utc_fields:
            field["type"] = _relabel(field["type"])
    return json.dumps(schema, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _relabel(node: Any) -> Any:
    if isinstance(node, list):
        return [_relabel(branch) for branch in node]
    if isinstance(node, dict) and node.get("logicalType") in _LOCAL_TO_UTC:
        return {**node, "logicalType": _LOCAL_TO_UTC[node["logicalType"]]}
    return node


__all__ = [
    "MAX_DEPTH",
    "TOP_LEVEL_NAME",
    "Admission",
    "admit",
    "parse_schema",
    "rewrite_for_write",
    "valid_name",
]
