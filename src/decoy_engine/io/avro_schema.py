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
# Arrow bytes per value after decoding: the value width, or the 4-byte offset of a string or
# bytes value, whose payload is never larger than its encoded bytes (counted separately by the
# caller). Validity is a bitmap (one bit per value), so it is left out; a boolean rounds up to 1.
_FIXED_COST = {"boolean": 1, "int": 4, "long": 8, "float": 4, "double": 8, "bytes": 4, "string": 4}
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
    "date": 4,
    "time-millis": 8,  # read as time64[ns]
    "time-micros": 8,
    "decimal": 16,
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
    """What the reader needs from an admitted schema: the per-row decoded-cost estimate, the
    fewest bytes one record can encode to (so a block's object count can be checked against its
    bytes before anything is decoded), and whether a top-level field is nested."""

    fixed_row_cost: int
    nested: bool
    min_row_bytes: int = 1


@dataclass(frozen=True)
class _Shape:
    cost: int  # decoded Arrow bytes per row that do not depend on the payload
    nested: bool
    min_bytes: int  # fewest encoded bytes


# Fewest encoded bytes per primitive: a varint is at least one byte, a length-prefixed value is
# at least its length byte, `float`/`double` are fixed width.
_MIN_BYTES = {"boolean": 1, "int": 1, "long": 1, "float": 4, "double": 8, "bytes": 1, "string": 1}


class _Refused(AvroSchemaUnsupported):
    """Internal: carries the field path and type word of a refusal, never a value."""

    def __init__(self, path: str, what: str) -> None:
        super().__init__()
        self.path = path
        self.what = what


def _word(value: Any) -> str | None:
    """`value` when it is a string, else None: membership tests run on strings only."""
    return value if isinstance(value, str) else None


def _full_name(node: dict[str, Any], namespace: str) -> tuple[str, str]:
    # Polars writes records named "" (invalid per the spec); a name only matters to a
    # reference, and every reference is refused, so an anonymous type is read as it is.
    name = _word(node.get("name"))
    if not name:
        return "", namespace
    if "." in name:
        return name, name.rsplit(".", 1)[0]
    ns = _word(node.get("namespace", namespace))
    ns = namespace if ns is None else ns
    return (f"{ns}.{name}" if ns else name), ns


class _Walk:
    def __init__(self) -> None:
        self.defined: set[str] = set()
        self.defining: set[str] = set()

    def ref(self, name: str, namespace: str, path: str) -> _Shape:
        """Refuse every reference: recursive, undefined or (unsupported by Polars) defined."""
        full = name if "." in name or not namespace else f"{namespace}.{name}"
        for candidate in (full, name):
            if candidate in self.defining:
                raise _Refused(path, "recursive")
            if candidate in self.defined:
                # Polars 1.42 does not resolve named-type references (probed: OutOfSpec).
                raise _Refused(path, "named_reference")
        raise _Refused(path, "undefined_name")

    def type_(self, node: Any, path: str, namespace: str, depth: int) -> _Shape:
        if depth > MAX_DEPTH:
            raise _Refused(path, "depth")
        if isinstance(node, str):
            if node == "null":
                raise _Refused(path, "null")
            if node in _PRIMITIVES:
                return _Shape(_FIXED_COST[node], False, _MIN_BYTES[node])
            return self.ref(node, namespace, path)
        if isinstance(node, list):
            return self.union(node, path, namespace, depth)
        if not isinstance(node, dict):
            raise _Refused(path, "type")
        kind = _word(node.get("type"))
        if kind in _PRIMITIVES:
            return self.logical(node, str(kind), path)
        if kind == "record":
            return self.record(node, path, namespace, depth)
        if kind == "enum":
            return self.enum(node, path, namespace)
        if kind == "array":
            self.type_(node.get("items"), f"{path}[]", namespace, depth + 1)
            return _Shape(0, True, 1)
        if kind == "fixed":
            return self.fixed(node, path, namespace)
        if kind == "map":
            raise _Refused(path, "map")
        if kind is not None and kind != "null":
            # `{"type": "SomeName"}`: a reference, refused like a bare one.
            return self.ref(kind, namespace, path)
        # Anything else, including `{"type": {...}}` (Polars refuses a wrapped schema).
        raise _Refused(path, "type")

    def union(self, node: list[Any], path: str, namespace: str, depth: int) -> _Shape:
        nulls = [branch for branch in node if branch == "null"]
        if len(node) != 2 or len(nulls) != 1:
            raise _Refused(path, "union")
        other = node[1] if node[0] == "null" else node[0]
        inner = self.type_(other, path, namespace, depth + 1)
        return _Shape(inner.cost, inner.nested, 1)  # the branch index alone, for a null

    def logical(self, node: dict[str, Any], base: str, path: str) -> _Shape:
        plain = _Shape(_FIXED_COST[base], False, _MIN_BYTES[base])
        raw = node.get("logicalType")
        if raw is None:
            return plain
        logical = _word(raw)
        if logical is None:
            raise _Refused(path, "type")
        if logical in _REFUSED_LOGICAL:
            raise _Refused(path, logical)
        bases = _LOGICAL_BASES.get(logical)
        if bases is None:
            return plain  # unknown logical type: ignored, per the spec
        if base not in bases:
            raise _Refused(path, "logical_type_base")
        if logical == "decimal":
            _decimal(node, path, None)
        return _Shape(_LOGICAL_COST.get(logical, _FIXED_COST[base]), False, plain.min_bytes)

    def record(self, node: dict[str, Any], path: str, namespace: str, depth: int) -> _Shape:
        full, ns = _full_name(node, namespace)
        fields = node.get("fields")
        if not isinstance(fields, list) or not fields:
            raise _Refused(path, "record_fields")
        if full:
            self.defining.add(full)
        cost = 0
        min_bytes = 0
        for i, field in enumerate(fields):
            name = _word(field.get("name")) if isinstance(field, dict) else None
            if name is None:
                raise _Refused(f"{path}.{i}", "field")
            sub = self.type_(field.get("type"), f"{path}.{name}", ns, depth + 1)
            cost += sub.cost
            min_bytes += sub.min_bytes
        self.defining.discard(full)
        if full:
            self.defined.add(full)
        return _Shape(cost, True, min_bytes)

    def enum(self, node: dict[str, Any], path: str, namespace: str) -> _Shape:
        full, _ = _full_name(node, namespace)
        symbols = node.get("symbols")
        if (
            not isinstance(symbols, list)
            or not symbols
            or not all(isinstance(s, str) for s in symbols)
        ):
            raise _Refused(path, "enum")
        self.defined.add(full)
        return _Shape(max(len(s.encode("utf-8")) for s in symbols) + 4, False, 1)

    def fixed(self, node: dict[str, Any], path: str, namespace: str) -> _Shape:
        full, _ = _full_name(node, namespace)
        size = node.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise _Refused(path, "fixed_size")
        self.defined.add(full)
        raw = node.get("logicalType")
        logical = _word(raw)
        if raw is not None and logical is None:
            raise _Refused(path, "type")
        if logical in ("duration", "uuid"):
            raise _Refused(path, str(logical))
        if logical == "decimal":
            _decimal(node, path, size)
            return _Shape(16, False, size)
        if logical in _LOGICAL_BASES:
            raise _Refused(path, "logical_type_base")
        return _Shape(4, False, size)


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
        min_bytes = 0
        nested = False
        for i, field in enumerate(fields):
            name = _word(field.get("name")) if isinstance(field, dict) else None
            if name is None:
                raise _Refused(str(i), "field")
            sub = walk.type_(field.get("type"), name, ns, 1)
            cost += sub.cost
            min_bytes += sub.min_bytes
            nested = nested or sub.nested
        admission = Admission(fixed_row_cost=cost, nested=nested, min_row_bytes=min_bytes)
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
