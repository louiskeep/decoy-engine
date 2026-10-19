"""F-audit cells: one concrete fixture per matrix row, plus the discard-and-substitute mutants.

A cell binds a matrix row (`_f_audit_matrix.ROWS`) to a runnable configuration and says which
columns carry the row's compute. Expected compute, route and reason come from the MATRIX, never
from a witness report. A mutant is installed after the witness hooks so the witness records the
producer's real (marked) output first; the mutant then DISCARDS that output and SUBSTITUTES an
independently recomputed value, which the value-equality witness must reject.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from decoy_engine.generation import _column_streams, _faker_pool, synthesize
from decoy_engine.generators.columns import ColumnGenerator
from tests.parity import _f0_fixtures as fx
from tests.parity import _f_audit_matrix as mx
from tests.parity._f0_witness import ColumnKey

# Irregular batch cuts applied to every table: zero-row batches, a duplicate cut, a straddle.
CUTS = (3, 11, 11, 29)
POOL_THRESHOLD = 90  # lowered from 50_000 so a 100-row table pools
ANCHORS = ["2024-03-01", "2024-03-15", "2024-04-02", "2024-12-30"]


@dataclass(frozen=True)
class Mutant:
    name: str
    install: Callable[[pytest.MonkeyPatch], None]
    match: str


@dataclass(frozen=True)
class Cell:
    row_id: str
    build: Callable[[], dict[str, Any]]
    # witnessed column -> the matrix row that owns its compute
    columns: dict[ColumnKey, str]
    # the column whose own admission the cell asserts (None: use the first witnessed column)
    admission_key: ColumnKey | None = None
    threshold: int | None = None
    inject: Callable[[pytest.MonkeyPatch], None] | None = None
    provider_snapshot: Any = None
    mutants: tuple[Mutant, ...] = field(default=())


def _t(name: str, rows: int, cols: list[dict[str, Any]]) -> dict[str, Any]:
    return fx.table(name, rows, cols)


# --------------------------------------------------------------------------------------------
# configurations
# --------------------------------------------------------------------------------------------


def cfg_sequence() -> dict[str, Any]:
    return fx.config(
        [
            _t(
                "t",
                40,
                [
                    {
                        "name": "id",
                        "type": "sequence",
                        "start": 5,
                        "step": 3,
                        "prefix": "S-",
                        "pad_length": 4,
                    },
                    {"name": "id_m", "type": "sequence", "start": 1, "null_probability": 0.3},
                ],
            )
        ]
    )


def cfg_categorical() -> dict[str, Any]:
    return fx.config(
        [
            _t(
                "t",
                40,
                [
                    {"name": "cat", "type": "categorical", "categories": ["a", "b", "c"]},
                    {
                        "name": "cat_m",
                        "type": "categorical",
                        "categories": ["u", "v"],
                        "weights": [3, 1],
                        "null_probability": 0.3,
                    },
                ],
            )
        ]
    )


def _parent(null_probability: float = 0.0) -> dict[str, Any]:
    col: dict[str, Any] = {"name": "k", "type": "categorical", "categories": ["K1", "K2", "K3"]}
    if null_probability:
        col["null_probability"] = null_probability
    return _t("par", 12, [col])


def cfg_reference(dist: str, *, parent_null: float = 0.0) -> dict[str, Any]:
    extra: dict[str, Any] = {"weights": [1, 2, 3]} if dist == "weighted" else {}
    ref = {
        "type": "reference",
        "reference_table": "par",
        "reference_column": "k",
        "distribution": dist,
        **extra,
    }
    child = [{"name": "r", **ref}, {"name": "r_m", "null_probability": 0.3, **ref}]
    return fx.config([_t("child", 40, child), _parent(parent_null)])


def cfg_reference_repair() -> dict[str, Any]:
    ref = {
        "name": "r",
        "type": "reference",
        "reference_table": "par",
        "reference_column": "k",
        "min_per_parent": 2,
        "max_per_parent": 4,
    }
    return fx.config([_t("child", 14, [ref]), _parent()])


def cfg_faker(faker_type: str, *, rows: int = 100, **extra: Any) -> dict[str, Any]:
    cols = [
        {"name": "f", "type": "faker", "faker_type": faker_type, **extra},
        {
            "name": "f_m",
            "type": "faker",
            "faker_type": faker_type,
            "null_probability": 0.2,
            **extra,
        },
    ]
    return fx.config([_t("t", rows, cols)])


def cfg_windowed() -> dict[str, Any]:
    win = {"type": "windowed_date", "anchor": "anchor", "min_days": 1, "max_days": 30}
    cols: list[dict[str, Any]] = [
        {"name": "anchor", "type": "categorical", "categories": ANCHORS, "null_probability": 0.15},
        {"name": "d", **win},
        {"name": "d_m", "null_probability": 0.3, **win},
    ]
    return fx.config([_t("t", 60, cols)])


def cfg_windowed_declined() -> dict[str, Any]:
    cols: list[dict[str, Any]] = [
        {"name": "anchor", "type": "formula", "formula": "'2024-01-01'"},
        {"name": "d", "type": "windowed_date", "anchor": "anchor", "min_days": 1, "max_days": 30},
    ]
    return fx.config([_t("t", 30, cols)])


_FM: dict[str, Any] = {"name": "fm", "type": "formula", "formula": "randint(1, 1000)"}


def cfg_formula() -> dict[str, Any]:
    return fx.config([_t("t", 40, [_FM])])


def cfg_formula_referenced() -> dict[str, Any]:
    cols: list[dict[str, Any]] = [
        {"name": "cat", "type": "categorical", "categories": ["a", "b"]},
        {"name": "seq", "type": "sequence", "start": 1},
        {
            "name": "rfm",
            "type": "formula",
            "formula": "f'{cat}-{seq}'",
            "references": ["cat", "seq"],
        },
    ]
    return fx.config([_t("t", 40, cols)])


def cfg_derived() -> dict[str, Any]:
    return fx.config(
        [_t("t", 40, [_FM, {"name": "der", "type": "derived", "expression": "fm + 1"}])]
    )


def cfg_derived_aggregate() -> dict[str, Any]:
    agg = {"name": "agg", "type": "derived_aggregate", "op": "sum", "column": "fm"}
    return fx.config([_t("t", 40, [_FM, agg])])


def cfg_fresh() -> dict[str, Any]:
    cols = [
        {"name": "cat", "type": "categorical", "categories": ["a", "b"], "determinism": "fresh"}
    ]
    return fx.config([_t("t", 30, cols)])


def cfg_mixed_plan() -> dict[str, Any]:
    """A declined formula in table `lone` forces the eligible categorical in `other` to legacy."""
    other: list[dict[str, Any]] = [
        {"name": "cat", "type": "categorical", "categories": ["a", "b", "c"]},
        {"name": "seq", "type": "sequence", "start": 1},
    ]
    return fx.config([_t("lone", 20, [_FM]), _t("other", 40, other)])


def cfg_custom_provider() -> dict[str, Any]:
    return fx.config(
        [_t("t", 40, [{"name": "f", "type": "faker", "faker_type": "f_audit_custom"}])]
    )


def custom_provider_snapshot() -> dict[str, Callable[[Any], Any]]:
    return {"f_audit_custom": lambda fake: f"C-{fake.word()}"}


def refuse_pool_build(mp: pytest.MonkeyPatch) -> None:
    """Admission and the gate pass, then the provider resolver declines the pool."""
    mp.setattr(_faker_pool, "build_pool_values", lambda *a, **kw: ([], False, False))


# --------------------------------------------------------------------------------------------
# mutants: execute the producer, DISCARD its output, SUBSTITUTE an independently recomputed one
# --------------------------------------------------------------------------------------------


def _outer(target: Any, name: str, replace: Callable[[Any], Any]) -> Callable[..., None]:
    def install(mp: pytest.MonkeyPatch) -> None:
        mp.setattr(target, name, replace(getattr(target, name)))

    return install


def _sub_sequence(inner: Any) -> Any:
    def next_batch(self: Any, offset: int, size: int) -> list[Any]:
        inner(self, offset, size)
        out = []
        for i in range(offset, offset + size):
            body = str(self._start + i * self._step)
            body = body.zfill(self._pad) if self._pad > 0 else body
            out.append(f"{self._prefix}{body}{self._suffix}")
        return out

    return next_batch


def _sub_reference_sequential(inner: Any) -> Any:
    def next_batch(self: Any, offset: int, size: int) -> list[Any]:
        inner(self, offset, size)
        vals = self._vals
        return (
            [vals[i % len(vals)] for i in range(offset, offset + size)] if vals else [None] * size
        )

    return next_batch


def _sub_reference_draw(inner: Any) -> Any:
    def next_batch(self: Any, offset: int, size: int) -> list[Any]:
        inner(self, offset, size)
        vals = self._vals
        return [vals[(i * 7 + 1) % len(vals)] for i in range(offset, offset + size)]

    return next_batch


def _sub_categorical(inner: Any) -> Any:
    def next_batch(self: Any, offset: int, size: int) -> list[Any]:
        inner(self, offset, size)
        return [self._cats[(i * 5 + 1) % len(self._cats)] for i in range(offset, offset + size)]

    return next_batch


def _sub_pooled(inner: Any) -> Any:
    def select(self: Any, size: int) -> list[Any]:
        inner(self, size)
        return [self._values[(i * 7 + 1) % len(self._values)] for i in range(size)]

    return select


def _sub_live(inner: Any) -> Any:
    def next_batch(self: Any, offset: int, size: int) -> list[Any]:
        inner(self, offset, size)
        return ["substituted"] * size

    return next_batch


def _sub_windowed(unmarked: Callable[..., list[Any]]) -> Callable[[Any], Any]:
    """Substitute the producer's own UNMARKED output: equal in value to an honest recompute, yet
    distinguishable from the marked value the producer handed the witness."""

    def replace(inner: Any) -> Any:
        def next_batch_from(self: Any, offset: int, size: int, siblings: Any) -> list[Any]:
            inner(self, offset, size, siblings)
            return unmarked(self, offset, size, siblings)

        return next_batch_from

    return replace


def _sub_formula(inner: Any) -> Any:
    def formula(col: dict[str, Any], n: int, *a: Any, **k: Any) -> list[Any]:
        inner(col, n, *a, **k)
        return [0] * n

    return formula


def _sub_formula_post_pass(inner: Any) -> Any:
    def fill(self: ColumnGenerator, col_name: str, *a: Any, **kw: Any) -> Any:
        series = inner(self, col_name, *a, **kw)
        return series.map(lambda _v: "substituted")

    return fill


def _sub_derived(inner: Any) -> Any:
    def derived(col: dict[str, Any], n: int, generated: Any) -> list[Any]:
        out = inner(col, n, generated)
        return [None if v is None else v + 1 for v in out]

    return derived


def _sub_aggregate(inner: Any) -> Any:
    def agg(col: dict[str, Any], n: int, generated: Any) -> list[Any]:
        out = list(inner(col, n, generated))
        return [None if v is None else v + 1 for v in out]

    return agg


def _sub_legacy_faker(inner: Any) -> Any:
    def faker(col: dict[str, Any], n: int, *a: Any, **k: Any) -> list[Any]:
        inner(col, n, *a, **k)
        return ["substituted"] * n

    return faker


def _sub_legacy_categorical(inner: Any) -> Any:
    def categorical(col: dict[str, Any], n: int, *a: Any, **k: Any) -> list[Any]:
        return list(reversed(inner(col, n, *a, **k)))

    return categorical


def mutants_for(row_id: str, unmarked_windowed: Callable[..., list[Any]]) -> tuple[Mutant, ...]:
    cs, syn = _column_streams, synthesize
    table: dict[str, tuple[Mutant, ...]] = {
        "sequence": (
            Mutant(
                "recomputed-sequence",
                _outer(cs.SequenceStream, "next_batch", _sub_sequence),
                "sequence output is not what the column holds",
            ),
        ),
        "categorical": (
            Mutant(
                "recomputed-categorical",
                _outer(cs.CategoricalStream, "next_batch", _sub_categorical),
                "python RNG selections",
            ),
        ),
        "reference-sequential": (
            Mutant(
                "recomputed-direct-index",
                _outer(cs.ReferenceStream, "next_batch", _sub_reference_sequential),
                "reference_sequential output is not what the column holds",
            ),
        ),
        "reference-random": (
            Mutant(
                "recomputed-draw",
                _outer(cs.ReferenceStream, "next_batch", _sub_reference_draw),
                "python RNG selections",
            ),
        ),
        "reference-weighted": (
            Mutant(
                "recomputed-draw",
                _outer(cs.ReferenceStream, "next_batch", _sub_reference_draw),
                "python RNG selections",
            ),
        ),
        **{
            fid: (
                Mutant(
                    "recomputed-pool-index",
                    _outer(_faker_pool.PooledSelector, "select", _sub_pooled),
                    "numpy-selected|sampler result",
                ),
            )
            for fid in ("faker-pooled", "faker-certified-locale-pair")
        },
        **{
            fid: (
                Mutant(
                    "constant-provider",
                    _outer(cs.PerRowFakerStream, "next_batch", _sub_live),
                    "live provider results",
                ),
            )
            for fid in (
                "faker-below-threshold",
                "faker-opt-out",
                "faker-uncertified",
                "faker-uncertified-locale",
                "faker-build-refused",
            )
        },
        "faker-custom-provider": (
            Mutant(
                "constant-provider",
                _outer(syn, "_faker", _sub_legacy_faker),
                "live provider results",
            ),
        ),
        "windowed-date": (
            Mutant(
                "unmarked-recompute",
                _outer(cs.WindowedDateStream, "next_batch_from", _sub_windowed(unmarked_windowed)),
                "windowed_date output is not what the column holds",
            ),
        ),
        "formula-inline": (
            Mutant("zeros", _outer(syn, "_formula", _sub_formula), "formula results"),
        ),
        "formula-referenced": (
            Mutant(
                "constant-post-pass",
                _outer(ColumnGenerator, "fill_referenced_formula_column", _sub_formula_post_pass),
                "formula results",
            ),
        ),
        "derived": (
            Mutant(
                "shifted",
                _outer(syn, "_derived_generate", _sub_derived),
                "derived output is not what the column holds",
            ),
        ),
        "derived-aggregate": (
            Mutant(
                "shifted",
                _outer(syn, "generate_derived_aggregate_column", _sub_aggregate),
                "derived_aggregate output is not what the column holds",
            ),
        ),
        "determinism-fresh": (
            Mutant(
                "reordered",
                _outer(syn, "_categorical", _sub_legacy_categorical),
                "python RNG selections",
            ),
        ),
        "mixed-plan-forced-legacy": (
            Mutant(
                "reordered",
                _outer(syn, "_categorical", _sub_legacy_categorical),
                "python RNG selections",
            ),
        ),
    }
    return table.get(row_id, ())


def _cells(unmarked_windowed: Callable[..., list[Any]]) -> tuple[Cell, ...]:
    def m(row_id: str) -> tuple[Mutant, ...]:
        return mutants_for(row_id, unmarked_windowed)

    def ref_cell(row_id: str, dist: str) -> Cell:
        return Cell(
            row_id,
            lambda: cfg_reference(dist),
            {("child", "r"): row_id, ("child", "r_m"): row_id, ("par", "k"): "categorical"},
            mutants=m(row_id),
        )

    def faker_cell(row_id: str, ftype: str, cfg: dict[str, Any] | None = None, **kw: Any) -> Cell:
        return Cell(
            row_id,
            lambda: cfg_faker(ftype, **(cfg or {})),
            {("t", "f"): row_id, ("t", "f_m"): row_id},
            mutants=m(row_id),
            **kw,
        )

    return (
        Cell(
            "sequence",
            cfg_sequence,
            {("t", "id"): "sequence", ("t", "id_m"): "sequence"},
            mutants=m("sequence"),
        ),
        Cell(
            "categorical",
            cfg_categorical,
            {("t", "cat"): "categorical", ("t", "cat_m"): "categorical"},
            mutants=m("categorical"),
        ),
        ref_cell("reference-sequential", "sequential"),
        ref_cell("reference-random", "random"),
        ref_cell("reference-weighted", "weighted"),
        faker_cell("faker-pooled", "city", threshold=POOL_THRESHOLD),
        faker_cell("faker-below-threshold", "city"),
        faker_cell("faker-opt-out", "city", threshold=POOL_THRESHOLD, cfg={"pooled": False}),
        faker_cell("faker-uncertified", "email", threshold=POOL_THRESHOLD),
        faker_cell(
            "faker-certified-locale-pair",
            mx.LOCALE_PAIR[0],
            threshold=POOL_THRESHOLD,
            cfg={"locale": mx.LOCALE_PAIR[1]},
        ),
        faker_cell(
            "faker-uncertified-locale",
            mx.LOCALE_PAIR[0],
            threshold=POOL_THRESHOLD,
            cfg={"locale": mx.UNCERTIFIED_LOCALE},
        ),
        faker_cell(
            "faker-build-refused", "city", threshold=POOL_THRESHOLD, inject=refuse_pool_build
        ),
        Cell(
            "windowed-date",
            cfg_windowed,
            {
                ("t", "d"): "windowed-date",
                ("t", "d_m"): "windowed-date",
                ("t", "anchor"): "categorical",
            },
            mutants=m("windowed-date"),
        ),
    )


def core_cells(unmarked_windowed: Callable[..., list[Any]]) -> tuple[Cell, ...]:
    return _cells(unmarked_windowed)


def legacy_cells(unmarked_windowed: Callable[..., list[Any]]) -> tuple[Cell, ...]:
    def m(row_id: str) -> tuple[Mutant, ...]:
        return mutants_for(row_id, unmarked_windowed)

    return (
        Cell(
            "reference-cardinality-repair",
            cfg_reference_repair,
            {},
            admission_key=("child", "r"),
        ),
        Cell(
            "faker-custom-provider",
            cfg_custom_provider,
            {("t", "f"): "faker-custom-provider"},
            provider_snapshot=custom_provider_snapshot(),
            mutants=m("faker-custom-provider"),
        ),
        Cell("windowed-date-declined", cfg_windowed_declined, {}, admission_key=("t", "d")),
        Cell(
            "formula-inline",
            cfg_formula,
            {("t", "fm"): "formula-inline"},
            mutants=m("formula-inline"),
        ),
        Cell(
            "formula-referenced",
            cfg_formula_referenced,
            {("t", "rfm"): "formula-referenced"},
            mutants=m("formula-referenced"),
        ),
        Cell(
            "derived",
            cfg_derived,
            {("t", "der"): "derived"},
            mutants=m("derived"),
        ),
        Cell(
            "derived-aggregate",
            cfg_derived_aggregate,
            {("t", "agg"): "derived-aggregate"},
            mutants=m("derived-aggregate"),
        ),
        Cell("statistical", fx.cfg_statistical, {}, admission_key=("stat", "amount")),
        Cell("grouped-series", fx.cfg_grouped_windowed, {}, admission_key=("gw", "cum")),
        Cell("group-key", fx.cfg_grouped_windowed, {}, admission_key=("gw", "gkey")),
        Cell(
            "determinism-fresh",
            cfg_fresh,
            {("t", "cat"): "determinism-fresh"},
            mutants=m("determinism-fresh"),
        ),
        Cell(
            "mixed-plan-forced-legacy",
            cfg_mixed_plan,
            {("other", "cat"): "mixed-plan-forced-legacy"},
            admission_key=("other", "cat"),
            mutants=m("mixed-plan-forced-legacy"),
        ),
    )
