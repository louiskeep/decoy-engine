"""Phase F F0: in-process repeatability of the composite_person samplers.

The checked-in oracle digest (`_f0_expected`) proves cross-venv stability. This module proves
the narrower, independent property that generating a fixture twice in one process with the same
seed gives byte-identical output, so the oracle match cannot be hiding run-to-run drift. It
relies on the autouse frozen Faker clock in `conftest.py`, which applies because the module name
starts with `test_phase_f_f0_`.
"""

from __future__ import annotations

from collections.abc import Callable

import pyarrow as pa
import pytest

from tests.parity import _f0_fixtures as fx
from tests.parity._f0_canonical import assert_matches_oracle

_COMPOSITE_FIXTURES: list[tuple[str, Callable[[], dict[str, pa.Table]]]] = [
    ("composite_person_deterministic", fx.run_composite_person_deterministic),
    ("composite_person_seeded_pool", fx.run_composite_person_seeded_pool),
]


def _ipc_bytes(table: pa.Table) -> bytes:
    """Arrow IPC stream bytes with a normalized chunk layout; carries schema and metadata."""
    table = table.combine_chunks()
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _fid_for(run: Callable[[], dict[str, pa.Table]]) -> str:
    matches = [f.fid for f in fx.FIXTURES if f.run is run]
    assert len(matches) == 1, f"{run.__name__}: expected exactly one registered fixture"
    return matches[0]


@pytest.mark.parametrize(
    ("label", "run"), _COMPOSITE_FIXTURES, ids=[c[0] for c in _COMPOSITE_FIXTURES]
)
def test_composite_person_repeats_byte_identically_in_process(
    label: str, run: Callable[[], dict[str, pa.Table]]
) -> None:
    first = run()
    second = run()

    assert list(first) == list(second), label
    assert first, f"{label}: fixture produced no tables"
    for name in first:
        assert first[name].num_rows > 0, f"{label}.{name}: empty table"
        assert first[name].schema.equals(second[name].schema, check_metadata=True), (label, name)
        assert _ipc_bytes(first[name]) == _ipc_bytes(second[name]), (
            f"{label}.{name}: two same-seed in-process runs differ"
        )

    # Tie the in-process result to the frozen oracle, using the second run.
    assert_matches_oracle(_fid_for(run), second)
