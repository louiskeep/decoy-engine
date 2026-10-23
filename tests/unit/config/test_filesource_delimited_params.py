"""FileSource delimited-dialect params: has_header / column_names / delimiter.

CSV-PARAMS slice. The engine validates config with pydantic (no
ValidationResult path for config), so each reserved `source_file.*` semantic
is enforced here as a `ValidationError`. These assert the contract the CLI's
`PipelineConfig.model_validate` path and the profiler both read.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from decoy_engine.config._sources import FileSource


def _csv(**extra: object) -> dict[str, object]:
    return {"type": "file", "format": "csv", "path": "data.csv", **extra}


class TestDelimitedParamsAccepted:
    def test_defaults_are_todays_behaviour(self) -> None:
        src = FileSource.model_validate(_csv())
        assert src.has_header is True
        assert src.column_names is None
        assert src.delimiter == ","

    def test_headerless_with_names_validates(self) -> None:
        src = FileSource.model_validate(
            _csv(has_header=False, column_names=["id", "name", "email"])
        )
        assert src.has_header is False
        assert src.column_names == ["id", "name", "email"]

    def test_tab_delimiter_validates(self) -> None:
        src = FileSource.model_validate(_csv(delimiter="\t"))
        assert src.delimiter == "\t"


class TestDelimitedParamsRejected:
    def test_headerless_without_names_is_error(self) -> None:
        # source_file.no_header_columns
        with pytest.raises(ValidationError, match="requires a non-empty `column_names`"):
            FileSource.model_validate(_csv(has_header=False))

    def test_names_with_header_is_error(self) -> None:
        # source_file.column_names_with_header
        with pytest.raises(ValidationError, match="forbidden when has_header=true"):
            FileSource.model_validate(_csv(column_names=["a", "b"]))

    def test_multichar_delimiter_is_error(self) -> None:
        # source_file.bad_delimiter
        with pytest.raises(ValidationError, match="must be exactly one character"):
            FileSource.model_validate(_csv(delimiter="||"))

    def test_empty_delimiter_is_error(self) -> None:
        with pytest.raises(ValidationError, match="must be exactly one character"):
            FileSource.model_validate(_csv(delimiter=""))

    def test_non_bool_has_header_is_error(self) -> None:
        # source_file.bad_has_header_type (pydantic type check). A list is not
        # coercible to bool (unlike the truthy strings pydantic accepts in lax
        # mode), so it exercises the type rejection.
        with pytest.raises(ValidationError):
            FileSource.model_validate(_csv(has_header=["not", "a", "bool"]))

    def test_delimited_param_on_parquet_is_error(self) -> None:
        # source_file.csv_param_on_non_csv
        with pytest.raises(ValidationError, match="only valid when format='csv'"):
            FileSource.model_validate(
                {"type": "file", "format": "parquet", "path": "data.parquet", "delimiter": "\t"}
            )

    def test_empty_column_names_is_error(self) -> None:
        # An empty list would read a zero-column frame and silently drop the file.
        with pytest.raises(ValidationError, match="requires a non-empty `column_names`"):
            FileSource.model_validate(_csv(has_header=False, column_names=[]))

    def test_duplicate_column_names_is_error(self) -> None:
        with pytest.raises(ValidationError, match="must be unique"):
            FileSource.model_validate(_csv(has_header=False, column_names=["x", "x"]))

    def test_blank_column_name_is_error(self) -> None:
        with pytest.raises(ValidationError, match="must be non-empty"):
            FileSource.model_validate(_csv(has_header=False, column_names=["id", "  "]))

    def test_column_names_on_fixed_width_is_error(self) -> None:
        with pytest.raises(ValidationError, match="only valid when format='csv'"):
            FileSource.model_validate(
                {
                    "type": "file",
                    "format": "fixed_width",
                    "path": "data.fwf",
                    "layout": {"columns": [{"name": "id", "start": 0, "width": 4}]},
                    "column_names": ["id"],
                }
            )
