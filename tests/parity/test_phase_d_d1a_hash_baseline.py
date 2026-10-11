"""Phase D / D1a step 1: pin the pre-D1a OOC hash output (test-only, no source change).

Every expected token is a hardcoded literal in `_d1a_hash_data.py`, captured from the
pure-Python `hash_array` before the native route existed. The same suite stays green
when D1a routes admitted columns through the compiled kernel: native output must
reproduce these bytes for shared-Unicode-version input, so a drift in either route
fails here.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.keyprovider import SecretKeyProvider
from tests.parity._d1a_hash_data import (
    EXPECTED_TOKENS,
    NS,
    SECRET,
    column_seed,
    hash_seeds,
    run_single_table,
    source_columns,
)
from tests.parity.test_phase_d_d0_baseline import _H

_TYPED = [n for n in source_columns()]


def _table_with_extras() -> pa.Table:
    cols = source_columns()
    return pa.table(
        {
            **cols,
            "c_trunc": cols["c_utf8"],
            "c_other_ns": cols["c_utf8"],
            "c_utf8_secret": cols["c_utf8"],
        }
    )


_SEEDS = (
    *hash_seeds(_TYPED),
    ("c_trunc", column_seed(provider_config=(("truncate", 12),))),
    ("c_other_ns", column_seed(namespace="ns_other")),
)


@pytest.mark.parametrize("batch_rows", [None, 3, 1])
def test_ooc_hash_matches_the_pinned_baseline(batch_rows: int | None) -> None:
    table = _table_with_extras().select([*_TYPED, "c_trunc", "c_other_ns"])
    out = run_single_table(table, _SEEDS, batch_rows=batch_rows).outputs["t"]
    for name, want in EXPECTED_TOKENS.items():
        if name == "c_utf8_secret":
            continue
        got = out.column(name)
        assert got.type == pa.string(), name
        assert got.to_pylist() == want, name


def test_secret_backed_mask_key_differs_from_job_seed_and_matches_the_pin() -> None:
    table = pa.table({"c_utf8_secret": source_columns()["c_utf8"]})
    seeds = (("c_utf8_secret", column_seed()),)
    out = run_single_table(table, seeds, key_provider=SecretKeyProvider(SECRET)).outputs["t"]
    got = out.column("c_utf8_secret").to_pylist()
    assert got == EXPECTED_TOKENS["c_utf8_secret"]
    assert got != EXPECTED_TOKENS["c_utf8"]


def test_composed_and_decomposed_text_hash_to_one_token() -> None:
    tokens = EXPECTED_TOKENS["c_utf8"]
    assert tokens[4] == tokens[5]  # "e" + U+0301 vs U+00E9


def test_namespace_binds_the_token() -> None:
    assert EXPECTED_TOKENS["c_other_ns"] != EXPECTED_TOKENS["c_utf8"]
    assert EXPECTED_TOKENS["c_trunc"] == [
        None if t is None else t[:12] for t in EXPECTED_TOKENS["c_utf8"]
    ]


def test_int64_anchor_matches_the_d0_literals() -> None:
    # Independent anchor: the D0 baseline pinned these tokens (job_seed 0x11*8, ns_p).
    table = pa.table({"k": pa.array([0, 1, 2], pa.int64())})
    out = run_single_table(table, (("k", column_seed(namespace="ns_p")),)).outputs["t"]
    assert out.column("k").to_pylist() == list(_H)
    assert NS == "ns_h"
