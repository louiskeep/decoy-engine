"""A table whose pandas metadata names an existing data column as the index has no such column
left once the index is stored, so the declaration gate refuses it. Moved out of
test_unified_slice_admission.py, which is over the test-file cap."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.errors import PipelineValidationError
from tests.physical.test_unified_slice_admission import _build, _profile_and_plan


def test_cheap_admission_declines_physical_named_index(tmp_path: Path) -> None:
    """A resident table whose metadata designates an EXISTING data column as
    the index (`index_columns: ["c"]`, no extra physical field): `source.
    column_names` still equals the configured surface exactly (nothing
    "extra" to catch via the undeclared-column check), but `to_pandas()`
    pulls `c` out of `frame.columns` entirely, leaving zero columns."""
    base = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    meta = {
        "index_columns": ["c"],
        "column_indexes": [],
        "columns": [
            {
                "name": "c",
                "field_name": "c",
                "pandas_type": "unicode",
                "numpy_type": "object",
                "metadata": None,
            }
        ],
        "attributes": {},
        "creator": {"library": "pyarrow", "version": "24.0.0"},
        "pandas_version": "2.3.3",
    }
    schema = base.schema.with_metadata({b"pandas": json.dumps(meta).encode()})
    source = base.cast(schema)
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    with pytest.raises(PipelineValidationError) as caught:  # the stored index is not a column
        _profile_and_plan(config, source)
    assert caught.value.code == "source_file.declared_column_missing"
    assert "column c" in str(caught.value)
