"""Phase D / D1a step 1: pin the pre-D1a OOC hash output (test-only, no source change).

Every expected token is a hardcoded literal in `_d1a_hash_data.py`, captured from the
pure-Python `hash_array` before the native route existed. The same suite stays green
when D1a routes admitted columns through the compiled kernel: native output must
reproduce these bytes for shared-Unicode-version input, so a drift in either route
fails here. It runs the payload columns and the parent-key column `hk` through both
drivers and both output paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.keyprovider import SecretKeyProvider
from tests.parity._d1a_hash_data import (
    EXPECTED_TOKENS,
    SECRET,
    column_seed,
    hash_seeds,
    run_chain,
    source_columns,
)
from tests.parity.test_phase_d_d0_baseline import _H

_TYPED = list(source_columns())
_SEEDS = (
    *hash_seeds(_TYPED),
    ("c_trunc", column_seed(provider_config=(("truncate", 12),))),
    ("c_other_ns", column_seed(namespace="ns_other")),
)
_PAYLOAD = ["c_trunc", "c_other_ns"]
# (id, run kwargs builder): resident, sink via the batch driver, sink via the reorder driver.
_MODES = ["resident", "sink-batch", "sink-reorder"]


def mode_kwargs(mode: str, tmp_path: Path) -> dict[str, Any]:
    if mode == "resident":
        return {}
    return {"out_dir": tmp_path / "out", "reorder": mode == "sink-reorder"}


def _payload() -> pa.Table:
    cols = source_columns()
    return pa.table({**cols, "c_trunc": cols["c_utf8"], "c_other_ns": cols["c_utf8"]})


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("batch_rows", [None, 3, 1])
def test_ooc_hash_matches_the_pinned_baseline(
    mode: str, batch_rows: int | None, tmp_path: Path
) -> None:
    run = run_chain(_payload(), _SEEDS, batch_rows=batch_rows, **mode_kwargs(mode, tmp_path))
    out = run.tables["t"]
    for name in (*_TYPED, *_PAYLOAD, "hk"):
        got = out.column(name)
        assert got.type == pa.string(), name
        assert got.to_pylist() == EXPECTED_TOKENS[name], name
    # The parent-key relation hands the masked key to the child unchanged.
    assert run.tables["child"].column("ck").to_pylist() == EXPECTED_TOKENS["hk"]


def test_secret_backed_mask_key_differs_from_job_seed_and_matches_the_pin() -> None:
    table = pa.table({"c_utf8_secret": source_columns()["c_utf8"]})
    seeds = (("c_utf8_secret", column_seed()),)
    run = run_chain(table, seeds, key_provider=SecretKeyProvider(SECRET))
    got = run.tables["t"].column("c_utf8_secret").to_pylist()
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
    run = run_chain(table, (("k", column_seed(namespace="ns_p")),))
    assert run.tables["t"].column("k").to_pylist() == list(_H)
