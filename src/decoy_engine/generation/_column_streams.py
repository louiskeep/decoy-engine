"""Per-column persistent generators for the unified generation core.

Each stream is created once per column and advanced `size=batch` per call, never re-created per
batch and never drawn as a full-length array and sliced. A stream's output for rows
`[offset, offset+size)` is a pure function of the envelope and the global row offset, which is
what makes the output independent of how the rows are partitioned into batches. The draws are
the ones the whole-table generators in `synthesize.py` make (same `GenDeriveContext` roots, same
RNG families), consumed incrementally:

- sequence: no RNG, `start + step * global_i`;
- categorical: the index kernel keyed on the global row (`_kernel_select`), or, for a config the
  kernel mapping declines, one `random.Random(base_int("py"))` with `choices(k=batch)` per call;
- per-row faker: `seed_instance(row_int("faker", global_i))` then the provider;
- pooled faker: pool built once (Python), then the index kernel keyed on the global row;
- reference (no repair): parent key set materialized once; `random`/`weighted` draw through the
  index kernel, `sequential` is arithmetic, and a declined weight set keeps one `random.Random`;
- windowed_date: no RNG state; each row derives its own offset from the global row index and
  its own same-batch anchor value (`transforms/windowed_date.py`), so it needs the sibling batch;
- null mask: one `default_rng(base_int("np")).random(size)` per call.

A kernel draw is a pure function of the global row, so it carries no state across batches. The
remaining RNG-backed streams rely on sequential draws from one generator equalling one longer draw
(CPython `random.Random`, NumPy `Generator`). The route-independence tests assert both over
irregular partitions.
"""

from __future__ import annotations

import random
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any, Protocol

import numpy as np
import pandas as pd

from decoy_engine.generation import _faker_pool, _kernel_select
from decoy_engine.generation._bounds import faker_pool_bytes
from decoy_engine.generators.derivation import GenDeriveContext
from decoy_engine.internal.faker_setup import get_faker_providers, make_faker

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from faker import Faker

    from decoy_engine.generation._budget_ledger import GenerationBudgetLedger


class ColumnStream(Protocol):
    def next_batch(self, offset: int, size: int) -> list[Any]: ...


class WindowedDateStream:
    """A date within a window of a same-table anchor column emitted earlier in the same batch.

    The per-row offset RNG is re-derived from (job seed, column namespace, global row index), so
    the stream holds no draw state and a row's value cannot depend on how rows are batched. A
    null anchor propagates to null without a draw; an invalid one raises `WindowedDateError`
    (the shared `apply_windowed_date` owns both rules, so the legacy path behaves identically).
    """

    def __init__(self, col: dict[str, Any], seed: int) -> None:
        from decoy_engine.transforms.windowed_date import WindowedDateConfig

        cfg: dict[str, Any] = {"anchor": col.get("anchor", "")}
        for key in ("min_days", "max_days", "distribution"):
            if key in col:
                cfg[key] = col[key]
        self._config = WindowedDateConfig.from_dict(cfg)
        self._seed = seed.to_bytes(8, "big")
        self._namespace = f"windowed_date/{col.get('name', 'windowed_date_col')}"

    @property
    def anchor(self) -> str:
        return self._config.anchor

    def next_batch(self, offset: int, size: int) -> list[Any]:
        raise ValueError("windowed_date needs its anchor batch; call next_batch_from")

    def next_batch_from(
        self, offset: int, size: int, siblings: Mapping[str, list[Any]]
    ) -> list[Any]:
        from decoy_engine.transforms.windowed_date import apply_windowed_date

        # object dtype keeps every cell as the Python value the stream emitted (a str, a date,
        # or None); pandas inference would turn a null into NaN and an all-null batch into float.
        anchor = pd.Series(siblings[self._config.anchor], dtype=object)
        df = pd.DataFrame({self._config.anchor: anchor})
        return list(
            apply_windowed_date(self._config, df, self._seed, self._namespace, row_offset=offset)
        )


class KeyPool:
    """Insertion-order unique, None-dropped key set of one referenced parent column.

    Insertion order matters: `random.choice(ref_vals)` indexes into it, so a sorted or hashed
    order would change which parent a child row picks.
    """

    def __init__(self) -> None:
        self.keys: list[Any] = []
        self._seen: set[Any] = set()

    def add(self, values: list[Any]) -> None:
        for v in values:
            if v is None or v in self._seen:
                continue
            self._seen.add(v)
            self.keys.append(v)

    def seal(self) -> list[Any]:
        """Drop the membership set once the parent is complete; only the ordered keys remain."""
        self._seen = set()
        return self.keys


class SequenceStream:
    def __init__(self, col: dict[str, Any]) -> None:
        self._start = int(col.get("start", 1))
        self._step = int(col.get("step", 1))
        self._prefix = str(col.get("prefix", ""))
        self._suffix = str(col.get("suffix", ""))
        self._pad = int(col.get("pad_length", 0))

    def next_batch(self, offset: int, size: int) -> list[Any]:
        out: list[Any] = []
        for i in range(offset, offset + size):
            value = self._start + i * self._step
            body = str(value).zfill(self._pad) if self._pad > 0 else str(value)
            out.append(f"{self._prefix}{body}{self._suffix}")
        return out


def gather(
    indexer: _kernel_select.Indexer, choices: list[Any], offset: int, size: int
) -> list[Any]:
    """The choices an indexer picks for global rows `offset .. offset + size - 1`."""
    return [choices[i] for i in indexer.draw(offset, size).tolist()]


class CategoricalStream:
    """Uniform or weighted choice over `categories`.

    The kernel indexer is chosen once, from the configuration alone. When it declines (empty or
    non-list categories, or a weight set the CDF mapping cannot represent) the column keeps the
    Python RNG, so the backend never depends on the host.
    """

    def __init__(self, col: dict[str, Any], ctx: GenDeriveContext) -> None:
        self._cats = col.get("categories", ["Category A", "Category B"])
        self._weights = col.get("weights")
        admissible = isinstance(self._cats, (list, tuple))
        self._indexer = (
            _kernel_select.choice_indexer(ctx, len(self._cats), self._weights)
            if admissible
            else None
        )
        self._choices = list(self._cats) if admissible else []
        # Instance-local Random: same Mersenne Twister state init as random.seed, thread-safe.
        self._rng = random.Random(ctx.base_int("py")) if self._indexer is None else None

    def next_batch(self, offset: int, size: int) -> list[Any]:
        if self._indexer is not None:
            return gather(self._indexer, self._choices, offset, size)
        assert self._rng is not None  # noqa: S101 -- set whenever there is no indexer
        return self._rng.choices(self._cats, weights=self._weights, k=size)


class PerRowFakerStream:
    """Provider and Faker instance resolved once per column, then seeded per global row."""

    def __init__(
        self,
        col: dict[str, Any],
        ctx: GenDeriveContext,
        seed: int,
        instance_default_locale: str | None,
        provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None,
    ) -> None:
        from decoy_engine.generation.synthesize import _get_default_faker

        faker_type = col.get("faker_type", "word")
        locale = col.get("locale")
        raw_kwargs = col.get("faker_kwargs") or {}
        self._kwargs: dict[str, Any] = raw_kwargs if isinstance(raw_kwargs, dict) else {}
        self._ctx = ctx
        pre_seed: int | None
        if locale:
            self._inst = make_faker(locale)
            pre_seed = None
        elif instance_default_locale:
            self._inst = make_faker(instance_default_locale)
            pre_seed = seed
        else:
            self._inst = _get_default_faker()
            pre_seed = seed
        providers = get_faker_providers(self._inst, provider_snapshot=provider_snapshot)
        self._provider = providers.get(faker_type) or providers["word"]
        if pre_seed is not None:
            self._inst.seed_instance(pre_seed)

    def next_batch(self, offset: int, size: int) -> list[Any]:
        out: list[Any] = []
        for i in range(offset, offset + size):
            self._inst.seed_instance(self._ctx.row_int("faker", i))
            out.append(self._provider(**self._kwargs))
        return out


class PooledFakerStream:
    def __init__(self, selector: _faker_pool.PooledSelector) -> None:
        self._selector = selector

    def next_batch(self, offset: int, size: int) -> list[Any]:
        return self._selector.select(offset, size)


class ReferenceStream:
    """Reference column without cardinality repair, over an already-sealed parent key list."""

    def __init__(self, col: dict[str, Any], ctx: GenDeriveContext, ref_vals: list[Any]) -> None:
        self._vals = ref_vals
        self._distribution = col.get("distribution", "random")
        weights = col.get("weights")
        # Size mismatch falls back to uniform, as the whole-table generator does.
        self._weights = weights if weights and len(weights) == len(ref_vals) else None
        self._indexer: _kernel_select.Indexer | None = None
        if ref_vals and self._distribution != "sequential":
            weighted = self._distribution == "weighted" and self._weights is not None
            self._indexer = _kernel_select.choice_indexer(
                ctx, len(ref_vals), self._weights if weighted else None
            )
        self._rng = (
            random.Random(ctx.base_int("py"))
            if self._indexer is None and self._distribution != "sequential"
            else None
        )

    def next_batch(self, offset: int, size: int) -> list[Any]:
        if not self._vals:
            return [None] * size
        if self._distribution == "sequential":
            return [self._vals[i % len(self._vals)] for i in range(offset, offset + size)]
        if self._indexer is not None:
            return gather(self._indexer, self._vals, offset, size)
        assert self._rng is not None  # noqa: S101 -- set whenever there is no indexer
        if self._distribution == "weighted":
            return [
                self._rng.choices(self._vals, weights=self._weights, k=1)[0] for _ in range(size)
            ]
        return [self._rng.choice(self._vals) for _ in range(size)]


class NullMaskedStream:
    """Wraps a stream with the V1 `null_probability` mask: one persistent `default_rng` draw."""

    def __init__(self, inner: ColumnStream, ctx: GenDeriveContext, probability: float) -> None:
        self._inner = inner
        self._p = probability
        self._rng = np.random.default_rng(ctx.base_int("np"))

    def next_batch(self, offset: int, size: int) -> list[Any]:
        return self.next_batch_from(offset, size, {})

    def next_batch_from(
        self, offset: int, size: int, siblings: Mapping[str, list[Any]]
    ) -> list[Any]:
        values = draw_batch(self._inner, offset, size, siblings)
        for idx in np.flatnonzero(self._rng.random(size) < self._p):
            values[int(idx)] = None
        return values


def draw_batch(
    stream: ColumnStream, offset: int, size: int, siblings: Mapping[str, list[Any]]
) -> list[Any]:
    """Draw one batch, handing the already-emitted sibling batches to a stream that reads them."""
    from_siblings = getattr(stream, "next_batch_from", None)
    if from_siblings is not None:
        return from_siblings(offset, size, siblings)
    return stream.next_batch(offset, size)


def make_column_stream(
    col: dict[str, Any],
    *,
    table: str,
    rows: int,
    seed: int,
    derive_key: Any,
    instance_default_locale: str | None,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None,
    key_pools: Mapping[tuple[str, str], list[Any]],
    ledger: GenerationBudgetLedger,
    stage: ExitStack,
    faker_payload: int | None,
) -> ColumnStream:
    """Build one column's stream. Reservations taken here live until `stage` closes."""
    ctx = GenDeriveContext.for_column(derive_key=derive_key, column_config=col, fallback_seed=seed)
    kind = col.get("type")
    inner: ColumnStream
    if kind == "sequence":
        inner = SequenceStream(col)
    elif kind == "categorical":
        inner = CategoricalStream(col, ctx)
    elif kind == "faker":
        inner = _faker_stream(
            f"{table}.{col['name']}",
            col,
            ctx,
            rows,
            seed,
            instance_default_locale,
            provider_snapshot,
            ledger,
            stage,
            faker_payload,
        )
    elif kind == "reference":
        inner = ReferenceStream(
            col, ctx, key_pools[(col["reference_table"], col["reference_column"])]
        )
    elif kind == "windowed_date":
        inner = WindowedDateStream(col, seed)
    else:
        raise ValueError(f"column {col.get('name')!r}: type {kind!r} has no streamed generator")
    null_prob = float(col.get("null_probability") or 0.0)
    return NullMaskedStream(inner, ctx, null_prob) if null_prob > 0 else inner


def _faker_stream(
    label: str,
    col: dict[str, Any],
    ctx: GenDeriveContext,
    rows: int,
    seed: int,
    instance_default_locale: str | None,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None,
    ledger: GenerationBudgetLedger,
    stage: ExitStack,
    faker_payload: int | None,
) -> ColumnStream:
    faker_type = col.get("faker_type", "word")
    requested_locale = col.get("locale") or instance_default_locale
    # Pool admission is the TABLE row count, fixed for the column before the first batch.
    if _faker_pool.pool_eligible(
        faker_type, requested_locale, rows, opted_out=col.get("pooled") is False
    ):
        raw_kwargs = col.get("faker_kwargs") or {}
        # Reserve before the 10_000 provider calls that build the pool allocate anything.
        reservation = ledger.reserve(
            f"faker_pool_build:{label}",
            faker_pool_bytes(10_000, faker_payload if faker_payload is not None else 0),
        )
        selector = _faker_pool.open_pooled_selector(
            faker_type=faker_type,
            faker_kwargs=raw_kwargs if isinstance(raw_kwargs, dict) else {},
            gen_ctx=ctx,
            effective_locale=requested_locale,
            provider_snapshot=provider_snapshot,
        )
        if selector is not None:
            stage.callback(reservation.release)
            return PooledFakerStream(selector)
        reservation.release()
    return PerRowFakerStream(col, ctx, seed, instance_default_locale, provider_snapshot)


__all__ = [
    "CategoricalStream",
    "ColumnStream",
    "KeyPool",
    "NullMaskedStream",
    "PerRowFakerStream",
    "PooledFakerStream",
    "ReferenceStream",
    "SequenceStream",
    "WindowedDateStream",
    "draw_batch",
    "gather",
    "make_column_stream",
]
