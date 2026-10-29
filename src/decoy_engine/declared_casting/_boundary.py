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

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine.contracts import declared_casting as C  # noqa: N812 -- short alias for the contract namespace
from decoy_engine.declared_casting import _catalog
from decoy_engine.declared_casting import _counts as K  # noqa: N812 -- short alias for the contract namespace
from decoy_engine.declared_casting import _quarantine_protocol as P  # noqa: N812 -- short alias for the contract namespace
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


class DeclaredCastRun:
    """State of one run's declared casting: ledger, originals, positions and the quarantine session."""

    def __init__(self, plan: Any, runtime: Any = None) -> None:
        self._tables = _compile(plan)
        self._policy_block = dict(plan.quarantine) if plan.quarantine else None
        self._runtime = runtime
        self._ledger = K.CastIssueLedger()
        self._originals: dict[str, pa.Table] = {}
        self._positions: dict[str, list[int]] = {}
        self._session: P.QuarantineSession | None = None
        self._receipt: P.QuarantineReceipt | None = None
        self._written: set[tuple[str, int]] = set()

    # -- public entry points -------------------------------------------------------------------

    def tables(self) -> frozenset[str]:
        return frozenset(self._tables)

    def pre_mask(self, sources: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(sources)
        for table, cols in self._tables.items():
            if table in sources:
                out[table] = self._pre_table(table, cols, sources[table])
        return out

    def post_mask(
        self, outputs: Mapping[str, pa.Table], sources: Mapping[str, Any]
    ) -> tuple[dict[str, pa.Table], dict[str, Any], dict[str, list[int]]]:
        """Formatted outputs, companion sources, and per-table kept-row maps (old index per new)."""
        new_out, new_src, kept = dict(outputs), dict(sources), {}
        for table, cols in self._tables.items():
            if table in outputs and any(c.fmt is not None for c in cols):
                table_out, keep = self._post_table(table, cols, outputs[table])
                new_out[table] = table_out
                if keep is not None:
                    kept[table] = keep
                    if isinstance(sources.get(table), pa.Table):
                        new_src[table] = sources[table].take(pa.array(keep, pa.int64()))
        return new_out, new_src, kept

    def finish(self) -> tuple[dict[str, Any], P.OpaqueRetrievalToken | None]:
        evidence = self._ledger.counts(completeness=K.COMPLETE)
        token = None
        if self._session is not None:
            self._receipt = self._runtime.sink.seal(self._session)
            self._session = None
            evidence["quarantine"] = self._receipt.to_evidence()
            token = self._runtime.sink.get_retrieval_token(
                self._receipt, P.QuarantineAccessContext(owner=self._runtime.owner)
            )
        return evidence, token

    def abort(self) -> None:
        if self._session is not None:
            store = self._runtime.sink
            store.abort(self._session)
            self._session = None

    def partial_counts(self) -> dict[str, Any]:
        return self._ledger.counts(completeness=K.PARTIAL)

    # -- pre-mask -----------------------------------------------------------------------------------

    def _pre_table(self, table: str, cols: list[_Col], src: Any) -> pa.Table:
        from decoy_engine.execution._pipeline_sources import materialize_source

        src = materialize_source(src)
        if any(c.policy == "quarantine" for c in cols):
            self._originals[table] = src  # only a quarantining table needs its raw rows later
        self._positions[table] = list(range(src.num_rows))
        replaced: dict[str, pa.Array] = {}
        for col in cols:
            replaced[col.name] = self._cast_column(table, col, src)
        quarantined = sorted(r for t, r in self._ledger.quarantined_rows() if t == table)
        names = src.column_names
        typed = pa.table({n: replaced.get(n, src.column(n)) for n in names})
        if not quarantined:
            return typed
        self._write_rows(table, quarantined)
        dropped = set(quarantined)
        keep = [i for i in range(src.num_rows) if i not in dropped]
        self._positions[table] = keep
        return typed.take(pa.array(keep, pa.int64()))

    def _cast_column(self, table: str, col: _Col, src: pa.Table) -> pa.Array:
        self._check_physical(col, src.schema.field(col.name).type)
        values = src.column(col.name).to_pylist()
        out: list[Any] = [None] * len(values)
        for i, raw in enumerate(values):
            if raw is None:
                continue
            if col.sentinels and _is_sentinel(col, raw):
                self._ledger.add_sentinel(table, col.name)
                continue
            result = col.parse(raw)
            if result.ok:
                out[i] = result.value
            else:
                self._disposition(table, col, i, "parse", result.reason)
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

    def _disposition(self, table: str, col: _Col, row: int, stage: str, reason: Any) -> None:
        pos = self._positions[table][row]
        self._ledger.add(K.CastIssue(table, col.name, pos, stage, reason, col.policy))
        if col.policy == "fail":
            from decoy_engine.execution._errors import DeclaredCastValueError

            raise DeclaredCastValueError(
                code=str(reason), counts=self._ledger.counts(completeness=K.PARTIAL)
            )

    # -- post-mask ----------------------------------------------------------------------------------

    def _post_table(
        self, table: str, cols: list[_Col], out: pa.Table
    ) -> tuple[pa.Table, list[int] | None]:
        positions = self._positions[table]
        replaced: dict[str, pa.Array] = {}
        before = {r for t, r in self._ledger.quarantined_rows() if t == table}
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
                    self._disposition(table, col, i, "format", result.reason)
            replaced[col.name] = pa.array(
                rendered, type=pa.binary() if col.model.output_format.binary else pa.string()
            )
        formatted = pa.table({n: replaced.get(n, out.column(n)) for n in out.column_names})
        fresh = sorted({r for t, r in self._ledger.quarantined_rows() if t == table} - before)
        if not fresh:
            return formatted, None
        self._write_rows(table, fresh)
        drop = set(fresh)
        keep = [i for i, pos in enumerate(positions) if pos not in drop]
        self._positions[table] = [positions[i] for i in keep]
        return formatted.take(pa.array(keep, pa.int64())), keep

    # -- quarantine --------------------------------------------------------------------------------

    def _write_rows(self, table: str, positions: list[int]) -> None:
        store = self._runtime.sink
        if self._session is None:
            self._session = store.begin(
                P.QuarantineRun(owner=self._runtime.owner, run_id=""),
                P.QuarantinePolicy.model_validate(self._policy_block),
            )
        original = self._originals[table]
        for pos in positions:
            if (table, pos) in self._written:
                continue
            self._written.add((table, pos))
            row = original.slice(pos, 1).to_pylist()[0]
            store.write_row(
                self._session,
                P.QuarantineEnvelope(table, pos, self._ledger.row_reasons(table, pos), row),
            )


def _is_sentinel(col: _Col, raw: Any) -> bool:
    if col.binary:
        return isinstance(raw, (bytes, bytearray)) and bytes(raw) in col.sentinels
    return isinstance(raw, str) and raw in col.sentinels
