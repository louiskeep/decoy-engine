"""The single casting boundary: sentinel -> parse before masking, format after masking.

`DeclaredCastRun` is the one object every route calls (`cast_sources_pre_mask`,
`format_outputs_post_mask`): there is no route-private parser. It applies each column's contract
to the framed lexeme, applies the declared disposition exactly once (fail / field-null / row
quarantine) and reduces everything to value-free counts. It returns new Arrow tables; it never
mutates its inputs. Rows are identified by absolute position in the source table, so attribution
is independent of any batch partitioning.

Disposition scopes (FRAME): ``fail`` aborts; ``null`` is field-level and keeps the row;
``quarantine`` is row-level and removes the whole row from normal output, streaming the original
framed row to the protected sink. A row that fails several fields is quarantined once.
"""

from __future__ import annotations

import dataclasses
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine.contracts import (
    declared_casting as C,  # noqa: N812 -- short alias for the contract namespace
)
from decoy_engine.declared_casting import _catalog
from decoy_engine.declared_casting import (
    _counts as K,  # noqa: N812 -- short alias for the contract namespace
)
from decoy_engine.declared_casting import (
    _quarantine_protocol as P,  # noqa: N812 -- short alias for the contract namespace
)
from decoy_engine.declared_casting._eligibility import contract_model

_R = reasons.runtime


def arrow_type(model: C.DeclaredColumnContractV1) -> pa.DataType:
    p = model.parse_format
    if model.logical_type == "string":
        return pa.string()
    if model.logical_type == "integer":
        prefix = "int" if p.signed else "uint"
        return getattr(pa, f"{prefix}{p.bit_width}")()
    if model.logical_type in ("decimal", "currency"):
        return pa.decimal128(p.precision, p.scale)
    return pa.date32()


@dataclass(frozen=True)
class _Col:
    table: str
    name: str
    model: C.DeclaredColumnContractV1
    parse: Callable[[Any], Any]
    fmt: Callable[[Any], Any] | None
    sentinels: tuple[Any, ...]
    binary: bool
    policy: str


def _compile(plan: Any) -> dict[str, list[_Col]]:
    by_table: dict[str, list[_Col]] = {}
    for c in plan.columns:
        model = contract_model(c)
        sentinels = (
            model.sentinel_to_null.decoded(binary=model.binary_input)
            if model.sentinel_to_null
            else ()
        )
        by_table.setdefault(c.table, []).append(
            _Col(
                c.table,
                c.column,
                model,
                _catalog.compile_parser(model.parse_format),
                _catalog.compile_formatter(model.output_format)
                if model.output_format is not None
                else None,
                sentinels,
                model.binary_input,
                model.error_policy,
            )
        )
    return by_table


def open_cast_run(plan: Any, runtime: Any) -> DeclaredCastRun | None:
    """The run's casting boundary, or None when the plan declares no contract."""
    block = getattr(plan, "declared_casting", None)
    return DeclaredCastRun(block, runtime) if block is not None else None


def remap_row_errors(records: Any, kept: Mapping[str, list[int]]) -> tuple[Any, ...]:
    """Row errors keep pointing at the same rows after declared quarantine removed some.

    ``kept[table]`` lists the old index of every row of that table that remains; a table absent
    from ``kept`` lost no rows. Order is preserved.
    """
    if not kept:
        return tuple(records)
    index = {t: {old: new for new, old in enumerate(rows)} for t, rows in kept.items()}
    out = []
    for rec in records:
        table_index = index.get(rec.table)
        if table_index is None:
            out.append(rec)
        elif rec.row_index in table_index:
            out.append(dataclasses.replace(rec, row_index=table_index[rec.row_index]))
    return tuple(out)


def seal_evidence(cast_run: DeclaredCastRun | None, metrics: dict[str, Any]) -> Any:
    """Seal the run's quarantine LAST and put its counts into ``metrics``; returns the token."""
    if cast_run is None:
        return None
    metrics["declared_casting"], token = cast_run.finish()
    return token


def declared_integer_columns(plan: Any) -> dict[str, set[str]]:
    """Declared integer columns per table.

    pandas widens an Arrow integer column that holds a null to float64, which would turn a typed
    ``42`` into ``42.0`` (and round past 2**53) before the post-mask format stage. These columns
    join the lossless-typing set the adapters already keep exact for FK keys and anchors.
    """
    block = getattr(plan, "declared_casting", None)
    out: dict[str, set[str]] = {}
    for c in getattr(block, "columns", None) or ():
        if contract_model(c).logical_type == "integer":
            out.setdefault(c.table, set()).add(c.column)
    return out


@dataclass
class CastBatch:
    """One cast batch: the typed rows that continue, where they came from, and the raw framing.

    ``positions`` are absolute source rows (independent of how the source was partitioned);
    ``offset`` is the absolute row of ``raw``'s first row; ``raw`` is kept only for a table that
    can quarantine, because only then does a later stage need the original framed row.
    """

    table: pa.Table
    positions: list[int]
    offset: int
    raw: pa.Table | None


class DeclaredCastRun:
    """One run's declared casting: ledger, quarantine session and per-table whole-table batches.

    The kernel is batch-shaped (``cast_batch`` / ``format_batch``); ``pre_mask`` / ``post_mask``
    are the same kernel applied to a resident table as a single batch, so a route that streams
    chunks and a route that holds the table cannot diverge.
    """

    def __init__(self, plan: Any, runtime: Any = None) -> None:
        self._tables = _compile(plan)
        self._policy_block = dict(plan.quarantine) if plan.quarantine else None
        self._runtime = runtime
        self._run_id = (
            (getattr(runtime, "run_id", None) or secrets.token_hex(16)) if runtime else ""
        )
        self._ledger = K.CastIssueLedger()
        self._whole: dict[str, CastBatch] = {}
        self._session: P.QuarantineSession | None = None
        self._receipt: P.QuarantineReceipt | None = None
        self._written: set[tuple[str, int]] = set()

    # -- public entry points -------------------------------------------------------------------

    def tables(self) -> frozenset[str]:
        return frozenset(self._tables)

    def format_tables(self) -> frozenset[str]:
        """Tables with at least one output format, i.e. those that need a post-mask stage."""
        return frozenset(t for t, cols in self._tables.items() if any(c.fmt for c in cols))

    def cast_batch(
        self, table: str, src: pa.Table | pa.RecordBatch, *, offset: int = 0, record: bool = True
    ) -> CastBatch:
        """Cast one batch (``offset`` = absolute row of its first row) and drop quarantined rows.

        ``record=False`` makes the same decisions without touching the ledger or the quarantine
        sink, for a route that re-reads a source in several passes: only one pass may count.
        """
        if isinstance(src, pa.RecordBatch):
            src = pa.Table.from_batches([src])
        cols = self._tables[table]
        ledger = self._ledger if record else K.CastIssueLedger()
        positions = list(range(offset, offset + src.num_rows))
        replaced = {col.name: self._cast_column(table, col, src, positions, ledger) for col in cols}
        names = src.column_names
        typed = pa.table({n: replaced.get(n, src.column(n)) for n in names})
        raw = src if any(c.policy == "quarantine" for c in cols) else None
        dropped = [i for i, p in enumerate(positions) if ledger.is_quarantined(table, p)]
        if not dropped:
            return CastBatch(typed, positions, offset, raw)
        if record:
            self._write_rows(table, [positions[i] for i in dropped], raw, offset)
        gone = set(dropped)
        keep = [i for i in range(src.num_rows) if i not in gone]
        return CastBatch(
            typed.take(pa.array(keep, pa.int64())), [positions[i] for i in keep], offset, raw
        )

    def format_batch(
        self, table: str, out: pa.Table, batch: CastBatch, *, record: bool = True
    ) -> tuple[pa.Table, list[int] | None]:
        """Render a masked batch; returns it and, when rows were removed, the kept indices."""
        cols = self._tables[table]
        if not any(c.fmt is not None for c in cols):
            return out, None
        ledger = self._ledger if record else K.CastIssueLedger()
        positions = batch.positions
        replaced: dict[str, pa.Array] = {}
        for col in cols:
            if col.fmt is None:
                continue
            values = out.column(col.name).to_pylist()
            rendered: list[Any] = [None] * len(values)
            for i, value in enumerate(values):
                if value is None:
                    continue
                result = col.fmt(value)
                if result.ok:
                    rendered[i] = result.value
                else:
                    self._disposition(table, col, positions[i], "format", result.reason, ledger)
            replaced[col.name] = pa.array(
                rendered, type=pa.binary() if col.model.output_format.binary else pa.string()
            )
        formatted = pa.table({n: replaced.get(n, out.column(n)) for n in out.column_names})
        dropped = [i for i, p in enumerate(positions) if ledger.is_quarantined(table, p)]
        if not dropped:
            return formatted, None
        if record:
            self._write_rows(table, [positions[i] for i in dropped], batch.raw, batch.offset)
        gone = set(dropped)
        keep = [i for i in range(out.num_rows) if i not in gone]
        batch.positions = [positions[i] for i in keep]
        return formatted.take(pa.array(keep, pa.int64())), keep

    def pre_mask(self, sources: Mapping[str, Any]) -> dict[str, Any]:
        from decoy_engine.execution._pipeline_sources import materialize_source

        out = dict(sources)
        for table in self._tables:
            if table in sources:
                self._whole[table] = self.cast_batch(table, materialize_source(sources[table]))
                out[table] = self._whole[table].table
        return out

    def post_mask(
        self, outputs: Mapping[str, pa.Table], sources: Mapping[str, Any]
    ) -> tuple[dict[str, pa.Table], dict[str, Any], dict[str, list[int]]]:
        """Formatted outputs, companion sources, and per-table kept-row maps (old index per new)."""
        new_out, new_src, kept = dict(outputs), dict(sources), {}
        for table in self._tables:
            if table in outputs and table in self._whole:
                table_out, keep = self.format_batch(table, outputs[table], self._whole[table])
                new_out[table] = table_out
                if keep is not None:
                    kept[table] = keep
                    if isinstance(sources.get(table), pa.Table):
                        new_src[table] = sources[table].take(pa.array(keep, pa.int64()))
        return new_out, new_src, kept

    def rendered_schema(self, table: str, schema: pa.Schema) -> pa.Schema:
        """``schema`` with every formatted column typed as its rendered form (text or bytes)."""
        out = schema
        for col in self._tables.get(table, ()):
            if col.fmt is None or col.name not in schema.names:
                continue
            kind = pa.binary() if col.model.output_format.binary else pa.string()
            out = out.set(out.get_field_index(col.name), pa.field(col.name, kind))
        return out

    def align_sources(
        self, sources: Mapping[str, Any], outputs: Mapping[str, pa.Table]
    ) -> dict[str, Any]:
        """Typed companion rows for tables whose chunks were cast and rendered inside a lane:
        the same rows, in the same order, as the lane published (no counting, no sink)."""
        out = dict(sources)
        for table in self._tables:
            src = sources.get(table)
            if not isinstance(src, pa.Table) or table not in outputs:
                continue
            batch = self.cast_batch(table, src, record=False)
            keep = [
                i
                for i, p in enumerate(batch.positions)
                if not self._ledger.is_quarantined(table, p)
            ]
            out[table] = batch.table.take(pa.array(keep, pa.int64()))
        return out

    def load_table(self, table: str, src: Any) -> Any:
        """``src`` cast as one whole-table batch when ``table`` is declared, else unchanged."""
        return self.pre_mask({table: src})[table] if table in self._tables else src

    def render_table(
        self, table: str, out: pa.Table, records: Any = ()
    ) -> tuple[pa.Table, tuple[Any, ...]]:
        """``out`` rendered, and the row errors re-pointed at the rows that remain."""
        if table not in self._tables:
            return out, tuple(records)
        rendered, _, kept = self.post_mask({table: out}, {})
        return rendered[table], remap_row_errors(records, kept)

    def finish(self) -> tuple[dict[str, Any], P.OpaqueRetrievalToken | None]:
        """Seal LAST: callers invoke this after every failure-capable validation, and ``abort``
        still discards the sealed artifact if anything after it fails."""
        evidence = self._ledger.counts(completeness=K.COMPLETE)
        token = None
        if self._session is not None:
            self._receipt = self._runtime.sink.seal(self._session)
            self._session = None
            evidence["quarantine"] = self._receipt.to_evidence()
            token = self._runtime.sink.get_retrieval_token(
                self._receipt,
                P.QuarantineAccessContext(owner=self._runtime.owner, run_id=self._run_id),
            )
        return evidence, token

    def abort(self) -> None:
        """Remove every trace of the run's quarantine: the open session or an already sealed one."""
        if self._session is None and self._receipt is None:
            return
        store = self._runtime.sink
        if self._session is not None:
            store.abort(self._session)
            self._session = None
        if self._receipt is not None:
            store.discard(self._receipt)
            self._receipt = None

    def partial_counts(self) -> dict[str, Any]:
        return self._ledger.counts(completeness=K.PARTIAL)

    # -- internals ----------------------------------------------------------------------------------

    def _cast_column(
        self, table: str, col: _Col, src: pa.Table, positions: list[int], ledger: K.CastIssueLedger
    ) -> pa.Array:
        self._check_physical(col, src.schema.field(col.name).type)
        values = src.column(col.name).to_pylist()
        out: list[Any] = [None] * len(values)
        for i, raw in enumerate(values):
            if raw is None:
                continue
            if col.sentinels and _is_sentinel(col, raw):
                ledger.add_sentinel(table, col.name)
                continue
            result = col.parse(raw)
            if result.ok:
                out[i] = result.value
            else:
                self._disposition(table, col, positions[i], "parse", result.reason, ledger)
        return pa.array(out, type=arrow_type(col.model))

    @staticmethod
    def _check_physical(col: _Col, dtype: pa.DataType) -> None:
        if col.binary:
            ok = (
                pa.types.is_binary(dtype)
                or pa.types.is_large_binary(dtype)
                or pa.types.is_fixed_size_binary(dtype)
            )
        else:
            ok = pa.types.is_string(dtype) or pa.types.is_large_string(dtype)
        if not ok:
            from decoy_engine.execution._errors import DeclaredCastRouteError

            raise DeclaredCastRouteError(
                code=reasons.route.DECLARED_CAST_INPUT_TYPE_INCOMPATIBLE,
                message=f"column {col.name!r} arrived with a physical type its declared contract cannot parse.",
            )

    def _disposition(
        self,
        table: str,
        col: _Col,
        position: int,
        stage: str,
        reason: Any,
        ledger: K.CastIssueLedger,
    ) -> None:
        ledger.add(K.CastIssue(table, col.name, position, stage, reason, col.policy))
        if col.policy == "fail":
            from decoy_engine.execution._errors import DeclaredCastValueError

            raise DeclaredCastValueError(
                code=str(reason), counts=ledger.counts(completeness=K.PARTIAL)
            )

    # -- quarantine --------------------------------------------------------------------------------

    def _write_rows(
        self, table: str, positions: list[int], raw: pa.Table | None, offset: int
    ) -> None:
        store = self._runtime.sink
        if self._session is None:
            self._session = store.begin(
                P.QuarantineRun(owner=self._runtime.owner, run_id=self._run_id),
                P.QuarantinePolicy.model_validate(self._policy_block),
            )
        for pos in positions:
            if (table, pos) in self._written:
                continue
            self._written.add((table, pos))
            row = raw.slice(pos - offset, 1).to_pylist()[0] if raw is not None else {}
            store.write_row(
                self._session,
                P.QuarantineEnvelope(table, pos, self._ledger.row_reasons(table, pos), row),
            )


def _is_sentinel(col: _Col, raw: Any) -> bool:
    if col.binary:
        return isinstance(raw, (bytes, bytearray)) and bytes(raw) in col.sentinels
    return isinstance(raw, str) and raw in col.sentinels
