"""S4-FIXED-WIDTH: schema + end-to-end cells for the fixed-width `FileSource`
variant (engine-finish-open-ended program).

Mirrors `test_v2_cloud_sources.py`'s two-class shape: schema
acceptance/rejection against `PipelineConfig` / `FixedWidthLayout`
directly, then an end-to-end cell that parses a real fixed-width file
through `profile_source` / `read_fixed_width`.

Fail-closed cells assert BOTH that the error fires AND that it never
embeds the offending cell value (source files may carry PII; see
`errors.FixedWidthParseError`).
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from decoy_engine.config import PipelineConfig
from decoy_engine.config._fixed_width import FixedWidthLayout
from decoy_engine.errors import FixedWidthParseError
from decoy_engine.profile._fixed_width_reader import read_fixed_width


def _base_config() -> dict[str, Any]:
    """A minimum PipelineConfig the schema accepts; tests mutate `sources`."""
    return {
        "version": 1,
        "global_settings": {"seed": 0},
        "sources": {},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": "name",
                        "strategy": "faker",
                        "provider": "person_name",
                        "namespace": "t_name",
                        "deterministic": True,
                    },
                ],
            },
        ],
        "targets": {
            "t": {"type": "file", "format": "csv", "path": "/tmp/out.csv"},
        },
        "relationships": [],
        "namespaces": {"t_name": {"declared_by": ["t.name"]}},
    }


def _simple_layout() -> dict[str, Any]:
    """name: 0-8 str, age: 8-11 int, score: 11-16 float."""
    return {
        "columns": [
            {"name": "name", "start": 0, "width": 8, "type": "str"},
            {"name": "age", "start": 8, "width": 3, "type": "int"},
            {"name": "score", "start": 11, "width": 5, "type": "float"},
        ]
    }


# ---------------------------------------------------------------------
# Schema acceptance / rejection (extra=forbid + cross-field validators)
# ---------------------------------------------------------------------


class TestFixedWidthSourceSchema:
    def test_source_descriptor_accepts_fixed_width_variant(self) -> None:
        """FileSource with format='fixed_width' + a valid layout is accepted."""
        cfg = _base_config()
        cfg["sources"] = {
            "t": {
                "type": "file",
                "format": "fixed_width",
                "path": "/tmp/in.txt",
                "layout": _simple_layout(),
            },
        }
        validated = PipelineConfig.model_validate(cfg)
        assert validated.sources["t"].format == "fixed_width"

    def test_fixed_width_requires_layout(self) -> None:
        """format='fixed_width' without a `layout` fails loud."""
        cfg = _base_config()
        cfg["sources"] = {
            "t": {"type": "file", "format": "fixed_width", "path": "/tmp/in.txt"},
        }
        with pytest.raises(ValidationError, match="requires a `layout`"):
            PipelineConfig.model_validate(cfg)

    def test_csv_forbids_layout(self) -> None:
        """A non-fixed_width format carrying a `layout` fails loud (the field
        is only meaningful for fixed_width; silently ignoring it would be
        dishonest)."""
        cfg = _base_config()
        cfg["sources"] = {
            "t": {
                "type": "file",
                "format": "csv",
                "path": "/tmp/in.csv",
                "layout": _simple_layout(),
            },
        }
        with pytest.raises(ValidationError, match="only valid when format"):
            PipelineConfig.model_validate(cfg)

    def test_layout_rejects_empty_columns(self) -> None:
        with pytest.raises(ValidationError):
            FixedWidthLayout.model_validate({"columns": []})

    def test_layout_rejects_overlapping_columns(self) -> None:
        bad = {
            "columns": [
                {"name": "a", "start": 0, "width": 5, "type": "str"},
                {"name": "b", "start": 3, "width": 5, "type": "str"},
            ]
        }
        with pytest.raises(ValidationError, match="overlaps"):
            FixedWidthLayout.model_validate(bad)

    def test_layout_rejects_out_of_order_columns(self) -> None:
        bad = {
            "columns": [
                {"name": "a", "start": 5, "width": 5, "type": "str"},
                {"name": "b", "start": 0, "width": 5, "type": "str"},
            ]
        }
        with pytest.raises(ValidationError, match="out of order"):
            FixedWidthLayout.model_validate(bad)

    def test_layout_allows_gaps_between_columns(self) -> None:
        """Non-overlapping gaps (unused byte ranges) are fine."""
        ok = {
            "columns": [
                {"name": "a", "start": 0, "width": 5, "type": "str"},
                {"name": "b", "start": 10, "width": 5, "type": "str"},
            ]
        }
        layout = FixedWidthLayout.model_validate(ok)
        assert layout.record_width == 15

    def test_layout_rejects_negative_start(self) -> None:
        bad = {"columns": [{"name": "a", "start": -1, "width": 5, "type": "str"}]}
        with pytest.raises(ValidationError):
            FixedWidthLayout.model_validate(bad)

    def test_layout_rejects_zero_width(self) -> None:
        bad = {"columns": [{"name": "a", "start": 0, "width": 0, "type": "str"}]}
        with pytest.raises(ValidationError):
            FixedWidthLayout.model_validate(bad)

    def test_layout_rejects_unknown_type(self) -> None:
        bad = {"columns": [{"name": "a", "start": 0, "width": 5, "type": "date"}]}
        with pytest.raises(ValidationError):
            FixedWidthLayout.model_validate(bad)

    def test_layout_rejects_missing_field(self) -> None:
        bad = {"columns": [{"name": "a", "start": 0, "type": "str"}]}  # missing width
        with pytest.raises(ValidationError):
            FixedWidthLayout.model_validate(bad)

    def test_layout_rejects_duplicate_column_names(self) -> None:
        bad = {
            "columns": [
                {"name": "a", "start": 0, "width": 5, "type": "str"},
                {"name": "a", "start": 5, "width": 5, "type": "str"},
            ]
        }
        with pytest.raises(ValidationError, match="duplicate"):
            FixedWidthLayout.model_validate(bad)

    def test_layout_rejects_multichar_pad(self) -> None:
        bad = {"columns": [{"name": "a", "start": 0, "width": 5, "type": "str", "pad": "ab"}]}
        with pytest.raises(ValidationError):
            FixedWidthLayout.model_validate(bad)

    def test_layout_rejects_extra_field(self) -> None:
        bad = {
            "columns": [
                {
                    "name": "a",
                    "start": 0,
                    "width": 5,
                    "type": "str",
                    "unknown_field": 1,
                }
            ]
        }
        with pytest.raises(ValidationError):
            FixedWidthLayout.model_validate(bad)


# ---------------------------------------------------------------------
# End-to-end: read_fixed_width + profile_source
# ---------------------------------------------------------------------


class TestFixedWidthSourceEndToEnd:
    def test_read_fixed_width_parses_columns_and_types(self, tmp_path: Path) -> None:
        layout = FixedWidthLayout.model_validate(_simple_layout())
        data = tmp_path / "people.txt"
        # name(8, left/space) age(3, right-justified) score(5, float)
        data.write_text("alice    3012.50\nbob      2503.00\n", encoding="utf-8")

        df = read_fixed_width(str(data), layout)

        assert list(df.columns) == ["name", "age", "score"]
        assert df["name"].tolist() == ["alice", "bob"]
        assert df["age"].tolist() == [30, 25]
        assert df["score"].tolist() == [12.50, 3.00]

    def test_read_fixed_width_right_align_strips_leading_pad(self, tmp_path: Path) -> None:
        layout = FixedWidthLayout.model_validate(
            {
                "columns": [
                    {
                        "name": "id",
                        "start": 0,
                        "width": 6,
                        "type": "str",
                        "pad": "0",
                        "align": "right",
                    },
                ]
            }
        )
        data = tmp_path / "ids.txt"
        data.write_text("000042\n001337\n", encoding="utf-8")

        df = read_fixed_width(str(data), layout)

        assert df["id"].tolist() == ["42", "1337"]

    def test_read_fixed_width_skips_blank_lines(self, tmp_path: Path) -> None:
        layout = FixedWidthLayout.model_validate(
            {"columns": [{"name": "a", "start": 0, "width": 3, "type": "str"}]}
        )
        data = tmp_path / "with_blanks.txt"
        data.write_text("abc\n\ndef\n", encoding="utf-8")

        df = read_fixed_width(str(data), layout)

        assert df["a"].tolist() == ["abc", "def"]

    def test_read_fixed_width_accepts_plain_dict_layout(self, tmp_path: Path) -> None:
        """`read_fixed_width` re-validates a plain dict via `FixedWidthLayout`
        (the shape a validated PipelineConfig dump produces)."""
        data = tmp_path / "people.txt"
        data.write_text("alice    3012.50\n", encoding="utf-8")

        df = read_fixed_width(str(data), _simple_layout())

        assert df["name"].tolist() == ["alice"]

    def test_read_fixed_width_row_width_mismatch_raises(self, tmp_path: Path) -> None:
        layout = FixedWidthLayout.model_validate(_simple_layout())
        data = tmp_path / "short.txt"
        data.write_text("alice    30\n", encoding="utf-8")  # missing the score column

        with pytest.raises(FixedWidthParseError, match="row-width mismatch"):
            read_fixed_width(str(data), layout)

    def test_read_fixed_width_zero_padded_numeric_parses(self, tmp_path: Path) -> None:
        """A genuine zero-padded numeric (a common fixed-width convention)
        parses to its numeric value, not a `FixedWidthParseError` -- the
        pad-stripped `""` is retried against the raw slice."""
        layout = FixedWidthLayout.model_validate(
            {
                "columns": [
                    {
                        "name": "id",
                        "start": 0,
                        "width": 5,
                        "type": "int",
                        "pad": "0",
                        "align": "right",
                    },
                ]
            }
        )
        data = tmp_path / "zero_padded.txt"
        data.write_text("00000\n00042\n", encoding="utf-8")

        df = read_fixed_width(str(data), layout)

        assert df["id"].tolist() == [0, 42]

    def test_read_fixed_width_all_space_numeric_field_raises_honestly(self, tmp_path: Path) -> None:
        """A numeric field that is genuinely blank in the source data (all
        pad character, no digits) must still raise -- never silently
        coerced to `0`. Only an actual zero-padded numeric parses."""
        layout = FixedWidthLayout.model_validate(
            {"columns": [{"name": "id", "start": 0, "width": 4, "type": "int"}]}
        )
        data = tmp_path / "blank_numeric.txt"
        data.write_text("    \n", encoding="utf-8")

        with pytest.raises(FixedWidthParseError, match="cannot cast"):
            read_fixed_width(str(data), layout)

    def test_read_fixed_width_bad_cast_raises_without_leaking_value(self, tmp_path: Path) -> None:
        """The bad-cast path must never leak the raw cell value -- not in
        the exception's message, not via a chained `__cause__`, and not
        in a fully rendered traceback (the surfaces `logging.exception`/
        `exc_info=True` and an uncaught-exception printout actually use).

        The CAST column here (`code`, an `int` field) wholly contains the
        secret token, so the token is the exact string handed to `int()`
        and actually appears in the vector under test. A layout that only
        slices *part* of a would-be secret into the cast column (or casts
        a `str` column, which never calls `int()`/`float()`) would let this
        assertion pass trivially without exercising the leak at all.
        """
        secret_token = "SECRET-9f3a1c2bXYZ"
        layout = FixedWidthLayout.model_validate(
            {"columns": [{"name": "code", "start": 0, "width": len(secret_token), "type": "int"}]}
        )
        data = tmp_path / "bad_code.txt"
        data.write_text(f"{secret_token}\n", encoding="utf-8")

        with pytest.raises(FixedWidthParseError, match="cannot cast") as excinfo:
            read_fixed_width(str(data), layout)

        exc = excinfo.value
        rendered_traceback = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

        assert "code" in str(exc)
        # PII safety: the raw offending value must never appear anywhere --
        # not the message, not the chained cause, not a rendered traceback.
        assert secret_token not in str(exc)
        assert secret_token not in str(exc.__cause__)
        assert secret_token not in rendered_traceback

    def test_profile_source_end_to_end_fixed_width(self, tmp_path: Path) -> None:
        from decoy_engine.profile import profile_source

        data = tmp_path / "people.txt"
        data.write_text("alice    3012.50\nbob      2503.00\ncarol    4599.90\n", encoding="utf-8")

        cfg = _base_config()
        cfg["sources"] = {
            "t": {
                "type": "file",
                "format": "fixed_width",
                "path": str(data),
                "layout": _simple_layout(),
            },
        }

        profile = profile_source(cfg, seed=0)

        assert len(profile.tables) == 1
        assert profile.tables[0].name == "t"
        column_names = {c.name for c in profile.tables[0].columns}
        assert column_names == {"name", "age", "score"}
