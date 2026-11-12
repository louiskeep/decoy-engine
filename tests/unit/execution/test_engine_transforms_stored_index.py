"""A config column that names only a stored pandas index field is refused by the declaration
gate, not by a late KeyError. Moved out of test_engine_transforms_exact.py, which is over the
test-file cap."""

from __future__ import annotations

import pytest

from decoy_engine.errors import PipelineValidationError
from tests.unit.execution._transform_testkit import single_table_config
from tests.unit.execution.test_engine_transforms_exact import _run, _stored_table


def test_stored_index_reference_without_transforms_is_refused(tmp_path):
    tbl = _stored_table()
    cfg = single_table_config(tmp_path, tbl, columns=[{"name": "ix", "strategy": "redact"}])
    with pytest.raises(PipelineValidationError) as exc:
        _run(cfg, tbl)
    assert exc.value.code == "source_file.declared_column_missing"
    assert "column ix" in str(exc.value)
