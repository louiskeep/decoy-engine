"""Task 4.4 C3/C6: the acceptance corpus.

One fixed slice schema mixing all four strategies (passthrough, redact,
truncate, hash) drives the batch-size x row-order matrix
(`tests/parity/native/test_phase2_gate.py:213`'s established pattern) and
doubles as the mixed-4-strategy case; separate minimal single-column
configs cover each strategy alone, redact/truncate variants, hash
truncation, and the null-density/all-null/empty degenerate shapes. Every
job runs through `run_shadow_and_oracle` + `assert_shadow_matches_oracle`
(C3's exit gate: value/null/order/row-count/schema hard failures,
diagnostics as multisets, planned==actual route evidence, positive
compiled-kernel call evidence for every hash node).
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.keyprovider import SecretKeyProvider
from tests.physical._shadow_helpers import (
    assert_every_node_bound,
    assert_route_evidence_matches_plan,
    assert_shadow_matches_oracle,
    build_config,
    run_shadow_and_oracle,
    write_read_only_fixture,
)

_MASK_KEY = bytes(range(32))


def _key_provider() -> SecretKeyProvider:
    return SecretKeyProvider(secret=_MASK_KEY, key_version="v1")


def _verify(
    tmp_path: Path, table_name: str, source: pa.Table, columns: list[dict], *, name: str
) -> None:
    path = write_read_only_fixture(tmp_path, source, name)
    config = build_config(tmp_path, table_name, path, columns)
    run = run_shadow_and_oracle(config, table_name, source, key_provider=_key_provider())
    assert_every_node_bound(run.plan)
    assert_shadow_matches_oracle(run)
    assert_route_evidence_matches_plan(run)


# ---------------------------------------------------------------------------
# The fixed mixed-4-strategy schema, reused for the batch-size x row-order
# matrix (which therefore also IS the mixed-4-strategy case) and the
# null-density case.
# ---------------------------------------------------------------------------

_FIXED_COLUMNS = [
    {"name": "h_email", "strategy": "hash", "namespace": "ns_email"},
    {"name": "h_uid", "strategy": "hash", "namespace": "ns_uid"},
    {"name": "pt_amount", "strategy": "passthrough"},
    {"name": "pt_flag", "strategy": "passthrough"},
    {"name": "pt_note", "strategy": "passthrough"},
    {"name": "rd_ssn", "strategy": "redact"},
    {"name": "tr_phone", "strategy": "truncate", "provider_config": {"length": 3, "keep": "head"}},
    {"name": "tr_card", "strategy": "truncate", "provider_config": {"length": 4, "keep": "tail"}},
]


def _build_fixed_source(n_rows: int, *, reverse: bool = False) -> pa.Table:
    idx = list(range(n_rows))
    if reverse:
        idx = list(reversed(idx))
    _str = pa.string()
    return pa.table(
        {
            "h_email": pa.array([f"user{i}@example.com" for i in idx], type=_str),
            "h_uid": pa.array([100_000_000 + i for i in idx], type=pa.int64()),
            "pt_amount": pa.array([(i * 13) % 1_000_000 for i in idx], type=pa.int64()),
            "pt_flag": pa.array([i % 2 == 0 for i in idx], type=pa.bool_()),
            # Null density: every 5th row is null (natural-order index; the
            # reversed source carries the same null POSITIONS relative to its
            # own row order, exercising order-preservation over real nulls).
            "pt_note": pa.array([None if i % 5 == 0 else f"note-{i}" for i in idx], type=_str),
            "rd_ssn": pa.array([f"5{i % 900:03d}-11-2222" for i in idx], type=_str),
            "tr_phone": pa.array([f"512{i % 9000:04d}" for i in idx], type=_str),
            "tr_card": pa.array([f"4000{i % 9999:04d}" for i in idx], type=_str),
        }
    )


_N_ROWS = 37  # not a multiple of any batch size below: exercises a ragged final chunk
_BATCH_SIZES = (1, 4, 11)
_ORDERS = (False, True)  # natural, then a fixed reversal


@pytest.mark.parametrize("reverse", _ORDERS, ids=["natural_order", "reversed_order"])
@pytest.mark.parametrize("batch_size", _BATCH_SIZES, ids=[f"batch_{b}" for b in _BATCH_SIZES])
def test_mixed_four_strategy_table_batch_size_x_row_order_matrix(
    tmp_path: Path, batch_size: int, reverse: bool
) -> None:
    source = _build_fixed_source(_N_ROWS, reverse=reverse)
    path = write_read_only_fixture(tmp_path, source, f"mixed_{batch_size}_{reverse}")
    config = build_config(tmp_path, "w", path, _FIXED_COLUMNS)
    run = run_shadow_and_oracle(
        config, "w", source, key_provider=_key_provider(), batch_size_rows=batch_size
    )
    assert_every_node_bound(run.plan)
    assert_shadow_matches_oracle(run)
    assert_route_evidence_matches_plan(run)


# ---------------------------------------------------------------------------
# Each strategy alone.
# ---------------------------------------------------------------------------


def test_passthrough_alone(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    _verify(
        tmp_path, "t", source, [{"name": "c", "strategy": "passthrough"}], name="passthrough_alone"
    )


def test_config_column_order_differs_from_source_matches_oracle(tmp_path: Path) -> None:
    """Codex final-gate HIGH: the pandas oracle preserves SOURCE column order,
    not config/node declaration order. Source is [a, b] but the config lists
    the columns [b, a]; both sides must still emit [a, b]. Pre-fix the shadow
    assembled in config order and diverged with a schema-diff."""
    source = pa.table(
        {
            "a": pa.array(["a0", "a1", "a2"], type=pa.string()),
            "b": pa.array(["b0", "b1", "b2"], type=pa.string()),
        }
    )
    columns = [
        {"name": "b", "strategy": "redact"},
        {"name": "a", "strategy": "hash", "namespace": "n"},
    ]
    _verify(tmp_path, "t", source, columns, name="config_order_permuted")


def test_passthrough_large_string_normalizes_to_string_matches_oracle(tmp_path: Path) -> None:
    """Codex final-gate HIGH: a non-empty large_string passthrough column
    round-trips through the pandas oracle as `string`; the shadow assembly
    must match, not emit `large_string`. Driven through the LIVE oracle."""
    source = pa.table({"c": pa.array(["x", "y", "z"], type=pa.large_string())})
    _verify(tmp_path, "t", source, [{"name": "c", "strategy": "passthrough"}], name="pt_large_str")


def test_passthrough_all_null_bool_normalizes_to_null_matches_oracle(tmp_path: Path) -> None:
    """Codex final-gate HIGH: an all-null bool passthrough column round-trips
    through the pandas oracle as `null`, not `bool`. Driven through the LIVE
    oracle so a future pandas promotion change is caught by the real compare."""
    source = pa.table({"c": pa.array([None, None, None], type=pa.bool_())})
    _verify(
        tmp_path, "t", source, [{"name": "c", "strategy": "passthrough"}], name="pt_allnull_bool"
    )


def test_passthrough_all_null_string_normalizes_to_null_matches_oracle(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array([None, None], type=pa.string())})
    _verify(
        tmp_path, "t", source, [{"name": "c", "strategy": "passthrough"}], name="pt_allnull_str"
    )


def test_passthrough_partial_null_int_upcasts_to_float_matches_oracle(tmp_path: Path) -> None:
    """dennis MEDIUM-1: drive the int64+null -> float64 passthrough oracle
    quirk through the LIVE oracle comparison, not just a pinned unit test. If a
    future pandas/pyarrow bump changes the oracle's int+null promotion, the
    coordinator's output-assembly must drift WITH it or this fails on the real
    schema/value compare (a hardcoded unit expectation would silently pass)."""
    source = pa.table({"c": pa.array([1, None, 3], type=pa.int64())})
    _verify(tmp_path, "t", source, [{"name": "c", "strategy": "passthrough"}], name="pt_int_null")


def test_redact_alone(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    _verify(tmp_path, "t", source, [{"name": "c", "strategy": "redact"}], name="redact_alone")


def test_redact_custom_redact_with_variant(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    columns = [{"name": "c", "strategy": "redact", "provider_config": {"redact_with": "***"}}]
    _verify(tmp_path, "t", source, columns, name="redact_custom")


def test_truncate_alone(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["abcdefgh", "ijklmnop", "qrstuvwx"], type=pa.string())})
    columns = [{"name": "c", "strategy": "truncate", "provider_config": {"length": 3}}]
    _verify(tmp_path, "t", source, columns, name="truncate_alone")


def test_hash_alone(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    columns = [{"name": "c", "strategy": "hash", "namespace": "n"}]
    _verify(tmp_path, "t", source, columns, name="hash_alone")


# ---------------------------------------------------------------------------
# Truncate variants: head / tail / legacy from_end / mask_char.
# ---------------------------------------------------------------------------


def test_truncate_keep_head(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["abcdefgh", "ijklmnop"], type=pa.string())})
    columns = [
        {"name": "c", "strategy": "truncate", "provider_config": {"length": 3, "keep": "head"}}
    ]
    _verify(tmp_path, "t", source, columns, name="truncate_head")


def test_truncate_keep_tail(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["abcdefgh", "ijklmnop"], type=pa.string())})
    columns = [
        {"name": "c", "strategy": "truncate", "provider_config": {"length": 3, "keep": "tail"}}
    ]
    _verify(tmp_path, "t", source, columns, name="truncate_tail")


def test_truncate_legacy_from_end(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["abcdefgh", "ijklmnop"], type=pa.string())})
    columns = [
        {"name": "c", "strategy": "truncate", "provider_config": {"length": 4, "from_end": True}}
    ]
    _verify(tmp_path, "t", source, columns, name="truncate_legacy")


def test_truncate_mask_char(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["abcdefgh", "ijklmnop"], type=pa.string())})
    columns = [
        {
            "name": "c",
            "strategy": "truncate",
            "provider_config": {"length": 3, "keep": "head", "mask_char": "*"},
        }
    ]
    _verify(tmp_path, "t", source, columns, name="truncate_mask_char")


# ---------------------------------------------------------------------------
# Hash truncation (the config token, distinct from the truncate strategy).
# ---------------------------------------------------------------------------


def test_hash_with_truncate_config(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    columns = [
        {"name": "c", "strategy": "hash", "namespace": "n", "provider_config": {"truncate": 16}}
    ]
    _verify(tmp_path, "t", source, columns, name="hash_truncate_config")


def test_hash_admitted_int_type(tmp_path: Path) -> None:
    # Null-bearing ints are rejected under hash (`reject_null_bearing_int`);
    # the admitted native-hash int case is exercised null-free.
    source = pa.table({"c": pa.array([1, 22, 333], type=pa.int64())})
    columns = [{"name": "c", "strategy": "hash", "namespace": "n"}]
    _verify(tmp_path, "t", source, columns, name="hash_int")


# ---------------------------------------------------------------------------
# Null-density / all-null / empty, per strategy.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("strategy", "values", "arrow_type", "columns"),
    [
        ("passthrough", ["a", None, "b"], pa.string(), [{"name": "c", "strategy": "passthrough"}]),
        ("redact", ["a", None, "b"], pa.string(), [{"name": "c", "strategy": "redact"}]),
        (
            "truncate",
            ["abcdef", None, "xyz"],
            pa.string(),
            [{"name": "c", "strategy": "truncate", "provider_config": {"length": 3}}],
        ),
        (
            "hash",
            ["a", None, "b"],
            pa.string(),
            [{"name": "c", "strategy": "hash", "namespace": "n"}],
        ),
    ],
    ids=["passthrough", "redact", "truncate", "hash"],
)
def test_null_density(tmp_path, strategy, values, arrow_type, columns) -> None:
    source = pa.table({"c": pa.array(values, type=arrow_type)})
    _verify(tmp_path, "t", source, columns, name=f"nulldensity_{strategy}")


@pytest.mark.parametrize(
    ("strategy", "arrow_type", "columns"),
    [
        ("passthrough", pa.string(), [{"name": "c", "strategy": "passthrough"}]),
        ("passthrough", pa.int64(), [{"name": "c", "strategy": "passthrough"}]),
        ("redact", pa.string(), [{"name": "c", "strategy": "redact"}]),
        (
            "truncate",
            pa.string(),
            [{"name": "c", "strategy": "truncate", "provider_config": {"length": 3}}],
        ),
        ("hash", pa.string(), [{"name": "c", "strategy": "hash", "namespace": "n"}]),
    ],
    ids=["passthrough_str", "passthrough_int", "redact", "truncate", "hash"],
)
def test_all_null(tmp_path, strategy, arrow_type, columns) -> None:
    source = pa.table({"c": pa.array([None, None], type=arrow_type)})
    _verify(tmp_path, "t", source, columns, name=f"allnull_{strategy}_{arrow_type}")


@pytest.mark.parametrize(
    ("strategy", "arrow_type", "columns"),
    [
        ("passthrough", pa.string(), [{"name": "c", "strategy": "passthrough"}]),
        ("passthrough", pa.int64(), [{"name": "c", "strategy": "passthrough"}]),
        ("passthrough", pa.bool_(), [{"name": "c", "strategy": "passthrough"}]),
        ("redact", pa.string(), [{"name": "c", "strategy": "redact"}]),
        (
            "truncate",
            pa.string(),
            [{"name": "c", "strategy": "truncate", "provider_config": {"length": 3}}],
        ),
        ("hash", pa.string(), [{"name": "c", "strategy": "hash", "namespace": "n"}]),
        ("hash", pa.int64(), [{"name": "c", "strategy": "hash", "namespace": "n"}]),
    ],
    ids=[
        "passthrough_str",
        "passthrough_int",
        "passthrough_bool",
        "redact",
        "truncate",
        "hash_str",
        "hash_int",
    ],
)
def test_empty(tmp_path, strategy, arrow_type, columns) -> None:
    source = pa.table({"c": pa.array([], type=arrow_type)})
    _verify(tmp_path, "t", source, columns, name=f"empty_{strategy}_{arrow_type}")
