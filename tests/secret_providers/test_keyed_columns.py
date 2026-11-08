"""AT-E7: pinned oracle and parity for `keyed_columns_for_config` (spec 16.12).

Expected results are written out, never computed from `column_is_keyed`, so a mutation of the
shared column walk (which both functions use) still fails the oracle table.
"""

from __future__ import annotations

import pytest

from decoy_engine.secret_providers import (
    KeyedColumn,
    keyed_columns_for_config,
    secret_requirements_for_config,
)


def _cfg(*cols, table="t", **extra):
    return {"tables": [{"name": table, "columns": list(cols), **extra}]}


def _col(name, strategy, **kw):
    return {"name": name, "strategy": strategy, **kw}


K = KeyedColumn

ALWAYS_KEYED = (
    "fpe",
    "hash",
    "date_shift",
    "shuffle",
    "bucket_perturb",
    "text_mask",
    "code_set",
    "grouped_series",
    "windowed_date",
    "group_key",
    "joint_mask",
    "tokenize",
)
KNOWN_UNKEYED = (
    "bucketize",
    "categorical",
    "derived",
    "derived_aggregate",
    "faker",
    "formula",
    "from_parent",
    "geo_generalize",
    "passthrough",
    "redact",
    "text_redact",
    "top_code",
    "truncate",
)

ORACLE = [
    ("empty", {}, ()),
    ("redact only", _cfg(_col("a", "redact")), ()),
    ("faker plain", _cfg(_col("a", "faker")), ()),
    ("faker deterministic", _cfg(_col("a", "faker", deterministic=True)), (K("t", "a", "faker"),)),
    (
        "allow_collisions counts as deterministic",
        _cfg(_col("a", "faker", allow_collisions=True)),
        (K("t", "a", "faker"),),
    ),
    ("vault column", _cfg(_col("a", "redact", vault=True)), (K("t", "a", "redact"),)),
    ("unknown strategy is keyed", _cfg(_col("a", "mystery")), (K("t", "a", "mystery"),)),
    ("missing strategy is keyed", _cfg({"name": "a"}), (K("t", "a", None),)),
    (
        "nested unkeyed child",
        _cfg(_col("a", "nested", provider_config={"strategy": "faker"})),
        (),
    ),
    (
        "nested keyed child",
        _cfg(_col("a", "nested", provider_config={"strategy": "hash"})),
        (K("t", "a", "nested"),),
    ),
    (
        "nested deterministic child",
        _cfg(
            _col(
                "a",
                "nested",
                provider_config={"strategy": "faker", "strategy_config": {"deterministic": True}},
            )
        ),
        (K("t", "a", "nested"),),
    ),
    (
        "nested of nested",
        _cfg(_col("a", "nested", provider_config={"strategy": "nested"})),
        (K("t", "a", "nested"),),
    ),
    (
        "nested unknown child",
        _cfg(_col("a", "nested", provider_config={"strategy": "mystery"})),
        (K("t", "a", "nested"),),
    ),
    (
        "generate table skipped",
        {
            "tables": [
                {"name": "g", "generate_columns": [{"name": "n", "type": "int"}], "columns": []},
                {"name": "t", "columns": [_col("a", "hash")]},
            ]
        },
        (K("t", "a", "hash"),),
    ),
    (
        "generate table with keyed columns still skipped",
        _cfg(_col("a", "hash"), generate_columns=[{"name": "n", "type": "int"}]),
        (),
    ),
    (
        "config order across tables and columns",
        {
            "tables": [
                {"name": "z", "columns": [_col("b", "hash"), _col("a", "fpe")]},
                {"name": "a", "columns": [_col("x", "redact"), _col("c", "shuffle")]},
            ]
        },
        (K("z", "b", "hash"), K("z", "a", "fpe"), K("a", "c", "shuffle")),
    ),
    (
        "non-dict entries ignored",
        {"tables": ["junk", {"name": "t", "columns": ["junk", _col("a", "hash")]}]},
        (K("t", "a", "hash"),),
    ),
    (
        "composite parent key has no single field",
        {
            **_cfg(_col("a", "redact")),
            "relationships": [
                {"parent": {"table": "p", "columns": ["x", "y"]}, "children": []},
            ],
        },
        (),
    ),
    (
        "missing table and column names are kept",
        {"tables": [{"columns": [{"strategy": "hash"}]}]},
        (K(None, None, "hash"),),
    ),
    ("relationship with no parent", {"relationships": [{"children": []}]}, ()),
    ("relationship that is not a mapping", {"relationships": ["x"]}, ()),
    *[(f"always keyed {s}", _cfg(_col("a", s)), (K("t", "a", s),)) for s in ALWAYS_KEYED],
    *[(f"known unkeyed {s}", _cfg(_col("a", s)), ()) for s in KNOWN_UNKEYED],
    *[
        (f"deterministic flips {s}", _cfg(_col("a", s, deterministic=True)), (K("t", "a", s),))
        for s in KNOWN_UNKEYED
    ],
]

IDS = [c[0] for c in ORACLE]


@pytest.mark.parametrize(("config", "expected"), [c[1:] for c in ORACLE], ids=IDS)
def test_keyed_columns_oracle(config, expected):
    assert keyed_columns_for_config(config) == expected


@pytest.mark.parametrize(("config", "expected"), [c[1:] for c in ORACLE], ids=IDS)
def test_secret_needed_exactly_when_keyed_or_composite_parent(config, expected):
    # A relationship whose parent is missing or malformed also needs the secret: the classifier
    # cannot prove it is a single-column key, so it fails safe.
    composite = any(
        not isinstance(r, dict)
        or not isinstance(r.get("parent"), dict)
        or len(r["parent"].get("columns") or []) > 1
        for r in config.get("relationships") or []
    )
    assert bool(secret_requirements_for_config(config)) == (bool(expected) or composite)


def test_oracle_covers_every_catalog_strategy():
    from decoy_engine.secret_providers._requirements import (
        ALWAYS_KEYED_STRATEGIES,
        KNOWN_UNKEYED_STRATEGIES,
    )

    assert set(ALWAYS_KEYED) == ALWAYS_KEYED_STRATEGIES
    assert set(KNOWN_UNKEYED) == KNOWN_UNKEYED_STRATEGIES


def test_keyed_column_is_frozen_and_value_free():
    col = KeyedColumn("t", "a", "hash")
    with pytest.raises(AttributeError):
        col.column = "b"  # type: ignore[misc]
    assert set(vars(col)) == {"table", "column", "strategy"}
