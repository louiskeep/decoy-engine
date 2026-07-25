"""C3c primitive 1: streaming per-batch mask parity.

`mask_batch` must reproduce `mask_table` exactly when a table is masked one
RecordBatch at a time: the kernel arrays (kernel/_scalar.py) are per-value
deterministic, so a batch's masked column must equal the corresponding
row-slice of the whole-column mask, byte for byte, for every admitted strategy
(hash, redact, truncate, passthrough). These tests pin that equality plus the
skip-column and not-in-plan behaviors the runner relies on.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution.out_of_core._mask import mask_batch, mask_column, mask_table
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed

_SEED = b"\x00" * 8


def _col(
    strategy: str,
    *,
    namespace: str | None = None,
    provider_config: tuple[tuple[str, Any], ...] = (),
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=namespace is not None,
        provider_config=provider_config,
        coherent_with=(),
    )


def _plan() -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "people",
                    TableSeed(
                        per_column=(
                            ("name", _col("hash", namespace="name_ns")),
                            ("email", _col("redact", provider_config=(("redact_with", "X"),))),
                            ("phone", _col("truncate", provider_config=(("length", 3),))),
                            ("age", _col("passthrough")),
                            ("fk_key", _col("hash", namespace="fk_ns")),
                            ("planned_but_absent", _col("redact")),
                        ),
                        per_group=(),
                    ),
                ),
            ),
        )
    )


def _table() -> pa.Table:
    n = 8
    return pa.table(
        {
            "name": [f"person-{i}" if i % 4 else None for i in range(n)],
            "email": [f"p{i}@example.com" if i % 3 else None for i in range(n)],
            "phone": [f"555-010{i}" for i in range(n)],
            "age": list(range(20, 20 + n)),
            "fk_key": [f"k{i}" for i in range(n)],
            "unplanned": [f"u{i}" for i in range(n)],
        }
    )


_SKIP = frozenset({"fk_key"})


def test_batch_mask_concat_equals_whole_table_mask() -> None:
    plan = _plan()
    table = _table()
    whole = mask_table(plan, "people", table, skip_columns=_SKIP)

    masked_batches = [
        mask_batch(plan, "people", batch, skip_columns=_SKIP)
        for batch in table.to_batches(max_chunksize=3)
    ]
    rebuilt = pa.Table.from_batches(masked_batches)

    assert rebuilt.schema == whole.schema
    assert rebuilt.equals(whole)


def test_each_batch_equals_whole_mask_row_slice() -> None:
    plan = _plan()
    table = _table()
    whole = mask_table(plan, "people", table, skip_columns=_SKIP)

    start = 0
    for batch in table.to_batches(max_chunksize=3):
        masked = mask_batch(plan, "people", batch, skip_columns=_SKIP)
        expected = whole.slice(start, batch.num_rows)
        assert pa.Table.from_batches([masked]).to_pydict() == expected.to_pydict()
        start += batch.num_rows
    assert start == table.num_rows


def test_mask_batch_leaves_skip_and_unplanned_columns_untouched() -> None:
    plan = _plan()
    table = _table()

    for batch in table.to_batches(max_chunksize=3):
        masked = mask_batch(plan, "people", batch, skip_columns=_SKIP)
        assert masked.column("fk_key").equals(batch.column("fk_key"))
        assert masked.column("unplanned").equals(batch.column("unplanned"))
        assert masked.schema.field("fk_key").type == batch.schema.field("fk_key").type


def test_all_null_redact_batch_yields_null_type_and_breaks_concat() -> None:
    # Executable tripwire for the documented redact caveat: an all-null batch
    # infers a null-typed column where a valued batch carries the redact_with
    # type, so a fixed-schema consumer crashes instead of corrupting. The
    # streaming runner owns reconciling this; hash and truncate always pin
    # pa.string(), so redact is the only strategy that can diverge.
    plan = _plan()
    valued = pa.RecordBatch.from_pydict({"email": pa.array(["a@example.com", None])})
    all_null = pa.RecordBatch.from_pydict({"email": pa.array([None, None], type=pa.string())})

    masked_valued = mask_batch(plan, "people", valued)
    masked_null = mask_batch(plan, "people", all_null)

    assert masked_valued.column("email").type == pa.string()
    assert masked_null.column("email").type == pa.null()
    with pytest.raises(pa.ArrowInvalid):
        pa.Table.from_batches([masked_valued, masked_null])


def test_mask_batch_table_absent_from_plan_returns_batch_unchanged() -> None:
    plan = _plan()
    table = _table()

    batch = table.to_batches(max_chunksize=4)[0]
    masked = mask_batch(plan, "not_in_plan", batch, skip_columns=frozenset())

    assert masked.equals(batch)


@pytest.mark.parametrize(
    ("provider_config", "code"),
    [
        ((("length", 0),), "truncate_length_invalid"),
        ((("length", -1),), "truncate_length_invalid"),
        ((("length", "x"),), "truncate_length_invalid"),
        ((("length", None),), "truncate_length_invalid"),
        ((("length", 3), ("keep", "middle")), "truncate_keep_invalid"),
        ((("length", 3), ("mask_char", "**")), "truncate_mask_char_invalid"),
    ],
)
def test_truncate_invalid_config_fails_closed_not_raw_passthrough(provider_config, code) -> None:
    """SC1 round-6 P1 (raw-leak backstop) regression.

    An invalid truncate config (bad length/keep/mask_char) previously fell back
    to `passthrough_array`, publishing the RAW, unmasked column -- a PII / raw-FK
    leak. It must instead raise `StrategyError` with the SAME code the pandas
    oracle (`_strategies/_truncate.py`) raises for that shape, never emit the
    source values.
    """
    seed = _col("truncate", provider_config=provider_config)
    values = pa.array(["raw-secret-0", "raw-secret-1"])
    with pytest.raises(StrategyError) as exc:
        mask_column(values, seed, _SEED)
    assert exc.value.code == code
    assert exc.value.strategy == "truncate"
