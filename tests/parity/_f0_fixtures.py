"""Pinned fixtures for the Phase F F0 generation contract (test-side only).

Every fixture names its ENTRY POINT. Two entry points exist in F0:

- ``generate_tables``: `decoy_engine.generation._plan_entry.generate_tables(plan, ...)`, the
  whole-table end-to-end surface, called on a Plan from `compile_plan`;
- ``run_pipeline``: `decoy_engine.execution.run_pipeline`, used ONLY for the generated-parent ->
  masked-child FK boundary, which `generate_tables` alone cannot establish;
- ``composite_sampler``: `CompositePerson.generate_bundle` / `CompositeNameEmail.generate_bundle`,
  a SEPARATE sampler contract. `generate_tables` never dispatches a composite (its
  `_generate_column` has no composite kind), so a composite fixture must not claim end-to-end
  coverage.

Inputs are literals in this file; the only external input is
`tests/fixtures/phase_f_f0/stat_snapshot.json`. Formulas avoid `today()` and the
`*_from_now` helpers, which read the wall clock (`ColumnGenerator._reference_date`) and would make
an expectation expire. Expected fingerprints are in `_f0_expected.py`; this module never computes
or stores an expectation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa

from decoy_engine.generation.synthesize import generate_tables
from decoy_engine.plan import compile_plan
from decoy_engine.profile import Profile

# The oracle in `_f0_expected.py` is a frozen contract recorded on this date. Faker's
# `date_of_birth`, `date_time*`, `date_between`, and `date_this_*` providers are computed
# relative to the wall clock (`datetime.now()` / `date.today()`), so without a frozen clock the
# recorded fingerprints only match on the recording day. Generation is run under this clock
# to reproduce them regardless of the real date.
ORACLE_CLOCK = datetime(2026, 10, 16, 12, 0, 0)


class _SameInstances(type):
    """Keep `isinstance(real_value, Oracle*)` true: Faker type-checks against the patched names."""

    _real: type

    def __instancecheck__(cls, obj: object) -> bool:
        return isinstance(obj, cls._real)

    def __subclasscheck__(cls, sub: type) -> bool:
        return issubclass(sub, cls._real)


class _OracleDatetime(datetime, metaclass=_SameInstances):
    _real = datetime

    @classmethod
    def now(cls, tz: Any = None) -> Any:
        return ORACLE_CLOCK.replace(tzinfo=tz) if tz is not None else ORACLE_CLOCK

    @classmethod
    def today(cls) -> Any:
        return ORACLE_CLOCK


class _OracleDate(date, metaclass=_SameInstances):
    _real = date

    @classmethod
    def today(cls) -> Any:
        return ORACLE_CLOCK.date()


@contextmanager
def frozen_faker_clock() -> Iterator[None]:
    """Pin the clock Faker's date/time providers read to `ORACLE_CLOCK`."""
    from faker.providers import date_time

    saved = {name: getattr(date_time, name) for name in ("datetime", "dtdate")}
    setattr(date_time, "datetime", _OracleDatetime)  # noqa: B010
    setattr(date_time, "dtdate", _OracleDate)  # noqa: B010
    try:
        yield
    finally:
        for name, original in saved.items():
            setattr(date_time, name, original)


SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "phase_f_f0" / "stat_snapshot.json"
)

# Dependency versions the expectations were captured under. Faker output is a function of the
# Faker data tables, so a Faker bump legitimately moves every faker fixture; the pin test names
# that cause instead of leaving a wall of digest failures.
PINNED_FAKER_VERSION = "40.23.0"

ENTRY_GENERATE_TABLES = "generate_tables"
ENTRY_COMPOSITE = "composite_sampler"
ENTRY_RUN_PIPELINE = "run_pipeline"


def _profile() -> Profile:
    return Profile(
        schema_version=1,
        tables=(),
        relationships=(),
        profiled_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        decoy_engine_version="f0",
    )


def config(tables: list[dict[str, Any]], *, seed: int = 20261016) -> dict[str, Any]:
    return {
        "version": 2,
        "global_settings": {"seed": seed},
        "sources": {},
        "tables": tables,
        "targets": {},
    }


def table(name: str, rows: int, cols: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": name, "row_count": rows, "generate_columns": cols}


def pinned_derive_key(label: str) -> bytes:
    """A fixed `derive_key` resolver (the pipeline-bound keyed path), independent of any vault."""
    return hashlib.sha256(b"f0-pinned-derive-key|" + label.encode("utf-8")).digest()


def run_generate_tables(
    cfg: dict[str, Any],
    *,
    instance_default_locale: str | None = None,
    derive_key: Callable[[str], bytes] | None = None,
) -> dict[str, pa.Table]:
    """The `generate_tables` entry point on a freshly compiled Plan."""
    plan = compile_plan(cfg, _profile(), decoy_engine_version="f0")
    return generate_tables(
        plan, derive_key=derive_key, instance_default_locale=instance_default_locale
    )


@dataclass(frozen=True)
class Fixture:
    """One oracle fixture: an id, the entry point it exercises, and a no-arg runner."""

    fid: str
    entry: str
    family: str
    run: Callable[[], dict[str, pa.Table]]
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# generate_tables fixtures, one concrete whole-table fixture per supported family.
# ---------------------------------------------------------------------------


def cfg_sequence() -> dict[str, Any]:
    return config(
        [
            table(
                "seq",
                12,
                [
                    {"name": "plain", "type": "sequence", "start": 1},
                    {
                        "name": "padded",
                        "type": "sequence",
                        "start": 100,
                        "step": 7,
                        "prefix": "INV-",
                        "suffix": "/x",
                        "pad_length": 6,
                    },
                    {"name": "neg", "type": "sequence", "start": 50, "step": -4},
                ],
            )
        ]
    )


def cfg_categorical() -> dict[str, Any]:
    return config(
        [
            table(
                "cat",
                40,
                [
                    {
                        "name": "weighted",
                        "type": "categorical",
                        "categories": ["red", "green", "blue", "amber"],
                        "weights": [5, 3, 2, 1],
                    },
                    {"name": "uniform", "type": "categorical", "categories": ["a", "b", "c"]},
                    {"name": "ints", "type": "categorical", "categories": [1, 2, 3, 4, 5]},
                ],
            )
        ]
    )


def cfg_pooled_faker() -> dict[str, Any]:
    """50_000 rows is the real `N_THRESHOLD`, so these columns take the REAL pooled path."""
    return config(
        [
            table(
                "pooled",
                50_000,
                [
                    {"name": "city", "type": "faker", "faker_type": "city"},
                    {"name": "last", "type": "faker", "faker_type": "last_name", "locale": "de_DE"},
                    {
                        "name": "street",
                        "type": "faker",
                        "faker_type": "street_name",
                        "locale": "fr_FR",
                    },
                ],
            )
        ]
    )


def cfg_live_faker() -> dict[str, Any]:
    """Below threshold or ineligible, so every row calls the provider (unpooled `_faker`)."""
    return config(
        [
            table(
                "live",
                30,
                [
                    {"name": "city", "type": "faker", "faker_type": "city"},
                    {"name": "email", "type": "faker", "faker_type": "email"},
                    {"name": "de_name", "type": "faker", "faker_type": "name", "locale": "de_DE"},
                    {
                        "name": "num",
                        "type": "faker",
                        "faker_type": "pyint",
                        "faker_kwargs": {"min_value": 5, "max_value": 9},
                    },
                    {"name": "fallback", "type": "faker", "faker_type": "not_a_real_provider"},
                    {"name": "opt_out", "type": "faker", "faker_type": "company", "pooled": False},
                ],
            )
        ]
    )


def cfg_live_faker_default_locale() -> dict[str, Any]:
    """Exercised with `instance_default_locale="fr_FR"`: the column has no locale of its own."""
    return config(
        [
            table(
                "live_loc",
                20,
                [
                    {"name": "city", "type": "faker", "faker_type": "city"},
                    {"name": "own", "type": "faker", "faker_type": "city", "locale": "de_DE"},
                ],
            )
        ]
    )


def cfg_keyed() -> dict[str, Any]:
    """Run with `derive_key=pinned_derive_key`: the keyed per-column root replaces the seed root."""
    return config(
        [
            table(
                "keyed",
                24,
                [
                    {"name": "cat", "type": "categorical", "categories": ["a", "b", "c", "d"]},
                    {"name": "city", "type": "faker", "faker_type": "city"},
                    {"name": "rnd", "type": "formula", "formula": "randint(1, 1000)"},
                    {
                        "name": "maybe",
                        "type": "categorical",
                        "categories": ["u", "v"],
                        "null_probability": 0.4,
                    },
                ],
            )
        ]
    )


def cfg_formula() -> dict[str, Any]:
    return config(
        [
            table(
                "formula",
                25,
                [
                    {"name": "rnd", "type": "formula", "formula": "randint(1, 1000)"},
                    {"name": "gau", "type": "formula", "formula": "round(gauss(50, 10), 3)"},
                    {"name": "pick", "type": "formula", "formula": "choice(['p', 'q', 'r'])"},
                    {"name": "row", "type": "formula", "formula": "i * 3 + 1"},
                    {"name": "hsh", "type": "formula", "formula": "hash(i)"},
                    {
                        "name": "fixed_date",
                        "type": "formula",
                        "formula": "f'2024-03-{1 + i % 28:02d}'",
                    },
                    {"name": "empty", "type": "formula", "formula": ""},
                    {"name": "bad", "type": "formula", "formula": "undefined_name + 1"},
                ],
            )
        ]
    )


def cfg_referenced_formula() -> dict[str, Any]:
    """Post-pass ordering: `rf_b` reads the finalized `rf_a`; `rf_c` reads `rf_d`, declared AFTER it,
    and so sees the None placeholder (resolved to ""); `rf_d` itself is filled last."""
    return config(
        [
            table(
                "rf",
                16,
                [
                    {"name": "pid", "type": "sequence", "start": 1, "prefix": "P"},
                    {"name": "grp", "type": "categorical", "categories": ["g1", "g2", "g3"]},
                    {
                        "name": "rf_a",
                        "type": "formula",
                        "formula": "f'{grp}-{pid}-{randint(1, 9)}'",
                        "references": ["grp", "pid"],
                    },
                    {
                        "name": "rf_b",
                        "type": "formula",
                        "formula": "f'{rf_a}/b{randint(1, 9)}'",
                        "references": ["rf_a"],
                        "null_probability": 0.25,
                    },
                    {
                        "name": "rf_c",
                        "type": "formula",
                        "formula": "f'[{rf_d}]'",
                        "references": ["rf_d"],
                    },
                    {
                        "name": "rf_d",
                        "type": "formula",
                        "formula": "f'd{i}'",
                        "references": ["pid"],
                    },
                ],
            )
        ]
    )


def cfg_derived() -> dict[str, Any]:
    return config(
        [
            table(
                "derived",
                14,
                [
                    {"name": "base", "type": "formula", "formula": "i * 2"},
                    {
                        "name": "maybe",
                        "type": "formula",
                        "formula": "i + 100",
                        "null_probability": 0.3,
                    },
                    {"name": "plus", "type": "derived", "expression": "base + 1"},
                    {"name": "from_nullable", "type": "derived", "expression": "maybe * 2"},
                ],
            )
        ]
    )


def cfg_reference() -> dict[str, Any]:
    """Standalone `reference`: parent keys with duplicates and nulls, every distribution, and the
    cardinality-repair path (`min_per_parent` / `max_per_parent`)."""
    parent_cols: list[dict[str, Any]] = [
        {
            "name": "pkey",
            "type": "categorical",
            "categories": ["K3", "K1", "K2", "K5", "K4"],
            "null_probability": 0.2,
        }
    ]
    child_cols: list[dict[str, Any]] = [
        {"name": "cid", "type": "sequence", "start": 1},
        {
            "name": "r_random",
            "type": "reference",
            "reference_table": "ref_parent",
            "reference_column": "pkey",
            "distribution": "random",
        },
        {
            "name": "r_seq",
            "type": "reference",
            "reference_table": "ref_parent",
            "reference_column": "pkey",
            "distribution": "sequential",
        },
        {
            "name": "r_weighted",
            "type": "reference",
            "reference_table": "ref_parent",
            "reference_column": "pkey",
            "distribution": "weighted",
            "weights": [1, 2, 3, 4, 5],
        },
        {
            "name": "r_bounded",
            "type": "reference",
            "reference_table": "ref_parent",
            "reference_column": "pkey",
            "distribution": "random",
            "min_per_parent": 2,
            "max_per_parent": 4,
        },
    ]
    # Child declared FIRST on purpose: the topological sort, not config order, must put the parent
    # ahead of it.
    return config([table("ref_child", 14, child_cols), table("ref_parent", 12, parent_cols)])


def cfg_derived_aggregate() -> dict[str, Any]:
    return config(
        [
            table(
                "agg",
                10,
                [
                    {"name": "v", "type": "formula", "formula": "i * 2 + 1"},
                    {
                        "name": "v_null",
                        "type": "formula",
                        "formula": "i + 1",
                        "null_probability": 0.3,
                    },
                    {"name": "a_sum", "type": "derived_aggregate", "op": "sum", "column": "v"},
                    {"name": "a_mean", "type": "derived_aggregate", "op": "mean", "column": "v"},
                    {"name": "a_min", "type": "derived_aggregate", "op": "min", "column": "v"},
                    {"name": "a_max", "type": "derived_aggregate", "op": "max", "column": "v"},
                    {
                        "name": "a_count",
                        "type": "derived_aggregate",
                        "op": "count",
                        "column": "v_null",
                    },
                ],
            )
        ]
    )


def cfg_statistical() -> dict[str, Any]:
    snap = str(SNAPSHOT_PATH)
    return config(
        [
            table(
                "stat",
                60,
                [
                    {"name": "amount", "type": "statistical", "snapshot_file": snap},
                    {"name": "age", "type": "statistical", "snapshot_file": snap},
                    {
                        "name": "joined",
                        "type": "statistical",
                        "snapshot_file": snap,
                    },
                    {
                        "name": "state",
                        "type": "statistical",
                        "snapshot_file": snap,
                        "allow_real_categories": True,
                    },
                ],
            )
        ]
    )


def cfg_conditional() -> dict[str, Any]:
    """Sequential conditional sampling: `tier` is drawn conditioned on the already-built `state`."""
    snap = str(SNAPSHOT_PATH)
    return config(
        [
            table(
                "cond",
                60,
                [
                    {
                        "name": "state",
                        "type": "statistical",
                        "snapshot_file": snap,
                        "allow_real_categories": True,
                    },
                    {
                        "name": "tier",
                        "type": "statistical",
                        "snapshot_file": snap,
                        "allow_real_categories": True,
                        "condition_on": "state",
                    },
                ],
            )
        ]
    )


def cfg_grouped_windowed() -> dict[str, Any]:
    return config(
        [
            table(
                "gw",
                18,
                [
                    {"name": "grp", "type": "categorical", "categories": ["g1", "g2", "g3"]},
                    {"name": "ord", "type": "formula", "formula": "i * 5"},
                    {
                        "name": "anchor",
                        "type": "formula",
                        "formula": "f'2024-03-{1 + i % 28:02d}'",
                    },
                    {"name": "cum", "type": "grouped_series", "group_by": "grp", "order_by": "ord"},
                    {
                        "name": "walk",
                        "type": "grouped_series",
                        "group_by": "grp",
                        "order_by": "ord",
                        "generator": "monotone_walk",
                        "start": 10,
                        "max_step": 5,
                    },
                    {
                        "name": "win",
                        "type": "windowed_date",
                        "anchor": "anchor",
                        "min_days": 1,
                        "max_days": 30,
                    },
                    {
                        "name": "gkey",
                        "type": "group_key",
                        "group_by": "grp",
                        "length": 12,
                        "prefix": "G-",
                    },
                ],
            )
        ]
    )


def cfg_nulls() -> dict[str, Any]:
    """Partial-null on several generator types, an all-null column, and a zero-row table."""
    return config(
        [
            table(
                "nulls",
                30,
                [
                    {
                        "name": "seq_partial",
                        "type": "sequence",
                        "start": 1,
                        "null_probability": 0.4,
                    },
                    {
                        "name": "cat_partial",
                        "type": "categorical",
                        "categories": ["u", "v"],
                        "null_probability": 0.4,
                    },
                    {
                        "name": "faker_partial",
                        "type": "faker",
                        "faker_type": "first_name",
                        "null_probability": 0.4,
                    },
                    {
                        "name": "cat_all_null",
                        "type": "categorical",
                        "categories": ["u", "v"],
                        "null_probability": 1.0,
                    },
                ],
            ),
            table("empty", 0, [{"name": "id", "type": "sequence", "start": 1}]),
        ]
    )


# ---------------------------------------------------------------------------
# composite sampler contracts (NOT generate_tables)
# ---------------------------------------------------------------------------

COMPOSITE_SEED = (0x0123456789ABCDEF).to_bytes(8, "big")


def _composite_spec(*, deterministic: bool, extra: dict[str, Any] | None = None) -> Any:
    from decoy_engine.providers_v2._adapter import ProviderSpec

    return ProviderSpec(
        locale="en_US",
        deterministic=deterministic,
        namespace="f0_ns" if deterministic else None,
        seed=COMPOSITE_SEED,
        extra=extra or {},
    )


def _bundle_table(bundle: dict[str, pd.Series]) -> pa.Table:
    # `pd.NA` -> None so the canonical comparator sees an Arrow null.
    return pa.table(
        {
            k: pa.array([None if v is pd.NA else v for v in bundle[k].tolist()])
            for k in sorted(bundle)
        }
    )


def run_composite_person_deterministic() -> dict[str, pa.Table]:
    from decoy_engine.generation.composite._person import CompositePerson

    gen = CompositePerson(coherent_namespace="f0_ns", pool_size=64)
    source = pd.Series(["alice", "bob", None, "carol", "alice", "dave"])
    out = gen.generate_bundle(
        _composite_spec(deterministic=True), len(source), source=source, deterministic=True
    )
    return {"person_det": _bundle_table(out)}


def run_composite_person_seeded_pool() -> dict[str, pa.Table]:
    from decoy_engine.generation.composite._person import CompositePerson

    gen = CompositePerson(coherent_namespace="f0_ns", pool_size=64)
    out = gen.generate_bundle(_composite_spec(deterministic=False), 20, deterministic=False)
    return {"person_pool": _bundle_table(out)}


def run_composite_name_email_deterministic() -> dict[str, pa.Table]:
    from decoy_engine.generation.composite._name_email import CompositeNameEmail

    gen = CompositeNameEmail(coherent_namespace="f0_ns", pool_size=64)
    source = pd.Series(["x1", "x2", "x1", None, "x3"])
    out = gen.generate_bundle(
        _composite_spec(deterministic=True), len(source), source=source, deterministic=True
    )
    return {"name_email_det": _bundle_table(out)}


def run_composite_name_email_seeded_pool() -> dict[str, pa.Table]:
    from decoy_engine.generation.composite._name_email import CompositeNameEmail

    gen = CompositeNameEmail(coherent_namespace="f0_ns", pool_size=64)
    out = gen.generate_bundle(_composite_spec(deterministic=False), 20, deterministic=False)
    return {"name_email_pool": _bundle_table(out)}


def run_mixed_fk() -> dict[str, pa.Table]:
    """Generated parent -> masked child through `run_pipeline`.

    `people` is a generate table; `accounts` is a mask table whose `person_id` is an FK child of
    `people.id` (orphan policy warn). The child's source keys cover the whole parent category set,
    but the generated parent only ever holds a subset of it, so the `orphan_fk` warning's row count
    depends on exactly which parent keys the whole-table generation produced. That is the coupling
    F1/F2 must preserve: the mask side reads the COMPLETE generated parent as its FK pool.
    """
    import tempfile

    import pyarrow.parquet as pq

    from decoy_engine.config import PipelineConfig
    from decoy_engine.execution import run_pipeline

    accounts = pa.table(
        {
            "person_id": pa.array([["x", "y", "z", "w", "v"][i % 5] for i in range(15)]),
            "note": pa.array([f"n{i}" for i in range(15)]),
        }
    )
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "accounts.parquet"
        pq.write_table(accounts, src)
        raw = {
            "version": 2,
            "global_settings": {"seed": 20261016},
            "sources": {"accounts": {"type": "file", "format": "parquet", "path": str(src)}},
            "targets": {
                "people": {"type": "file", "format": "csv", "path": "people.out.csv"},
                "accounts": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(Path(tmp) / "accounts.out.parquet"),
                },
            },
            "tables": [
                table(
                    "people",
                    5,
                    [
                        {
                            "name": "id",
                            "type": "categorical",
                            "categories": ["x", "y", "z", "w", "v"],
                        },
                        {"name": "nm", "type": "faker", "faker_type": "first_name"},
                    ],
                ),
                {
                    "name": "accounts",
                    "columns": [
                        {"name": "person_id", "strategy": "passthrough"},
                        {"name": "note", "strategy": "passthrough"},
                    ],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "people", "columns": ["id"]},
                    "children": [{"table": "accounts", "columns": ["person_id"]}],
                    "orphan_policy": "warn",
                    "namespace": "ns_people",
                }
            ],
        }
        cfg = PipelineConfig.model_validate(raw).model_dump()
        result = run_pipeline(cfg, sources={"accounts": accounts}, engine_version="f0")
    warnings = pa.table(
        {
            "code": [w.code for w in result.warnings],
            "column": [w.column for w in result.warnings],
            "orphan_rows": [int(w.detail.get("orphan_rows", -1)) for w in result.warnings],
        }
    )
    # run_pipeline outputs went through a pandas round trip, so their schema metadata embeds the
    # pyarrow version string. That is a dependency artifact, not generation behavior; drop it.
    outputs = {k: v.replace_schema_metadata(None) for k, v in result.outputs.items()}
    return {**outputs, "warnings": warnings}


def _gt(builder: Callable[[], dict[str, Any]], **kw: Any) -> Callable[[], dict[str, pa.Table]]:
    def run() -> dict[str, pa.Table]:
        return run_generate_tables(builder(), **kw)

    # The re-freeze manifest rebuilds each fixture's pinned input envelope from these.
    run.builder = builder  # type: ignore[attr-defined]
    run.kwargs = kw  # type: ignore[attr-defined]
    return run


FIXTURES: tuple[Fixture, ...] = (
    Fixture("sequence", ENTRY_GENERATE_TABLES, "sequence", _gt(cfg_sequence)),
    Fixture("categorical", ENTRY_GENERATE_TABLES, "categorical", _gt(cfg_categorical)),
    Fixture(
        "pooled_faker",
        ENTRY_GENERATE_TABLES,
        "pooled-faker",
        _gt(cfg_pooled_faker),
        note="50_000 rows = real N_THRESHOLD; three pooled columns (legacy type, two certified locale pairs)",
    ),
    Fixture("live_faker", ENTRY_GENERATE_TABLES, "live-faker", _gt(cfg_live_faker)),
    Fixture(
        "live_faker_default_locale",
        ENTRY_GENERATE_TABLES,
        "live-faker",
        _gt(cfg_live_faker_default_locale, instance_default_locale="fr_FR"),
    ),
    Fixture(
        "keyed_derive",
        ENTRY_GENERATE_TABLES,
        "keyed-derivation",
        _gt(cfg_keyed, derive_key=pinned_derive_key),
    ),
    Fixture("formula", ENTRY_GENERATE_TABLES, "formula", _gt(cfg_formula)),
    Fixture(
        "referenced_formula",
        ENTRY_GENERATE_TABLES,
        "referenced-formula",
        _gt(cfg_referenced_formula),
    ),
    Fixture("derived", ENTRY_GENERATE_TABLES, "derived", _gt(cfg_derived)),
    Fixture("reference", ENTRY_GENERATE_TABLES, "reference", _gt(cfg_reference)),
    Fixture(
        "derived_aggregate", ENTRY_GENERATE_TABLES, "derived_aggregate", _gt(cfg_derived_aggregate)
    ),
    Fixture("statistical", ENTRY_GENERATE_TABLES, "statistical", _gt(cfg_statistical)),
    Fixture("conditional", ENTRY_GENERATE_TABLES, "conditional-sampling", _gt(cfg_conditional)),
    Fixture(
        "grouped_windowed", ENTRY_GENERATE_TABLES, "grouped-windowed", _gt(cfg_grouped_windowed)
    ),
    Fixture("nulls", ENTRY_GENERATE_TABLES, "partial-null/all-null", _gt(cfg_nulls)),
    Fixture(
        "mixed_fk_generated_parent",
        ENTRY_RUN_PIPELINE,
        "generated-parent->masked-child FK",
        run_mixed_fk,
    ),
    Fixture(
        "composite_person_deterministic",
        ENTRY_COMPOSITE,
        "composite",
        run_composite_person_deterministic,
    ),
    Fixture(
        "composite_person_seeded_pool",
        ENTRY_COMPOSITE,
        "composite",
        run_composite_person_seeded_pool,
    ),
    Fixture(
        "composite_name_email_deterministic",
        ENTRY_COMPOSITE,
        "composite",
        run_composite_name_email_deterministic,
    ),
    Fixture(
        "composite_name_email_seeded_pool",
        ENTRY_COMPOSITE,
        "composite",
        run_composite_name_email_seeded_pool,
    ),
)

FIXTURES_BY_ID: dict[str, Fixture] = {f.fid: f for f in FIXTURES}
