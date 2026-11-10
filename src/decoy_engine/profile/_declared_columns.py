"""The pre-read boundary that checks every source column is declared in the config.

The error names a position, never a column: an undeclared name may be a data value, because
`has_header: true` on a headerless file makes the first data row the "header". One boundary
checks every catalog a route will read (config sources, supplied tables, loader tables and
chunks), and the declared set is the output projection's `known_output_columns` read from
config instead of from the plan.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from decoy_engine import fileaccess
from decoy_engine.fileaccess import FileAccess
from decoy_engine.profile._csv_reader import read_csv_source, resolve_csv_dialect, source_refusal
from decoy_engine.profile._readers import build_declared_source
from decoy_engine.validation_result import CODES

if TYPE_CHECKING:
    import pyarrow as pa

_TOKEN = object()


def is_declared_column(entry: Mapping[str, Any]) -> bool:
    """A column entry counts as declared when it has a non-empty name and strategy."""
    name, strategy = entry.get("name"), entry.get("strategy")
    return isinstance(name, str) and bool(name) and isinstance(strategy, str) and bool(strategy)


def _table_entry(config: Mapping[str, Any], table: str) -> Mapping[str, Any]:
    for entry in config.get("tables") or []:
        if isinstance(entry, Mapping) and entry.get("name") == table:
            return entry
    return {}


def declared_columns(config: Mapping[str, Any], table: str) -> tuple[str, ...]:
    """Declared columns of `table` in order: its `columns` entries, the columns of its
    `drop_column` transforms, then the child columns of every composite relationship whose
    child is `table`. Scalar FK children are not added; they must be `columns` entries."""
    entry = _table_entry(config, table)
    names = [
        c["name"]
        for c in entry.get("columns") or []
        if isinstance(c, Mapping) and is_declared_column(c)
    ]
    for op in entry.get("transforms") or []:
        if isinstance(op, Mapping) and op.get("op") == "drop_column":
            names.extend(c for c in op.get("columns") or [] if isinstance(c, str))
    for rel in config.get("relationships") or []:
        for child in (rel.get("children") or []) if isinstance(rel, Mapping) else []:
            cols = child.get("columns") if isinstance(child, Mapping) else None
            if child.get("table") == table and isinstance(cols, list) and len(cols) > 1:
                names.extend(c for c in cols if isinstance(c, str))
    return tuple(dict.fromkeys(names))


def arrow_columns(schema: pa.Schema) -> tuple[str, ...]:
    """Column names of an Arrow schema, without a stored pandas index field."""
    from decoy_engine.execution._transforms import stored_index_fields

    index = stored_index_fields(schema)
    return tuple(n for n in schema.names if n not in index)


@fileaccess.entry_point
def source_columns(
    descriptor: Mapping[str, Any], *, table: str, file_access: FileAccess | None = None
) -> tuple[str, ...]:
    """The ordered column names a source descriptor yields, from a header or footer only."""
    path = descriptor.get("path")
    is_csv = descriptor.get("type") == "file" and descriptor.get("format") == "csv"
    if is_csv and isinstance(path, str):
        dialect = resolve_csv_dialect(descriptor, table=table, file_access=file_access)
        if dialect.column_names is not None:
            return tuple(dialect.column_names)
        frame = read_csv_source(
            path,
            dialect,
            table=table,
            dtype=str,
            nrows=0,
            file_access=file_access,
            field_key=f"sources.{table}.path",
        )
        return tuple(str(c) for c in frame.columns)
    schema = build_declared_source(table, dict(descriptor), file_access=file_access).schema()
    return arrow_columns(schema)


@dataclasses.dataclass(frozen=True)
class DeclarationInput:
    """One catalog to check. `columns=None` is existence-only, never an accepted unknown."""

    table: str
    columns: tuple[str, ...] | None
    origin: str


@dataclasses.dataclass(frozen=True)
class DeclarationFinding:
    """One undeclared table or column set. `detail` is public; `undeclared` is for the fixture
    codemod only and stays out of `repr`."""

    kind: Literal["table", "column"]
    table: str
    detail: str
    undeclared: tuple[str, ...] = dataclasses.field(default=(), repr=False)


def declaration_findings(
    config: Mapping[str, Any], inputs: Sequence[DeclarationInput]
) -> tuple[DeclarationFinding, ...]:
    """Every finding, table findings first, each group in input order."""
    from decoy_engine.execution._pipeline_context import classify_table_kinds

    known = {e.get("name") for e in config.get("tables") or [] if isinstance(e, Mapping)}
    kinds = classify_table_kinds(dict(config))
    tables: list[DeclarationFinding] = []
    columns: list[DeclarationFinding] = []
    for item in inputs:
        if item.table not in known:
            width = "" if item.columns is None else f" ({len(item.columns)} columns)"
            tables.append(
                DeclarationFinding(
                    "table",
                    item.table,
                    f"has no tables entry{width}; add a table named {item.table} that lists "
                    "every column, or remove the source",
                    item.columns or (),
                )
            )
        elif item.columns is not None and kinds.get(item.table) == "mask":
            declared = frozenset(declared_columns(config, item.table))
            missing = [(i, c) for i, c in enumerate(item.columns, 1) if c not in declared]
            if missing:
                columns.append(
                    DeclarationFinding(
                        "column",
                        item.table,
                        f"column {missing[0][0]} of {len(item.columns)} "
                        f"({len(missing)} undeclared); list every column of the table, "
                        "even as passthrough",
                        tuple(c for _, c in missing),
                    )
                )
    return (*tables, *columns)


def check_columns_declared(config: Mapping[str, Any], inputs: Sequence[DeclarationInput]) -> None:
    """Raise the first finding as its registered refusal; return when all is declared."""
    findings = declaration_findings(config, inputs)
    if not findings:
        return
    first = findings[0]
    kind = first.kind
    if kind == "table":
        raise source_refusal(CODES.SOURCE_FILE_UNDECLARED_TABLE, first.table, first.detail)
    if kind == "column":
        raise source_refusal(CODES.SOURCE_FILE_UNDECLARED_COLUMN, first.table, first.detail)
    raise AssertionError("unreachable finding kind")


def _config_sources(config: Mapping[str, Any]) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    sources = config.get("sources") or {}
    return tuple((name, d) for name, d in sources.items() if isinstance(d, Mapping))


def _supplied(schemas: Mapping[str, pa.Schema]) -> tuple[DeclarationInput, ...]:
    return tuple(
        DeclarationInput(name, arrow_columns(schemas[name]), "supplied") for name in sorted(schemas)
    )


def _run_schemas(
    schemas: Mapping[str, pa.Schema], loader_schemas: Mapping[str, pa.Schema]
) -> dict[str, pa.Schema]:
    """Supplied tables by their own schema, loader tables as declared; one schema per table."""
    import pyarrow as pa

    merged = dict(schemas)
    for name, schema in loader_schemas.items():
        if not isinstance(schema, pa.Schema):
            raise TypeError("run_pipeline: each source_loader_schemas value must be a pa.Schema.")
        if name in merged and not merged[name].equals(schema):
            raise ValueError("run_pipeline: a table has two different declared schemas.")
        merged[name] = schema
    return merged


@dataclasses.dataclass(frozen=True)
class DeclarationRequest:
    """What a route will read, as the catalogs `prepare_declared_sources` resolves and checks.
    Built only by the classmethods."""

    _config: Mapping[str, Any]
    _descriptors: tuple[tuple[str, Mapping[str, Any]], ...]
    _inputs: tuple[DeclarationInput, ...]

    @classmethod
    def for_descriptors(cls, config: Mapping[str, Any]) -> DeclarationRequest:
        return cls(config, _config_sources(config), ())

    @classmethod
    def for_supplied(
        cls, config: Mapping[str, Any], schemas: Mapping[str, pa.Schema]
    ) -> DeclarationRequest:
        return cls(config, (), _supplied(schemas))

    @classmethod
    def for_run(
        cls,
        config: Mapping[str, Any],
        schemas: Mapping[str, pa.Schema],
        loader_schemas: Mapping[str, pa.Schema] | None = None,
    ) -> DeclarationRequest:
        """`loader_schemas` declares the tables a source loader serves, checked before it runs."""
        merged = _run_schemas(schemas, loader_schemas or {})
        return cls(config, _config_sources(config), _supplied(merged))

    @classmethod
    def for_chunks(
        cls, config: Mapping[str, Any], table: str, source_schema: pa.Schema | None
    ) -> DeclarationRequest:
        inputs = [DeclarationInput(name, None, "descriptor") for name, _ in _config_sources(config)]
        inputs.append(DeclarationInput(table, None, "chunk"))
        if source_schema is not None:
            inputs.append(DeclarationInput(table, arrow_columns(source_schema), "chunk"))
        return cls(config, (), tuple(inputs))


class PreparedDeclaredSources:
    """Proof that a route's catalogs were checked before any data row was read.

    Only `prepare_declared_sources` builds one. A catalog a route meets later (a chunk, a
    loader table) goes through the same module-level check.
    """

    def __init__(
        self, token: object, config: Mapping[str, Any], inputs: tuple[DeclarationInput, ...]
    ) -> None:
        if token is not _TOKEN:
            raise TypeError("PreparedDeclaredSources is built by prepare_declared_sources")
        self._config = config
        self._config_id = id(config)
        self.inputs = inputs

    def matches(self, config: Mapping[str, Any]) -> bool:
        """True when `config` is the object this capability was prepared for."""
        return id(config) == self._config_id

    def check_names(self, table: str, names: Iterable[str]) -> None:
        check_columns_declared(self._config, [DeclarationInput(table, tuple(names), "check")])

    def checked_chunks(
        self, table: str, first_names: Iterable[str], chunk_iter: Iterable[Any]
    ) -> Iterator[Any]:
        """The later chunks, each checked just before it is yielded. The first chunk's names
        are checked now."""
        self.check_names(table, first_names)
        return (self._checked_chunk(table, chunk) for chunk in chunk_iter)

    def _checked_chunk(self, table: str, chunk: Any) -> Any:
        self.check_names(table, arrow_columns(chunk.schema))
        return chunk

    def checked_loader(self, loader: Callable[[str], Any]) -> Callable[[str], Any]:
        """`loader` wrapped so each table it returns is checked before the caller sees it."""

        def load(name: str) -> Any:
            loaded = loader(name)
            self.check_names(name, arrow_columns(loaded.schema))
            return loaded

        return load


@fileaccess.entry_point
def prepare_declared_sources(
    request: DeclarationRequest, *, file_access: FileAccess | None = None
) -> PreparedDeclaredSources:
    """Resolve the request into ordered inputs, check them once, and return the capability."""
    resolved = [
        DeclarationInput(
            name, source_columns(descriptor, table=name, file_access=file_access), "descriptor"
        )
        for name, descriptor in request._descriptors
    ]
    inputs = (*resolved, *request._inputs)
    check_columns_declared(request._config, inputs)
    return PreparedDeclaredSources(_TOKEN, request._config, inputs)


__all__ = [
    "DeclarationFinding",
    "DeclarationRequest",
    "PreparedDeclaredSources",
    "check_columns_declared",
    "declaration_findings",
    "declared_columns",
    "prepare_declared_sources",
    "source_columns",
]
