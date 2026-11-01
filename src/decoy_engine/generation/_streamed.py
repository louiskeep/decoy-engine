"""The single generation core: batches in, a bounded sink or an in-memory table out.

`run_generation` is the one algorithm behind both public routes. Resident `generate_tables` runs
it into an accumulating in-memory consumer and concatenates; `generate_tables_streamed` runs it
into a bounded transactional sink when the sink and the byte budget allow. Because both routes
consume the same batches from the same persistent per-column streams (`_column_streams.py`), the
bytes are identical by construction. The semantic decision (which values) is made once from the
plan (`_admission.py`); this module only decides HOW to materialize them.

Execution mode, for an eligible plan:

- a bounded sink is present and the streaming peak fits the budget: stream batches to the sink;
- otherwise accumulate the same batches in memory, if the whole-table accumulator fits;
- if even that exceeds the ceiling, fail with `GenerationBudgetExceeded`. The core never emits
  different values to make a run fit, and it never switches mode after the first batch (a breach
  mid-run aborts the sink transaction).

The legacy whole-table generator (`synthesize._generate_tables_from_config`) runs only for plans
with a semantically ineligible column.

Memory is enforced by a run-scoped `GenerationBudgetLedger` (`_budget_ledger.py`): each bounded
stage reserves before it allocates, and a plan with a column that has no defensible bound is
rejected before any provider is invoked, on every path. The publish session is
`execution._chunked_output_sink.OutputPublish`, the established stage-then-rename commit that
aborts on any failure.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine import reasons
from decoy_engine.generation._admission import PlanAdmission, classify_plan
from decoy_engine.generation._bounds import (
    accumulator_bytes,
    batch_buffer_bytes,
    compute_payload_bounds,
    faker_pool_bytes,
    key_pool_bytes,
    legacy_table_bytes,
    sink_buffer_bytes,
)
from decoy_engine.generation._budget_ledger import (
    GenerationBudgetExceeded,
    GenerationBudgetLedger,
    GenerationUnboundedError,
)
from decoy_engine.generation._column_streams import (
    ColumnStream,
    KeyPool,
    draw_batch,
    make_column_stream,
)
from decoy_engine.generation._faker_pool import pool_eligible
from decoy_engine.generation.pool._runtime_pool_size import DEFAULT_POOL_SIZE
from decoy_engine.progress import get_active_channel

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    from faker import Faker

DEFAULT_BATCH_ROWS = 65_536
_MIN_BATCH_ROWS = 1_024

__all__ = [
    "DEFAULT_BATCH_ROWS",
    "GenerationResult",
    "is_bounded_sink",
    "run_generation",
]


@dataclass(frozen=True)
class GenerationResult:
    """Outcome of one run. Metadata only when streamed; the tables when resident."""

    mode: str
    reason: str
    row_counts: dict[str, int]
    tables: dict[str, pa.Table] | None
    peak_reserved_bytes: int
    declines: tuple[str, ...] = field(default=())


def is_bounded_sink(sink: Any) -> bool:
    """The explicit capability marker. `isinstance(sink, TransactionalSink)` matches method
    names only, and the callable adapter materializes the whole stream, so it proves nothing."""
    return sink is not None and getattr(sink, "bounded_write_batches", False) is True


# A caller-supplied layout is held as a Python sequence of ints: an 8-byte slot plus, for a
# non-cached value, a 28-byte int object. Charged before the run so it is in the ledger, never
# silently outside it.
_LAYOUT_ENTRY_BYTES = 36


@dataclass(frozen=True)
class _Layout:
    """One table's batch layout without materializing it.

    The default layout is arithmetic (`rows`, `batch_rows`), so a 10M-row table at `batch_rows=1`
    costs O(1) here instead of a 10M-entry list allocated before any budget check. A supplied
    layout is kept as the caller's own sequence (never copied) and validated in one streaming
    pass, so the maximum batch and the bookkeeping charge are known before anything is reserved.
    """

    rows: int
    batch_rows: int
    max_batch: int
    supplied: Sequence[int] | None = None
    bookkeeping_bytes: int = 0

    def sizes(self) -> Iterator[int]:
        if self.supplied is not None:
            yield from self.supplied
            return
        full, rest = divmod(self.rows, self.batch_rows)
        for _ in range(full):
            yield self.batch_rows
        if rest:
            yield rest


def _make_layout(
    name: str,
    rows: int,
    batch_rows: int,
    partition: Callable[[str, int], Sequence[int]] | None,
) -> _Layout:
    if partition is None:
        return _Layout(rows, batch_rows, min(rows, batch_rows))
    supplied = partition(name, rows)
    total = count = largest = 0
    for size in supplied:
        if size < 0:
            raise ValueError(f"partition for table {name!r} does not cover {rows} rows")
        total += size
        count += 1
        largest = max(largest, size)
    if total != rows:
        raise ValueError(f"partition for table {name!r} does not cover {rows} rows")
    return _Layout(rows, batch_rows, largest, supplied, count * _LAYOUT_ENTRY_BYTES)


def _table_order(config: dict[str, Any]) -> list[dict[str, Any]]:
    from decoy_engine.generation.synthesize import _topo_sort

    generate_by_name = {
        t["name"]: t for t in config.get("tables") or [] if t.get("generate_columns")
    }
    deps = {
        name: {c["reference_table"] for c in t["generate_columns"] if c.get("type") == "reference"}
        for name, t in generate_by_name.items()
    }
    return [generate_by_name[name] for name in _topo_sort(deps)]


def _payloads(table: dict[str, Any], bounds: Mapping[tuple[str, str], int | None]) -> list[int]:
    return [bounds.get((table["name"], c["name"])) or 0 for c in table["generate_columns"]]


def _referenced_keys(tables: Sequence[dict[str, Any]]) -> set[tuple[str, str]]:
    return {
        (c["reference_table"], c["reference_column"])
        for t in tables
        for c in t["generate_columns"]
        if c.get("type") == "reference"
    }


def _pooled_columns(
    table: dict[str, Any], instance_default_locale: str | None
) -> list[dict[str, Any]]:
    rows = int(table.get("row_count") or 0)
    return [
        c
        for c in table["generate_columns"]
        if c.get("type") == "faker"
        and pool_eligible(
            c.get("faker_type", "word"),
            c.get("locale") or instance_default_locale,
            rows,
            opted_out=c.get("pooled") is False,
        )
    ]


@dataclass
class _Plan:
    """Preflight figures for an eligible plan, all conservative upper bounds in bytes."""

    key_pools: int
    accumulators: int
    stage_peak: int
    sink_peak: int
    layouts: int = 0

    def streaming_peak(self) -> int:
        return self.layouts + self.key_pools + self.stage_peak + self.sink_peak

    def resident_peak(self) -> int:
        return self.layouts + self.key_pools + self.accumulators + self.stage_peak


def _preflight(
    tables: Sequence[dict[str, Any]],
    bounds: Mapping[tuple[str, str], int | None],
    batch_rows_of: Callable[[dict[str, Any]], int],
    instance_default_locale: str | None,
    layouts: int = 0,
) -> _Plan:
    referenced = _referenced_keys(tables)
    key_total = accumulators = stage_peak = sink_peak = 0
    for t in tables:
        rows = int(t.get("row_count") or 0)
        payloads = _payloads(t, bounds)
        batch = batch_rows_of(t)
        for c in t["generate_columns"]:
            if (t["name"], c["name"]) in referenced:
                key_total += key_pool_bytes(rows, bounds.get((t["name"], c["name"])) or 0)
        accumulators += accumulator_bytes(rows, payloads)
        pools = sum(
            faker_pool_bytes(DEFAULT_POOL_SIZE, bounds.get((t["name"], c["name"])) or 0)
            for c in _pooled_columns(t, instance_default_locale)
        )
        stage_peak = max(stage_peak, batch_buffer_bytes(payloads, batch) + pools)
        sink_peak = max(sink_peak, sink_buffer_bytes(payloads, batch))
    return _Plan(key_total, accumulators, stage_peak, sink_peak, layouts)


def _reject_unbounded(config: dict[str, Any], bounds: Mapping[tuple[str, str], int | None]) -> None:
    """Bounded ceiling: a column with no defensible bound fails the plan before any provider."""
    missing = [f"{t}.{c}" for (t, c), b in bounds.items() if b is None]
    if missing:
        raise GenerationUnboundedError(
            code=reasons.runtime.GENERATION_COLUMN_UNBOUNDED,
            message=(
                "no defensible value-width bound under a bounded ceiling for column(s): "
                + ", ".join(sorted(missing))
            ),
        )


def _fit_band() -> float:
    from decoy_engine.execution._mem_estimate import K_CALIBRATION_ERROR_BAND

    return float(K_CALIBRATION_ERROR_BAND)


def _fits(ledger: GenerationBudgetLedger, need: int) -> bool:
    if ledger.remaining_bytes is None:
        return True
    return need * (1 + _fit_band()) < ledger.remaining_bytes


def _check_width(arr: pa.Array, bound: int, column: str) -> None:
    if not (pa.types.is_string(arr.type) or pa.types.is_large_string(arr.type)):
        return
    longest = pc.max(pc.binary_length(arr)).as_py()  # type: ignore[attr-defined, unused-ignore]
    if longest is not None and longest > bound:
        raise GenerationBudgetExceeded(
            code=reasons.runtime.VALUE_EXCEEDS_DECLARED_BOUND,
            message=f"column {column!r} produced a value wider than its declared {bound}-byte bound.",
        )


def _table_batches(
    table: dict[str, Any],
    schema: pa.Schema,
    sizes: Iterable[int],
    streams: Sequence[ColumnStream],
    widths: Mapping[str, int],
    collectors: Mapping[str, KeyPool],
) -> Iterator[pa.RecordBatch]:
    offset = 0
    names = [c["name"] for c in table["generate_columns"]]
    for size in sizes:
        arrays = []
        # Post-null-mask values of the columns already emitted in THIS batch, which is what the
        # legacy path's `generated` mapping holds when a windowed_date column reads its anchor.
        emitted: dict[str, list[Any]] = {}
        for name, stream, field_ in zip(names, streams, schema, strict=True):
            values = draw_batch(stream, offset, size, emitted)
            emitted[name] = values
            arr = pa.array(values, type=field_.type)
            if name in widths:
                _check_width(arr, widths[name], name)
            if name in collectors:
                collectors[name].add(values)
            arrays.append(arr)
        yield pa.RecordBatch.from_arrays(arrays, schema=schema)
        offset += size


def run_generation(
    config: dict[str, Any],
    *,
    derive_key: Any = None,
    instance_default_locale: str | None = None,
    sink: Any = None,
    budget_bytes: int | None = None,
    batch_rows: int | None = None,
    partition: Callable[[str, int], Sequence[int]] | None = None,
    ledger: GenerationBudgetLedger | None = None,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
    legacy_kwargs: dict[str, Any] | None = None,
) -> GenerationResult:
    """Run a plan's configuration through the core. See the module docstring for the modes.

    ``budget_bytes=None`` and no ``ledger`` is the explicit unbounded mode: no byte ceiling is
    enforced and no bounded-memory guarantee is made. ``partition(table, rows)`` overrides the
    batch layout (used to prove route-independence over irregular partitions).
    """
    from decoy_engine.plan._seed import _normalize_job_seed_int

    resolved_batch = batch_rows
    if ledger is None:
        if budget_bytes is None:
            ledger = GenerationBudgetLedger(None)
        else:
            from decoy_engine.execution.out_of_core._budget import resolve_budget

            resolved = resolve_budget(budget_bytes)
            ledger = GenerationBudgetLedger(resolved.budget_bytes)
            resolved_batch = resolved_batch or resolved.batch_rows
    resolved_batch = resolved_batch or DEFAULT_BATCH_ROWS
    admission = classify_plan(config, provider_snapshot=provider_snapshot)
    tables = _table_order(config)
    # Bounds are only needed (and only defensible to demand) under a bounded ceiling.
    bounds = compute_payload_bounds(config, provider_snapshot) if ledger.bounded else {}
    if ledger.bounded:
        _reject_unbounded(config, bounds)
    if not admission.eligible:
        return _run_legacy(
            config,
            admission,
            tables,
            bounds,
            ledger,
            derive_key,
            instance_default_locale,
            provider_snapshot,
            legacy_kwargs or {},
        )

    layout = {
        t["name"]: _make_layout(t["name"], int(t.get("row_count") or 0), resolved_batch, partition)
        for t in tables
    }
    batch_of = {name: lay.max_batch for name, lay in layout.items()}
    layout_bytes = sum(lay.bookkeeping_bytes for lay in layout.values())
    plan = _preflight(
        tables, bounds, lambda t: batch_of[t["name"]], instance_default_locale, layout_bytes
    )
    stream_it = is_bounded_sink(sink) and _fits(ledger, plan.streaming_peak())
    if not stream_it and not _fits(ledger, plan.resident_peak()):
        raise GenerationBudgetExceeded(
            code=reasons.runtime.GENERATION_BUDGET_EXCEEDED,
            message=(
                f"resident generation needs {plan.resident_peak()} bytes at most; "
                f"{ledger.remaining_bytes} remain under the ceiling."
            ),
        )
    if sink is None:
        reason = "no_sink"
    elif not is_bounded_sink(sink):
        reason = "sink_not_bounded"
    else:
        reason = "eligible" if stream_it else "budget_does_not_fit_streaming"
    seed = _normalize_job_seed_int(config)
    enforce_widths = ledger.bounded
    out: dict[str, pa.Table] = {}
    counts: dict[str, int] = {}
    key_pools: dict[tuple[str, str], list[Any]] = {}
    referenced = _referenced_keys(tables)

    def _consume_table(
        t: dict[str, Any],
        schema: pa.Schema,
        lay: _Layout,
        collectors: dict[str, KeyPool],
        consume: Callable[[str, pa.Schema, Iterator[pa.RecordBatch]], None],
        stage: ExitStack,
    ) -> None:
        """Build one table's streams, drain them, and let them die on return.

        The streams (and any Faker pool they own) are locals of this frame so they are freed
        before the caller closes `stage` and releases their reservations. Holding them in the
        run-level loop kept table N's pool alive while table N+1's was built, with its charge
        already released.
        """
        name = t["name"]
        streams = [
            make_column_stream(
                c,
                table=name,
                rows=int(t.get("row_count") or 0),
                seed=seed,
                derive_key=derive_key,
                instance_default_locale=instance_default_locale,
                provider_snapshot=provider_snapshot,
                key_pools=key_pools,
                ledger=ledger,
                stage=stage,
                faker_payload=bounds.get((name, c["name"])),
            )
            for c in t["generate_columns"]
        ]
        widths: dict[str, int] = {}
        for c in t["generate_columns"]:
            width = bounds.get((name, c["name"]))
            if enforce_widths and c.get("type") == "faker" and width is not None:
                widths[c["name"]] = width
        consume(name, schema, _table_batches(t, schema, lay.sizes(), streams, widths, collectors))

    _pg = get_active_channel()

    def generate(consume: Callable[[str, pa.Schema, Iterator[pa.RecordBatch]], None]) -> None:
        with ExitStack() as run_stack:
            if layout_bytes:
                run_stack.callback(ledger.reserve("partition_layout", layout_bytes).release)
            for t in tables:
                name, rows = t["name"], int(t.get("row_count") or 0)
                payloads = _payloads(t, bounds)
                schema = admission.schema(name)
                collectors: dict[str, KeyPool] = {}
                for c in t["generate_columns"]:
                    if (name, c["name"]) in referenced:
                        run_stack.callback(
                            ledger.reserve(
                                f"parent_key_pool:{name}.{c['name']}",
                                key_pool_bytes(rows, bounds.get((name, c["name"])) or 0),
                            ).release
                        )
                        collectors[c["name"]] = KeyPool()
                with ExitStack() as stage:
                    stage.callback(
                        ledger.reserve(
                            f"batch_buffers:{name}", batch_buffer_bytes(payloads, batch_of[name])
                        ).release
                    )
                    if stream_it:
                        stage.callback(
                            ledger.reserve(
                                f"sink_buffer:{name}", sink_buffer_bytes(payloads, batch_of[name])
                            ).release
                        )
                    else:
                        # The accumulator is the output itself: charged for the rest of the run.
                        run_stack.callback(
                            ledger.reserve(
                                f"accumulator:{name}", accumulator_bytes(rows, payloads)
                            ).release
                        )
                    _pg_started = _pg.phase_start("generate", table=name, rows_in=rows)
                    _consume_table(t, schema, layout[name], collectors, consume, stage)
                    _pg.phase_finish("generate", _pg_started, table=name, rows_out=rows)
                for col_name, pool in collectors.items():
                    key_pools[(name, col_name)] = pool.seal()
                counts[name] = rows

    if stream_it:
        from decoy_engine.execution._chunked_output_sink import OutputPublish

        with OutputPublish(sink, True, False) as publish:
            publish.open()
            generate(lambda n, s, b: sink.write_batches(n, _pg.count_rows(n, b), schema=s))
            publish.commit()
        return GenerationResult("streamed", reason, counts, None, ledger.peak_bytes)

    def accumulate(name: str, schema: pa.Schema, batches: Iterator[pa.RecordBatch]) -> None:
        batch_list = list(_pg.count_rows(name, batches))
        out[name] = (
            pa.Table.from_batches(batch_list, schema=schema) if batch_list else schema.empty_table()
        )

    generate(accumulate)
    return GenerationResult("resident", reason, counts, out, ledger.peak_bytes)


def _run_legacy(
    config: dict[str, Any],
    admission: PlanAdmission,
    tables: Sequence[dict[str, Any]],
    bounds: Mapping[tuple[str, str], int | None],
    ledger: GenerationBudgetLedger,
    derive_key: Any,
    instance_default_locale: str | None,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None,
    legacy_kwargs: dict[str, Any],
) -> GenerationResult:
    """Whole-table generation for plans with a semantically ineligible column.

    Under a bounded ceiling the whole-table upper bound is reserved BEFORE the first provider
    runs. Produced faker widths are verified against their declared caps afterwards; that check
    is a tripwire (the allocation has happened), which is why only table-listed provider types
    have a bound at all.
    """
    from decoy_engine.generation.synthesize import _generate_tables_from_config

    if ledger.bounded:
        need = sum(
            legacy_table_bytes(int(t.get("row_count") or 0), _payloads(t, bounds)) for t in tables
        )
        if not _fits(ledger, need):
            raise GenerationBudgetExceeded(
                code=reasons.runtime.GENERATION_BUDGET_EXCEEDED,
                message=f"whole-table generation needs {need} bytes at most; "
                f"{ledger.remaining_bytes} remain under the ceiling.",
            )
        ledger.reserve("legacy_whole_table", need)
    out = _generate_tables_from_config(
        config,
        derive_key,
        instance_default_locale,
        provider_snapshot=provider_snapshot,
        **legacy_kwargs,
    )
    if ledger.bounded:
        for t in tables:
            for c in t["generate_columns"]:
                bound = bounds.get((t["name"], c["name"]))
                if c.get("type") == "faker" and bound is not None:
                    _check_width(
                        out[t["name"]].column(c["name"]).combine_chunks(), bound, c["name"]
                    )
    counts = {name: tbl.num_rows for name, tbl in out.items()}
    declines = tuple(f"{c.table}.{c.column}:{c.reason}" for c in admission.declines)
    return GenerationResult(
        "resident", f"legacy:{admission.reason()}", counts, out, ledger.peak_bytes, declines
    )
