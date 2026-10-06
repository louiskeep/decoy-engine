"""One string-only source-type gate serves categorical, bucket_perturb and date_shift (R1 E4).

Expected values are the exact reasons the three per-strategy gates produced on engine
main 01c560da.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine.execution.native._real_type_admission import string_source_type_rejection

_STRATEGIES = ("categorical", "bucket_perturb", "date_shift")
_TYPES = [
    (pa.string(), None),
    (pa.large_string(), "large_string"),
    (pa.int64(), "int64"),
    (pa.null(), "null"),
]


@pytest.mark.parametrize("strategy", _STRATEGIES)
@pytest.mark.parametrize(("typ", "shown"), _TYPES)
def test_string_gate_reason_is_byte_identical(
    strategy: str, typ: pa.DataType, shown: str | None
) -> None:
    schema = pa.schema([pa.field("c", typ)])
    expected = None if shown is None else f"{strategy}_source_type_not_string:c:{shown}"
    assert string_source_type_rejection(strategy, "c", schema) == expected
