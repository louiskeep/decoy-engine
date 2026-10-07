"""Shared builders for the C8-iii-a tests (`when:` on text_redact, bucket_perturb, date_shift).

Every comparison is against the oracle, so no value here needs a hand-written expected output.
A source has the target `v`, a string selector `k` and an integer passthrough `p`.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from tests.native._c6c_i_support import TEXTS, tr_col
from tests.native._chunked_bucket_perturb_support import bp_col, date_value
from tests.native._chunked_date_shift_support import ds_col
from tests.native._chunked_entry_support import NEEDS_COMPANION, passthrough

BAD_DATE = "not-a-date"
KIND_NAMES = ("text_redact", "bucket_perturb", "date_shift")
KINDS = [
    pytest.param("text_redact", id="text_redact"),
    pytest.param("bucket_perturb", id="bucket_perturb", marks=NEEDS_COMPANION),
    pytest.param("date_shift", id="date_shift", marks=NEEDS_COMPANION),
]

# Chunk sizes of a 23-row table; a zero is a zero-row chunk with the full schema.
N = 23
CHUNKINGS: dict[str, list[int]] = {
    "whole": [23],
    "even": [8, 8, 7],
    "ragged": [1, 0, 6, 3, 0, 9, 4],
}


def target(kind: str, name: str = "v", **extra: object) -> dict[str, object]:
    if kind == "text_redact":
        return {**tr_col(name), **extra}
    if kind == "bucket_perturb":
        return bp_col(name, **extra)
    return ds_col(name, **extra)


def value(kind: str, i: int) -> str:
    return TEXTS[i % len(TEXTS)] if kind == "text_redact" else date_value(i)


def values(kind: str, n: int, start: int = 0) -> list[str | None]:
    return [None if (start + i) % 5 == 2 else value(kind, start + i) for i in range(n)]


def selectors(n: int, start: int = 0) -> list[str | None]:
    return [
        None if (start + i) % 7 == 3 else ("x" if (start + i) % 3 == 0 else "y") for i in range(n)
    ]


def make_table(
    kind: str,
    n: int = N,
    start: int = 0,
    *,
    bad: tuple[int, ...] = (),
    v: list[str | None] | None = None,
    k: list[str | None] | None = None,
) -> pa.Table:
    vals = list(v) if v is not None else values(kind, n, start)
    for pos in bad:
        vals[pos] = BAD_DATE
    sel = list(k) if k is not None else selectors(len(vals), start)
    return pa.table(
        {
            "v": pa.array(vals, pa.string()),
            "k": pa.array(sel, pa.string()),
            "p": pa.array(list(range(start, start + len(vals))), pa.int64()),
        }
    )


def chunk_by_sizes(table: pa.Table, sizes: list[int]) -> list[pa.Table]:
    out, at = [], 0
    for size in sizes:
        out.append(table.slice(at, size))
        at += size
    assert at == table.num_rows
    return out


def columns(kind: str, when: str | None, **extra: object) -> list[dict[str, object]]:
    col = target(kind, **extra)
    if when is not None:
        col["when"] = when
    return [col, passthrough("k"), passthrough("p")]


def predicates(kind: str) -> dict[str, str]:
    one, two = value(kind, 0), value(kind, 1)
    return {
        "zero": "k == 'zzz'",
        "partial": "k == 'x'",
        "all_non_null": "k in ['x', 'y']",
        "all_rows": "k != 'zzz'",
        "sibling_not_in": "k not in ['x']",
        "target_eq": f"v == '{one}'",
        "target_in": f"v in ['{one}', '{two}']",
        "target_ne": f"v != '{one}'",
        "target_not_in": f"v not in ['{one}']",
    }


# Unmasked bucket_perturb jobs that succeed natively on main. Their output is recorded in
# `_c8_iii_a_main_goldens.json` and must not change when the special-format gate moves them to
# the oracle.
SPECIAL_FORMATS = ("mixed", "ISO8601")
SPECIAL_FORMAT_JOBS: dict[str, list[list[str | None]]] = {
    "ordinary_dates": [
        [date_value(0), date_value(1), None],
        [date_value(2), "junk"],
        [],
        [date_value(3)],
    ],
    "uniform_offset": [
        ["2024-01-15T12:00:00+01:00", "2024-03-02T08:30:00+01:00", None],
        ["2024-06-09T23:59:59+01:00"],
    ],
    "all_null": [[None, None], [None]],
    "empty": [[]],
}
# Offsets that differ across the values: the oracle parses them, native does not.
MIXED_OFFSET_VALUES = ["2024-01-15T12:00:00+01:00", "2024-07-15T12:00:00+02:00"]


def declines() -> dict[str, tuple[list[dict[str, Any]], str]]:
    return {
        "ner_text_redact": (
            [
                {**target("text_redact"), "provider_config": {"ner": True}},
                passthrough("k"),
                passthrough("p"),
            ],
            "text_redact",
        ),
        "implicit_format_bucket_perturb": (
            [bp_col("v", date_format=None), passthrough("k"), passthrough("p")],
            "bucket_perturb",
        ),
        "implicit_format_date_shift": (
            [ds_col("v", date_format=None), passthrough("k"), passthrough("p")],
            "date_shift",
        ),
        "date_shift_group_by": (
            [ds_col("v", group_by="k"), passthrough("k"), passthrough("p")],
            "date_shift",
        ),
        "positional_categorical": (
            [
                {
                    "name": "v",
                    "strategy": "categorical",
                    "namespace": "ns_c",
                    "provider_config": {"categories": ["a", "b"]},
                },
                passthrough("k"),
                passthrough("p"),
            ],
            "text_redact",
        ),
        "positional_faker": (
            [
                {
                    "name": "v",
                    "strategy": "faker",
                    "provider": "person_first_name",
                    "pool_size": 40,
                },
                passthrough("k"),
                passthrough("p"),
            ],
            "text_redact",
        ),
        "deterministic_faker": (
            [
                {
                    "name": "v",
                    "strategy": "faker",
                    "provider": "person_first_name",
                    "deterministic": True,
                    "namespace": "ns_f",
                    "pool_size": 40,
                },
                passthrough("k"),
                passthrough("p"),
            ],
            "text_redact",
        ),
        "group_key": (
            [
                {
                    "name": "v",
                    "strategy": "group_key",
                    "namespace": "ns_g",
                    "provider_config": {"group_by": "k", "length": 8},
                },
                passthrough("k"),
                passthrough("p"),
            ],
            "text_redact",
        ),
        "text_mask": (
            [
                {"name": "v", "strategy": "text_mask", "provider_config": {"detectors": ["ssn"]}},
                passthrough("k"),
                passthrough("p"),
            ],
            "text_redact",
        ),
        "windowed_date": (
            [
                {
                    "name": "v",
                    "strategy": "windowed_date",
                    "provider_config": {"anchor": "k", "max_days": 10},
                },
                passthrough("k"),
                passthrough("p"),
            ],
            "date_shift",
        ),
        "top_code": (
            [
                {"name": "v", "strategy": "top_code", "provider_config": {"preset": "hipaa_age"}},
                passthrough("k"),
                passthrough("p"),
            ],
            "text_redact",
        ),
        "code_set": (
            [
                {
                    "name": "v",
                    "strategy": "code_set",
                    "namespace": "ns_cs",
                    "provider_config": {"code_set": "icd10"},
                },
                passthrough("k"),
                passthrough("p"),
            ],
            "text_redact",
        ),
    }
