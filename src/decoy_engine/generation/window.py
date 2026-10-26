"""`generate_rows`: a windowed, admitted, random-access read of one generate table (GEN-API Slice 0).

Returns rows `[offset, offset + count)` of one table, byte-identical to the same absolute
positions of a full `generate_tables` run, for Class A columns only (see `_window_admission`).
Row `i` of a Class A column is a pure function of `(column seed, i)`: the established
counter-based pattern for a seekable, resumable stream (Salmon et al., "Parallel random numbers:
as easy as 1, 2, 3", SC 2011), realized here with the engine's per-row reseed instead of Philox.

Offset idiom, per family (the correctness crux of a windowed read):

- statistical numeric / categorical / datetime / freetext: seeded `col_seed + offset + k`
  (`sample_column(start=offset)`);
- a CONDITIONAL statistical categorical: two indices. The per-row seed is the absolute index,
  while `parent_values` is indexed by the LOCAL window position `k`, so the parent window handed
  in is exactly `count` long and aligned to the window. A single absolute index into a
  count-long parent list would hit `sample_column`'s out-of-range fallback and silently draw the
  wrong conditioned value;
- sequence and per-row Faker: the absolute positional index (`next_batch(offset, size)`);
- windowed_date: the absolute row number via `row_offset`, over the window's anchor values.

A request is drawn in chunks. Each chunk is itself a window at its own absolute offset, so the
chunk size never changes a value, and the optional byte budget and deadline are checked between
chunks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.fileaccess import FileAccess, entry_point
from decoy_engine.generation._window_admission import (
    CODE_NOT_CLASS_A,
    CODE_TIMEOUT,
    CODE_WINDOW_INVALID,
    DEFAULT_MAX_CELL_CHARS,
    GenerationAdmissionError,
    GenerationWindowError,
    GenerationWindowTimeoutError,
    WindowAdmission,
    classify_window_table,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from faker import Faker

    from decoy_engine.generation._column_streams import ColumnStream

DEFAULT_CHUNK_ROWS = 4_096
# Row positions are packed into 8 bytes by the per-row derivations.
_MAX_POSITION = 2**63

__all__ = [
    "GeneratedWindow",
    "classify_window",
    "classify_window_from_generation_plan",
    "generate_rows",
]


@dataclass(frozen=True)
class GeneratedWindow:
    """The rows served, with how far a byte budget let the read go."""

    table: pa.Table
    offset: int
    rows_requested: int
    rows_emitted: int
    bytes_emitted: int
    capped: bool


def _validate_window(offset: Any, count: Any) -> None:
    for name, value in (("offset", offset), ("count", count)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise GenerationWindowError(
                code=CODE_WINDOW_INVALID, message=f"{name} must be a non-negative int"
            )
    if offset + count > _MAX_POSITION:
        raise GenerationWindowError(
            code=CODE_WINDOW_INVALID, message="offset + count exceeds the addressable row range"
        )


def classify_window(
    plan: Any,
    table: str,
    *,
    max_cell_chars: int = DEFAULT_MAX_CELL_CHARS,
    window_end: int = _MAX_POSITION,
    instance_default_locale: str | None = None,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
) -> WindowAdmission:
    """Class A / Class B verdict for `table`; raises a coded `GenerationAdmissionError` on refusal."""
    from decoy_engine.generation._plan_entry import _prepare

    prepared = _prepare(plan, "classify_window")
    return classify_window_table(
        prepared.config,
        table,
        statistical_specs=prepared.legacy_kwargs["statistical_specs"],
        max_cell_chars=max_cell_chars,
        window_end=window_end,
        instance_default_locale=instance_default_locale,
        provider_snapshot=provider_snapshot,
    )


def classify_window_from_generation_plan(
    generation_plan: Any,
    table: str,
    *,
    max_cell_chars: int = DEFAULT_MAX_CELL_CHARS,
    window_end: int = _MAX_POSITION,
    instance_default_locale: str | None = None,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
) -> WindowAdmission:
    """`classify_window` for a caller that holds only the compiled `GenerationPlan`."""
    from decoy_engine.generation._plan_entry import _prepare_pinned

    prepared = _prepare_pinned(generation_plan, "classify_window_from_generation_plan")
    return classify_window_table(
        prepared.config,
        table,
        statistical_specs=prepared.legacy_kwargs["statistical_specs"],
        max_cell_chars=max_cell_chars,
        window_end=window_end,
        instance_default_locale=instance_default_locale,
        provider_snapshot=provider_snapshot,
    )


def _positional(
    stream: ColumnStream,
) -> Callable[[int, int, Mapping[str, list[Any]]], list[Any]]:
    """A stream that already takes the absolute position, adapted to the window-draw shape."""

    def draw(off: int, n: int, _siblings: Mapping[str, list[Any]]) -> list[Any]:
        return stream.next_batch(off, n)

    return draw


def _build_streams(
    cols: list[dict[str, Any]],
    *,
    table: str,
    seed: int,
    derive_key: Any,
    instance_default_locale: str | None,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None,
    specs: Mapping[tuple[str, str], Any],
) -> dict[str, Callable[[int, int, Mapping[str, list[Any]]], list[Any]]]:
    """One window-drawing callable per column, built once and reused for every chunk."""
    from decoy_engine.generation._column_streams import (
        PerRowFakerStream,
        SequenceStream,
        WindowedDateStream,
    )
    from decoy_engine.generation.statistical import sample_column
    from decoy_engine.generators.derivation import GenDeriveContext

    draws: dict[str, Callable[[int, int, Mapping[str, list[Any]]], list[Any]]] = {}
    for col in cols:
        name, kind = col["name"], col.get("type")
        if kind == "sequence":
            draws[name] = _positional(SequenceStream(col))
        elif kind == "faker":
            ctx = GenDeriveContext.for_column(
                derive_key=derive_key, column_config=col, fallback_seed=seed
            )
            draws[name] = _positional(
                PerRowFakerStream(col, ctx, seed, instance_default_locale, provider_snapshot)
            )
        elif kind == "statistical":
            spec = specs[(table, name)]
            digest = f"sha256:{spec.snapshot_digest}" if spec.snapshot_digest else None
            col_seed = GenDeriveContext.for_column(
                derive_key=derive_key,
                column_config=col,
                fallback_seed=seed,
                snapshot_content_digest=digest,
            ).base_int("np")

            def draw_stat(
                off: int,
                n: int,
                sib: Mapping[str, list[Any]],
                spec: Any = spec,
                col_seed: int = col_seed,
            ) -> list[Any]:
                # Two-index contract: seed by the absolute `off`, parent by the local position.
                parent = sib[spec.condition_on] if spec.condition_on is not None else None
                if parent is not None and len(parent) != n:
                    raise GenerationWindowError(
                        code=CODE_WINDOW_INVALID,
                        message="parent window is not aligned to the requested window",
                    )
                return sample_column(spec, n, col_seed=col_seed, parent_values=parent, start=off)

            draws[name] = draw_stat
        elif kind == "windowed_date":
            wd = WindowedDateStream(col, seed)

            def draw_windowed(
                off: int, n: int, sib: Mapping[str, list[Any]], s: WindowedDateStream = wd
            ) -> list[Any]:
                return s.next_batch_from(off, n, sib)

            draws[name] = draw_windowed
        else:  # the classifier already routed every other family to Class B
            raise GenerationWindowError(
                code=CODE_NOT_CLASS_A, message=f"column {name!r}: {kind!r} is not Class A"
            )
    return draws


def _row_bytes(tbl: pa.Table) -> np.ndarray:
    """Arrow data bytes each row costs: value width plus the 4-byte offset of a string cell."""
    total = np.zeros(tbl.num_rows, dtype=np.int64)
    for column in tbl.columns:
        if pa.types.is_string(column.type) or pa.types.is_large_string(column.type):
            sizes = pc.binary_length(column)  # type: ignore[attr-defined, unused-ignore]
            lengths = sizes.fill_null(0).to_numpy(zero_copy_only=False)
            total += lengths.astype(np.int64) + 4
        else:
            total += max(column.type.bit_width // 8, 1) if hasattr(column.type, "bit_width") else 8
    return total


@entry_point
def generate_rows(
    plan: Any,
    *,
    table: str,
    offset: int,
    count: int,
    seed: int,
    max_cell_chars: int = DEFAULT_MAX_CELL_CHARS,
    budget_bytes: int | None = None,
    deadline_s: float | None = None,
    chunk_rows: int = DEFAULT_CHUNK_ROWS,
    derive_key: Any = None,
    instance_default_locale: str | None = None,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
    file_access: FileAccess | None = None,
) -> GeneratedWindow:
    """Rows `[offset, offset + count)` of `table`, identical to a full run at those positions.

    `seed` is the job seed the full run would read from the plan's configuration; it is an
    explicit input here, not something the plan pins. The table must be Class A
    (`GenerationAdmissionError` otherwise, refusals before Class B). Offsets past the declared
    `row_count` are served: Class A values do not depend on the table length.

    `budget_bytes` caps the Arrow data bytes emitted (see `_row_bytes`): the read stops at the
    last whole row that fits and reports `capped=True`; the wire encoder remains the authority on
    encoded size. `deadline_s` is a wall-clock cap in seconds for the whole call, checked between
    chunks, raising `GenerationWindowTimeoutError`.
    """
    from decoy_engine.generation._plan_entry import _prepare
    from decoy_engine.plan._seed import _normalize_job_seed_int

    _validate_window(offset, count)
    if isinstance(chunk_rows, bool) or not isinstance(chunk_rows, int) or chunk_rows < 1:
        raise GenerationWindowError(code=CODE_WINDOW_INVALID, message="chunk_rows must be >= 1")
    job_seed = _normalize_job_seed_int({"global_settings": {"seed": seed}})
    started = time.monotonic()

    prepared = _prepare(plan, "generate_rows")
    specs = prepared.legacy_kwargs["statistical_specs"]
    verdict = classify_window_table(
        prepared.config,
        table,
        statistical_specs=specs,
        max_cell_chars=max_cell_chars,
        window_end=offset + count,
        instance_default_locale=instance_default_locale,
        provider_snapshot=provider_snapshot,
    )
    if not verdict.is_class_a:
        detail = ", ".join(f"{c}:{r}" for c, r in verdict.class_b_reasons)
        raise GenerationAdmissionError(
            code=CODE_NOT_CLASS_A,
            message=f"table is Class B and cannot be served by a windowed read ({detail})",
            table=table,
            column=None,
            prop=None,
        )

    cols = next(t["generate_columns"] for t in prepared.config["tables"] if t.get("name") == table)
    draws = _build_streams(
        cols,
        table=table,
        seed=job_seed,
        derive_key=derive_key,
        instance_default_locale=instance_default_locale,
        provider_snapshot=provider_snapshot,
        specs=specs,
    )
    names = [c["name"] for c in cols]
    acc: dict[str, list[Any]] = {n: [] for n in names}
    emitted = used = 0
    capped = False
    done = 0
    while done < count and not capped:
        if deadline_s is not None and time.monotonic() - started > deadline_s:
            raise GenerationWindowTimeoutError(
                code=CODE_TIMEOUT, message=f"deadline of {deadline_s}s passed after {emitted} rows"
            )
        size = min(chunk_rows, count - done)
        window: dict[str, list[Any]] = {}
        for name in names:  # declared order: a conditional column reads its earlier parent
            window[name] = draws[name](offset + done, size, window)
        keep = size
        if budget_bytes is not None:
            running = np.cumsum(_row_bytes(pa.table(window)))
            keep = int(np.searchsorted(running, budget_bytes - used, side="right"))
            capped = keep < size
            used += int(running[keep - 1]) if keep else 0
        for name in names:
            acc[name].extend(window[name][:keep])
        emitted += keep
        done += size
    # Re-check after the last chunk: the in-loop guard runs only BEFORE each chunk, so a final
    # chunk (or a zero-row request) that overruns would otherwise return successfully.
    if deadline_s is not None and time.monotonic() - started > deadline_s:
        raise GenerationWindowTimeoutError(
            code=CODE_TIMEOUT, message=f"deadline of {deadline_s}s passed after {emitted} rows"
        )
    result = pa.table(acc) if emitted else pa.table({n: pa.array([], pa.null()) for n in names})
    return GeneratedWindow(
        result,
        offset,
        count,
        emitted,
        used if budget_bytes is not None else int(_row_bytes(result).sum()),
        capped,
    )
