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
from decoy_engine.errors import ConfigError, FixedWidthParseError
from decoy_engine.profile._fixed_width_reader import read_fixed_width


class _ReadlineCountingFile:
    """Wraps a real file handle, counting `readline()` calls, so a test
    can assert exactly how many lines a reader actually pulled from disk
    instead of inferring it indirectly from validation side effects."""

    def __init__(self, fh: Any) -> None:
        self._fh = fh
        self.readline_calls = 0

    def readline(self, *args: Any, **kwargs: Any) -> str:
        self.readline_calls += 1
        return self._fh.readline(*args, **kwargs)  # type: ignore[no-any-return]

    def __iter__(self) -> _ReadlineCountingFile:
        return self

    def __next__(self) -> str:
        line = self.readline()
        if line == "":
            raise StopIteration
        return line

    def __enter__(self) -> _ReadlineCountingFile:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._fh.__exit__(*exc_info)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fh, name)


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
        the exception's message, not via `__cause__` or `__context__`
        (chained OR not), and not in a fully rendered traceback (the
        surfaces `logging.exception`/`exc_info=True` and an uncaught-
        exception printout actually use).

        `__context__ is None` is the load-bearing assertion here: Python
        auto-populates `__context__` with the currently-handled exception
        for ANY `raise` executed inside that handler, `from None` or not
        (see `_fixed_width_reader._cast_value`'s comment) -- checking only
        `__cause__` (which `from None` reliably sets to `None`) misses
        this leak entirely, since `str(None)` never contains the token
        regardless of what `__context__` holds.

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
        assert exc.__cause__ is None
        assert exc.__context__ is None
        # PII safety: the raw offending value must never appear anywhere --
        # not the message, not a chain, not `repr`, not a rendered traceback.
        assert secret_token not in str(exc)
        assert secret_token not in repr(exc)
        assert secret_token not in str(exc.__cause__)
        assert secret_token not in str(exc.__context__)
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


# ---------------------------------------------------------------------
# A5a gate remediation: malformed layout, undecodable bytes, max_records
# argument validation and read bound, byte-vs-character characterization.
# ---------------------------------------------------------------------


class TestFixedWidthErrorWrapping:
    def test_malformed_layout_dict_raises_config_error_without_chaining(
        self, tmp_path: Path
    ) -> None:
        """A dict that fails `FixedWidthLayout.model_validate` must surface
        as the engine's own `ConfigError`, not a raw pydantic
        `ValidationError` -- and, per the same outside-the-handler rule as
        the cast path, with no `__cause__`/`__context__` chain."""
        data = tmp_path / "irrelevant.txt"
        data.write_text("abc\n", encoding="utf-8")
        bad_layout = {"columns": [{"name": "a", "start": 0, "type": "str"}]}  # missing width

        with pytest.raises(ConfigError) as excinfo:
            read_fixed_width(str(data), bad_layout)

        exc = excinfo.value
        assert not isinstance(exc, ValidationError)
        assert exc.__cause__ is None
        assert exc.__context__ is None

    def test_non_utf8_file_raises_fixed_width_parse_error_without_leaking_bytes(
        self, tmp_path: Path
    ) -> None:
        """`UnicodeDecodeError.object` holds the raw undecodable bytes,
        which may themselves carry PII -- the wrapper must never expose
        them, and must not chain the original `UnicodeDecodeError` in via
        `__cause__`/`__context__`."""
        layout = FixedWidthLayout.model_validate(
            {"columns": [{"name": "a", "start": 0, "width": 3, "type": "str"}]}
        )
        data = tmp_path / "not_utf8.txt"
        secret_bytes = b"\xff\xfe\x00SECRET"
        data.write_bytes(secret_bytes + b"\n")

        with pytest.raises(FixedWidthParseError) as excinfo:
            read_fixed_width(str(data), layout)

        exc = excinfo.value
        rendered_traceback = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        assert exc.__cause__ is None
        assert exc.__context__ is None
        assert "SECRET" not in str(exc)
        assert "SECRET" not in rendered_traceback


class TestMaxRecordsValidation:
    def test_rejects_bool_max_records(self, tmp_path: Path) -> None:
        """`bool` is a subclass of `int`; treating `True`/`False` as 1/0
        records is almost certainly a caller mistake, so it is rejected
        rather than silently accepted."""
        layout = FixedWidthLayout.model_validate(_simple_layout())
        data = tmp_path / "people.txt"
        data.write_text("alice    3012.50\n", encoding="utf-8")

        with pytest.raises(TypeError, match="max_records"):
            read_fixed_width(str(data), layout, max_records=True)

    def test_rejects_non_int_max_records(self, tmp_path: Path) -> None:
        layout = FixedWidthLayout.model_validate(_simple_layout())
        data = tmp_path / "people.txt"
        data.write_text("alice    3012.50\n", encoding="utf-8")

        with pytest.raises(TypeError, match="max_records"):
            read_fixed_width(str(data), layout, max_records="1")  # type: ignore[arg-type]

    def test_rejects_negative_max_records(self, tmp_path: Path) -> None:
        layout = FixedWidthLayout.model_validate(_simple_layout())
        data = tmp_path / "people.txt"
        data.write_text("alice    3012.50\n", encoding="utf-8")

        with pytest.raises(ValueError, match="max_records"):
            read_fixed_width(str(data), layout, max_records=-1)

    def test_max_records_zero_reads_zero_records(self, tmp_path: Path) -> None:
        layout = FixedWidthLayout.model_validate(_simple_layout())
        data = tmp_path / "people.txt"
        data.write_text("alice    3012.50\nbob      2503.00\n", encoding="utf-8")

        df = read_fixed_width(str(data), layout, max_records=0)

        assert list(df.columns) == ["name", "age", "score"]
        assert len(df) == 0

    def test_max_records_cap_never_reads_more_lines_than_the_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for the SC7a bounded-read cap reading one line past
        what it needed, instrumented directly on the file handle's
        `readline()` call count.

        Two easier-looking proxies for this turned out to be unreliable
        and were rejected: a too-short-but-valid line 2 is silently
        discarded by the row-width check either way, so it cannot tell
        "line 2 was never fetched" apart from "line 2 was fetched, then
        discarded before validation" -- both look identical from the
        outside. A non-UTF-8 line 2 does distinguish old from new code,
        but for the wrong reason: `TextIOWrapper` reads and decodes in
        internal chunks, so a short file can trip `UnicodeDecodeError` on
        the very first `readline()` call regardless of the cap logic --
        an unrelated buffering artifact, not the over-read this test is
        for. Counting actual `readline()` calls on the real file handle
        is the only thing that isolates the cap's own behavior from both.
        """
        layout = FixedWidthLayout.model_validate(
            {"columns": [{"name": "a", "start": 0, "width": 3, "type": "str"}]}
        )
        data = tmp_path / "three_lines.txt"
        data.write_text("abc\ndef\nghi\n", encoding="utf-8")

        real_open = open
        opened: list[_ReadlineCountingFile] = []

        def fake_open(*args: Any, **kwargs: Any) -> _ReadlineCountingFile:
            wrapped = _ReadlineCountingFile(real_open(*args, **kwargs))
            opened.append(wrapped)
            return wrapped

        monkeypatch.setattr(
            "decoy_engine.profile._fixed_width_reader.open", fake_open, raising=False
        )

        df = read_fixed_width(str(data), layout, max_records=1)

        assert df["a"].tolist() == ["abc"]
        assert len(opened) == 1
        assert opened[0].readline_calls == 1, (
            f"expected exactly 1 readline() call for max_records=1, got "
            f"{opened[0].readline_calls} -- the cap read past what it needed"
        )


class TestFixedWidthByteVsCharacterCharacterization:
    def test_read_fixed_width_slices_by_character_not_byte_offset(self, tmp_path: Path) -> None:
        """CHARACTERIZATION, not a correctness claim: `FixedWidthLayout`
        documents `start`/`width` as BYTE offsets (config._fixed_width
        module docstring), but this reader opens the file as decoded
        UTF-8 TEXT and slices by Python string index (character
        position), not by encoded byte position. The two coincide for
        ASCII-only data and diverge once a multibyte character appears.

        This pins TODAY's character-based behavior so a future switch to
        true byte slicing -- tracked in
        docs/plans/2026-07-22-input-format-parity.md:197 ("Fixed-width
        core") -- shows up here as a reviewed, intentional change instead
        of a silent one. Do not "fix" this test to assert byte slicing;
        that is a separate, out-of-scope change.
        """
        layout = FixedWidthLayout.model_validate(
            {
                "columns": [
                    {"name": "a", "start": 0, "width": 1},
                    {"name": "b", "start": 1, "width": 3},
                ]
            }
        )
        data = tmp_path / "multibyte.txt"
        # "é" is one Python character but two UTF-8 bytes (0xC3 0xA9); a
        # true byte-offset reader would not draw the column boundary
        # after that single character the way this character-index
        # reader does.
        data.write_text("éxyz\n", encoding="utf-8")

        df = read_fixed_width(str(data), layout)

        assert df["a"].tolist() == ["é"]
        assert df["b"].tolist() == ["xyz"]


def test_read_fixed_width_decode_error_names_the_exact_line(tmp_path) -> None:
    """The bad byte sits well past the first 8 KB; the error names its line."""
    path = tmp_path / "bad.dat"
    path.write_bytes(b"abc\n" * 5000 + b"a\xffc\n")
    layout = {"columns": [{"name": "v", "start": 0, "width": 3}]}
    with pytest.raises(FixedWidthParseError, match=r": line 5001: not valid UTF-8"):
        read_fixed_width(path, layout)


def test_read_fixed_width_capped_read_never_examines_bytes_past_the_cap(tmp_path) -> None:
    """A valid capped record followed immediately by undecodable bytes: the cap
    is honored and the tail is never decoded."""
    path = tmp_path / "tail.dat"
    path.write_bytes(b"abc\n" + b"\xff\xfe\xfd\n" * 3000)
    layout = {"columns": [{"name": "v", "start": 0, "width": 3}]}
    df = read_fixed_width(path, layout, max_records=1)
    assert df["v"].tolist() == ["abc"]


def test_read_fixed_width_crlf_lines_read_like_lf(tmp_path) -> None:
    layout = {"columns": [{"name": "v", "start": 0, "width": 3}]}
    lf = tmp_path / "lf.dat"
    crlf = tmp_path / "crlf.dat"
    lf.write_bytes(b"abc\ndef\n")
    crlf.write_bytes(b"abc\r\ndef\r\n")
    assert read_fixed_width(crlf, layout).equals(read_fixed_width(lf, layout))


@pytest.mark.parametrize(
    ("bad_path", "exc_type"),
    [(b"/tmp/x.dat", TypeError), (123, TypeError), ("/tmp/a\x00b.dat", ValueError)],
)
def test_read_fixed_width_rejects_invalid_paths(bad_path: Any, exc_type: type) -> None:
    layout = {"columns": [{"name": "v", "start": 0, "width": 3}]}
    with pytest.raises(exc_type):
        read_fixed_width(bad_path, layout)
