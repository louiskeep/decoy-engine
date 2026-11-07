"""Every declared file format has a reader, a profile path, a writer and a run-result value.

The formats come from the config Literals themselves (`typing.get_args`), not a hand list, so a
format added to `FileSource.format` or `FileTarget.format` without the rest fails here.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import get_args

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config._sources import FileSource
from decoy_engine.config._targets import FileTarget
from decoy_engine.io import CALLER_WRITTEN_TARGETS, TARGET_WRITERS, write_avro, write_orc
from decoy_engine.profile import build_profile_source
from decoy_engine.run_result._model import OutputFormat

SOURCE_FORMATS = get_args(FileSource.model_fields["format"].annotation)
TARGET_FORMATS = get_args(FileTarget.model_fields["format"].annotation)
_TABLE = pa.table({"a": ["x", "y", "z"], "b": ["1", "2", "3"]})
_LAYOUT = {
    "columns": [{"name": "a", "start": 0, "width": 1}, {"name": "b", "start": 1, "width": 1}]
}


def _fixed_width(path: Path) -> dict[str, object]:
    path.write_text("x1\ny2\nz3\n", encoding="utf-8")
    return {"layout": _LAYOUT}


# One way to make a small real file per format; a format missing here fails the guard.
MAKERS: dict[str, Callable[[Path], dict[str, object]]] = {
    "csv": lambda p: (pd.DataFrame(_TABLE.to_pydict()).to_csv(p, index=False), {})[1],
    "parquet": lambda p: (pq.write_table(_TABLE, p), {})[1],
    "fixed_width": _fixed_width,
    "avro": lambda p: (write_avro(_TABLE, p, table="t"), {})[1],
    "orc": lambda p: (write_orc(_TABLE, p, table="t"), {})[1],
}


@pytest.mark.parametrize("fmt", SOURCE_FORMATS)
def test_every_source_format_has_a_profile_reader(tmp_path: Path, fmt: str) -> None:
    assert fmt in MAKERS, f"no fixture maker for source format {fmt!r}"
    path = tmp_path / f"f.{fmt}"
    extra = MAKERS[fmt](path)
    source = build_profile_source({"type": "file", "format": fmt, "path": str(path), **extra})
    assert source.schema().names == ["a", "b"]
    assert source.row_count().value == 3
    assert len(source.sample_frame(2)) == 2


@pytest.mark.parametrize("fmt", TARGET_FORMATS)
def test_every_target_format_has_a_writer(fmt: str) -> None:
    assert (fmt in TARGET_WRITERS) != (fmt in CALLER_WRITTEN_TARGETS), fmt
    if fmt in TARGET_WRITERS:
        assert callable(TARGET_WRITERS[fmt])


def test_writer_tables_name_only_declared_formats() -> None:
    assert set(TARGET_WRITERS) | set(CALLER_WRITTEN_TARGETS) == set(TARGET_FORMATS)


def test_run_result_output_format_agrees_with_file_target() -> None:
    assert set(get_args(OutputFormat)) == set(TARGET_FORMATS)
