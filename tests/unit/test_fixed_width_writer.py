"""`write_fixed_width`: the inverse of `read_fixed_width`.

Covers the round-trip invariant (anything the writer accepts reads back
unchanged), every fail-closed condition over the supported domain, failure-safe
atomic publication, the `FileTarget` schema, PII-safe error messages, and the
public export. Plan: docs/plans/2026-10-22-fixed-width-output-writer.md.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

import decoy_engine
from decoy_engine import read_fixed_width, write_fixed_width
from decoy_engine.config import FixedWidthLayout
from decoy_engine.config._targets import FileTarget, GCSTarget, S3Target
from decoy_engine.errors import FixedWidthWriteError
from decoy_engine.profile import _fixed_width_writer
from decoy_engine.profile._fixed_width_writer import (
    write_fixed_width as _internal_write_fixed_width,
)

# --------------------------------------------------------------------------- #
# A1: round-trip property -- anything the writer accepts reads back unchanged. #
# --------------------------------------------------------------------------- #

# A safe string alphabet: no newline (would split the record) and no space
# (align=left pad=" " would strip a trailing space), so these values round-trip.
_SAFE_STR = st.text(alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E), max_size=10)


@st.composite
def _layout_and_frame(draw: st.DrawFn) -> tuple[FixedWidthLayout, pd.DataFrame]:
    n_cols = draw(st.integers(min_value=1, max_value=4))
    n_rows = draw(st.integers(min_value=0, max_value=5))
    columns: list[dict[str, object]] = []
    column_values: dict[str, list[object]] = {}
    start = draw(st.integers(min_value=0, max_value=2))  # optional leading gap
    for i in range(n_cols):
        name = f"c{i}"
        col_type = draw(st.sampled_from(["str", "int", "float"]))
        if col_type == "str":
            values: list[object] = draw(st.lists(_SAFE_STR, min_size=n_rows, max_size=n_rows))
            pad, align = " ", "left"
            needed = max((len(v) for v in values), default=0)
        elif col_type == "int":
            ints = draw(
                st.lists(
                    st.integers(min_value=-99999, max_value=99999),
                    min_size=n_rows,
                    max_size=n_rows,
                )
            )
            values = list(ints)
            pad = draw(st.sampled_from([" ", "0"]))
            align = "right"  # ints stay right-aligned so a pad-strip is unambiguous
            needed = max((len(str(v)) for v in ints), default=0)
        else:
            floats = draw(
                st.lists(
                    st.floats(allow_nan=False, allow_infinity=False, width=32),
                    min_size=n_rows,
                    max_size=n_rows,
                )
            )
            values = list(floats)
            pad, align = " ", "right"
            needed = max((len(repr(float(v))) for v in floats), default=0)
        width = draw(st.integers(min_value=max(1, needed), max_value=max(1, needed) + 3))
        columns.append(
            {
                "name": name,
                "start": start,
                "width": width,
                "type": col_type,
                "pad": pad,
                "align": align,
            }
        )
        column_values[name] = values
        start += width + draw(st.integers(min_value=0, max_value=2))  # optional gap
    layout = FixedWidthLayout(columns=columns)  # type: ignore[arg-type]
    frame = pd.DataFrame(column_values, columns=[c["name"] for c in columns])
    return layout, frame


# Hypothesis tests cannot take a function-scoped pytest fixture, so examples
# share one temp directory (each write overwrites the previous output).
_HYP_DIR = Path(tempfile.mkdtemp(prefix="fw-hyp-"))


@settings(max_examples=250)
@given(_layout_and_frame())
def test_round_trip_anything_accepted_reads_back_unchanged(
    case: tuple[FixedWidthLayout, pd.DataFrame],
) -> None:
    layout, frame = case
    path = _HYP_DIR / "out.txt"
    try:
        write_fixed_width(frame, layout, str(path))
    except FixedWidthWriteError:
        assume(False)  # a legitimate fail-closed example; not a round-trip case
    read_back = read_fixed_width(str(path), layout)
    for column in layout.columns:
        assert read_back[column.name].tolist() == frame[column.name].tolist()


# --------------------------------------------------------------------------- #
# Explicit round-trip cases (gaps, zero-pad, negatives, right-align, LF).      #
# --------------------------------------------------------------------------- #


def test_gaps_zero_padded_and_negative_ints_round_trip(tmp_path: Path) -> None:
    layout = FixedWidthLayout(
        columns=[
            {"name": "code", "start": 2, "width": 4, "type": "str"},  # leading gap of 2
            {"name": "qty", "start": 8, "width": 5, "type": "int", "pad": "0", "align": "right"},
            {"name": "bal", "start": 13, "width": 6, "type": "int", "pad": " ", "align": "right"},
        ]
    )
    frame = pd.DataFrame({"code": ["AB", "CD"], "qty": [0, 42], "bal": [-5, 1000]})

    out = write_fixed_width(frame, layout, str(tmp_path / "o.txt"))
    raw = Path(out).read_bytes()

    assert b"\r" not in raw and raw.endswith(b"\n")  # LF terminators only
    read_back = read_fixed_width(out, layout)
    assert read_back["code"].tolist() == ["AB", "CD"]
    assert read_back["qty"].tolist() == [0, 42]
    assert read_back["bal"].tolist() == [-5, 1000]


def test_empty_string_and_float_round_trip(tmp_path: Path) -> None:
    layout = FixedWidthLayout(
        columns=[
            {"name": "s", "start": 0, "width": 4, "type": "str"},
            {"name": "f", "start": 4, "width": 8, "type": "float", "align": "right"},
        ]
    )
    frame = pd.DataFrame({"s": ["", "hi"], "f": [1.5, -0.25]})
    out = write_fixed_width(frame, layout, str(tmp_path / "o.txt"))
    read_back = read_fixed_width(out, layout)
    assert read_back["s"].tolist() == ["", "hi"]
    assert read_back["f"].tolist() == [1.5, -0.25]


def test_zero_rows_writes_empty_file(tmp_path: Path) -> None:
    layout = FixedWidthLayout(columns=[{"name": "a", "start": 0, "width": 3, "type": "str"}])
    frame = pd.DataFrame({"a": pd.Series([], dtype="object")})
    out = write_fixed_width(frame, layout, str(tmp_path / "o.txt"))
    assert Path(out).read_bytes() == b""
    assert read_fixed_width(out, layout).shape[0] == 0


# --------------------------------------------------------------------------- #
# Fail-closed conditions.                                                      #
# --------------------------------------------------------------------------- #


def test_overflow_fails_closed(tmp_path: Path) -> None:
    layout = FixedWidthLayout(columns=[{"name": "a", "start": 0, "width": 3, "type": "str"}])
    frame = pd.DataFrame({"a": ["toolong"]})
    with pytest.raises(FixedWidthWriteError) as exc:
        write_fixed_width(frame, layout, str(tmp_path / "o.txt"))
    assert "overflow" in str(exc.value)


def test_trailing_pad_string_fails_closed(tmp_path: Path) -> None:
    # align=left pad=" " value "ab " would read back as "ab".
    layout = FixedWidthLayout(
        columns=[{"name": "a", "start": 0, "width": 5, "type": "str", "pad": " ", "align": "left"}]
    )
    frame = pd.DataFrame({"a": ["ab "]})
    with pytest.raises(FixedWidthWriteError):
        write_fixed_width(frame, layout, str(tmp_path / "o.txt"))


def test_leading_pad_string_right_align_fails_closed(tmp_path: Path) -> None:
    layout = FixedWidthLayout(
        columns=[{"name": "a", "start": 0, "width": 5, "type": "str", "pad": "x", "align": "right"}]
    )
    frame = pd.DataFrame({"a": ["xab"]})  # lstrip "x" -> "ab"
    with pytest.raises(FixedWidthWriteError):
        write_fixed_width(frame, layout, str(tmp_path / "o.txt"))


def test_int_left_align_zero_pad_fails_closed(tmp_path: Path) -> None:
    # value 50, align=left pad="0" -> "5000"... reader rstrips "0" -> 5.
    layout = FixedWidthLayout(
        columns=[{"name": "n", "start": 0, "width": 4, "type": "int", "pad": "0", "align": "left"}]
    )
    frame = pd.DataFrame({"n": [50]})
    with pytest.raises(FixedWidthWriteError):
        write_fixed_width(frame, layout, str(tmp_path / "o.txt"))


@pytest.mark.parametrize(
    ("col_type", "value"),
    [("str", None), ("float", float("nan")), ("int", None)],
)
def test_null_and_nan_fail_closed(tmp_path: Path, col_type: str, value: object) -> None:
    layout = FixedWidthLayout(
        columns=[{"name": "a", "start": 0, "width": 6, "type": col_type, "align": "right"}]
    )
    frame = pd.DataFrame({"a": [value]})
    with pytest.raises(FixedWidthWriteError):
        write_fixed_width(frame, layout, str(tmp_path / "o.txt"))


@pytest.mark.parametrize("bad", ["a\nb", "a\rb"])
def test_embedded_newline_fails_closed(tmp_path: Path, bad: str) -> None:
    layout = FixedWidthLayout(columns=[{"name": "a", "start": 0, "width": 6, "type": "str"}])
    frame = pd.DataFrame({"a": [bad]})
    with pytest.raises(FixedWidthWriteError):
        write_fixed_width(frame, layout, str(tmp_path / "o.txt"))


@pytest.mark.parametrize(
    ("col_type", "value"),
    [
        ("str", 5),  # non-str in str column
        ("int", True),  # bool in int column
        ("int", 1.5),  # float in int column
        ("float", 5),  # int in float column
    ],
)
def test_type_mismatch_fails_closed(tmp_path: Path, col_type: str, value: object) -> None:
    layout = FixedWidthLayout(
        columns=[{"name": "a", "start": 0, "width": 8, "type": col_type, "align": "right"}]
    )
    frame = pd.DataFrame({"a": [value]})
    with pytest.raises(FixedWidthWriteError):
        write_fixed_width(frame, layout, str(tmp_path / "o.txt"))


def test_missing_layout_column_fails_closed(tmp_path: Path) -> None:
    layout = FixedWidthLayout(
        columns=[
            {"name": "a", "start": 0, "width": 3, "type": "str"},
            {"name": "b", "start": 3, "width": 3, "type": "str"},
        ]
    )
    frame = pd.DataFrame({"a": ["xyz"]})  # no "b"
    with pytest.raises(FixedWidthWriteError) as exc:
        write_fixed_width(frame, layout, str(tmp_path / "o.txt"))
    assert "missing" in str(exc.value)


def test_extra_frame_column_fails_closed(tmp_path: Path) -> None:
    layout = FixedWidthLayout(columns=[{"name": "a", "start": 0, "width": 3, "type": "str"}])
    frame = pd.DataFrame({"a": ["xyz"], "b": ["extra"]})
    with pytest.raises(FixedWidthWriteError) as exc:
        write_fixed_width(frame, layout, str(tmp_path / "o.txt"))
    assert "does not name" in str(exc.value)


# --------------------------------------------------------------------------- #
# Failure-safe publication.                                                    #
# --------------------------------------------------------------------------- #


def test_failure_publishes_nothing_and_leaves_existing_file(tmp_path: Path) -> None:
    layout = FixedWidthLayout(columns=[{"name": "a", "start": 0, "width": 3, "type": "str"}])
    target = tmp_path / "o.txt"
    target.write_text("PRE-EXISTING\n", encoding="utf-8")
    # row 0 ok, row 1 overflows -> whole write fails closed.
    frame = pd.DataFrame({"a": ["ok", "toolong"]})
    with pytest.raises(FixedWidthWriteError):
        write_fixed_width(frame, layout, str(target))
    assert target.read_text(encoding="utf-8") == "PRE-EXISTING\n"
    # no leftover temp files in the directory.
    assert [p.name for p in tmp_path.iterdir()] == ["o.txt"]


def test_success_replaces_existing_file(tmp_path: Path) -> None:
    layout = FixedWidthLayout(columns=[{"name": "a", "start": 0, "width": 3, "type": "str"}])
    target = tmp_path / "o.txt"
    target.write_text("OLD\n", encoding="utf-8")
    write_fixed_width(pd.DataFrame({"a": ["new"]}), layout, str(target))
    assert read_fixed_width(str(target), layout)["a"].tolist() == ["new"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["o.txt"]


# --------------------------------------------------------------------------- #
# PII-safe error messages.                                                     #
# --------------------------------------------------------------------------- #


def test_error_message_never_carries_the_value_or_chains_a_cause(tmp_path: Path) -> None:
    secret = "SSN-123456789-TOOLONG"
    layout = FixedWidthLayout(columns=[{"name": "a", "start": 0, "width": 3, "type": "str"}])
    frame = pd.DataFrame({"a": [secret]})
    with pytest.raises(FixedWidthWriteError) as exc:
        write_fixed_width(frame, layout, str(tmp_path / "o.txt"))
    message = str(exc.value)
    assert secret not in message
    assert "123456789" not in message
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    # position + column name are present; the value is not.
    assert "row 0" in message and "'a'" in message


# --------------------------------------------------------------------------- #
# Input guards.                                                                #
# --------------------------------------------------------------------------- #


def test_non_dataframe_raises_type_error(tmp_path: Path) -> None:
    layout = FixedWidthLayout(columns=[{"name": "a", "start": 0, "width": 3, "type": "str"}])
    with pytest.raises(TypeError):
        write_fixed_width([{"a": "xyz"}], layout, str(tmp_path / "o.txt"))  # type: ignore[arg-type]


def test_nul_in_path_raises_value_error(tmp_path: Path) -> None:
    layout = FixedWidthLayout(columns=[{"name": "a", "start": 0, "width": 3, "type": "str"}])
    with pytest.raises(ValueError):
        write_fixed_width(pd.DataFrame({"a": ["xyz"]}), layout, "bad\x00path")


def test_layout_dict_is_revalidated(tmp_path: Path) -> None:
    # A plain dict layout works, mirroring the reader.
    out = write_fixed_width(
        pd.DataFrame({"a": ["xyz"]}),
        {"columns": [{"name": "a", "start": 0, "width": 3, "type": "str"}]},
        str(tmp_path / "o.txt"),
    )
    assert read_fixed_width(out, FixedWidthLayout(columns=[{"name": "a", "start": 0, "width": 3}]))[
        "a"
    ].tolist() == ["xyz"]


# --------------------------------------------------------------------------- #
# Schema (FileTarget) + public export.                                         #
# --------------------------------------------------------------------------- #


def test_file_target_fixed_width_requires_layout() -> None:
    with pytest.raises(ValidationError):
        FileTarget(type="file", format="fixed_width", path="o.txt")  # type: ignore[call-arg]
    ok = FileTarget(
        type="file",
        format="fixed_width",
        path="o.txt",
        layout=FixedWidthLayout(columns=[{"name": "a", "start": 0, "width": 3}]),
    )
    assert ok.format == "fixed_width"


def test_file_target_layout_forbidden_for_csv_parquet() -> None:
    with pytest.raises(ValidationError):
        FileTarget(
            type="file",
            format="csv",
            path="o.csv",
            layout=FixedWidthLayout(columns=[{"name": "a", "start": 0, "width": 3}]),
        )


def test_cloud_targets_still_reject_fixed_width() -> None:
    with pytest.raises(ValidationError):
        S3Target(type="s3", format="fixed_width", bucket="b", key="k")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        GCSTarget(type="gcs", format="fixed_width", bucket="b", object="o")  # type: ignore[arg-type]


def test_write_fixed_width_is_the_same_object_and_exported() -> None:
    assert write_fixed_width is _internal_write_fixed_width
    assert "write_fixed_width" in decoy_engine.__all__


# --------------------------------------------------------------------------- #
# Encode / path guards / I/O-failure cleanup.                                  #
# --------------------------------------------------------------------------- #


def test_unencodable_character_fails_closed(tmp_path: Path) -> None:
    # A lone surrogate is a valid Python str but cannot encode as UTF-8, and the
    # reader decodes UTF-8, so it cannot round-trip.
    layout = FixedWidthLayout(columns=[{"name": "a", "start": 0, "width": 4, "type": "str"}])
    frame = pd.DataFrame({"a": ["\ud800"]})
    with pytest.raises(FixedWidthWriteError) as exc:
        write_fixed_width(frame, layout, str(tmp_path / "o.txt"))
    assert "UTF-8" in str(exc.value)
    assert not (tmp_path / "o.txt").exists()


def test_bytes_path_raises_type_error(tmp_path: Path) -> None:
    layout = FixedWidthLayout(columns=[{"name": "a", "start": 0, "width": 3, "type": "str"}])
    with pytest.raises(TypeError):
        write_fixed_width(pd.DataFrame({"a": ["xyz"]}), layout, b"bytes-path")  # type: ignore[arg-type]


def test_io_failure_on_replace_propagates_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = FixedWidthLayout(columns=[{"name": "a", "start": 0, "width": 3, "type": "str"}])

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(_fixed_width_writer.os, "replace", _boom)
    target = tmp_path / "o.txt"
    with pytest.raises(OSError):
        write_fixed_width(pd.DataFrame({"a": ["xyz"]}), layout, str(target))
    # The canonical file was not created and no temp file leaked.
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_silent_unlink_ignores_missing_file(tmp_path: Path) -> None:
    # Best-effort cleanup never raises on an already-gone temp file.
    _fixed_width_writer._silent_unlink(str(tmp_path / "does-not-exist.tmp"))


# --------------------------------------------------------------------------- #
# Codex final-gate regressions: record-breaking pad, duplicate columns,       #
# directory-fsync close guard.                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pad", ["\n", "\r"])
def test_record_separator_pad_rejected_before_publication(tmp_path: Path, pad: str) -> None:
    """A pad char that is itself a record separator (LF/CR) passes the per-field
    check but makes the assembled file read back with the wrong row structure
    (silent row loss / parse error). Reject it fail-closed before any write."""
    layout = FixedWidthLayout(
        columns=[{"name": "a", "start": 0, "width": 1, "type": "str", "pad": pad}]
    )
    frame = pd.DataFrame({"a": [""]})
    path = tmp_path / "o.txt"
    with pytest.raises(FixedWidthWriteError) as exc:
        write_fixed_width(frame, layout, str(path))
    assert "line separator" in str(exc.value)
    assert not path.exists()  # nothing published
    # no leaked value or exception chain
    assert exc.value.__cause__ is None and exc.value.__context__ is None


def test_duplicate_frame_columns_rejected_as_coded_error(tmp_path: Path) -> None:
    """A frame with duplicate column labels used to raise an uncoded
    AttributeError (frame[name] is a DataFrame). Reject it as a coded,
    value-free FixedWidthWriteError before the temp file is created."""
    layout = FixedWidthLayout(columns=[{"name": "a", "start": 0, "width": 2, "type": "int"}])
    frame = pd.DataFrame([[1, 2]], columns=["a", "a"])
    path = tmp_path / "o.txt"
    with pytest.raises(FixedWidthWriteError) as exc:
        write_fixed_width(frame, layout, str(path))
    assert "duplicate" in str(exc.value)
    assert not path.exists()


def test_fsync_dir_survives_close_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The best-effort directory fsync must not raise out of a successful write:
    an os.close fault after os.replace would otherwise surface as a spurious
    failure on an already-published file."""
    import os

    real_close = os.close

    def raising_close(fd: int) -> None:
        real_close(fd)
        raise OSError("EIO on close")

    monkeypatch.setattr(os, "close", raising_close)
    # Must return cleanly despite the close fault.
    _fixed_width_writer._fsync_dir(str(tmp_path))


def test_negative_zero_sign_preserved_on_round_trip(tmp_path: Path) -> None:
    """-0.0 must either round-trip with its sign intact or fail closed; it must
    never be silently accepted as +0.0 (== treats them equal)."""
    layout = FixedWidthLayout(
        columns=[{"name": "a", "start": 0, "width": 6, "type": "float", "align": "right"}]
    )
    frame = pd.DataFrame({"a": [-0.0]})
    path = tmp_path / "o.txt"
    out = write_fixed_width(frame, layout, str(path))
    back = read_fixed_width(str(out), layout)
    got = float(back["a"].iloc[0])
    import math

    assert got == 0.0 and math.copysign(1.0, got) == -1.0  # sign preserved


def test_negative_zero_with_minus_pad_fails_closed(tmp_path: Path) -> None:
    """A '-' pad strips the value's own sign on read-back, turning -0.0 into
    +0.0. The exact round-trip check must now reject it rather than publish a
    sign-flipped value."""
    layout = FixedWidthLayout(
        columns=[
            {"name": "a", "start": 0, "width": 6, "type": "float", "align": "right", "pad": "-"}
        ]
    )
    frame = pd.DataFrame({"a": [-0.0]})
    path = tmp_path / "o.txt"
    with pytest.raises(FixedWidthWriteError):
        write_fixed_width(frame, layout, str(path))
    assert not path.exists()


def test_extreme_integer_fails_closed_with_coded_error(tmp_path: Path) -> None:
    """An integer beyond Python's int->str digit cap used to raise a raw
    ValueError; it must fail closed with a coded, value-free FixedWidthWriteError."""
    layout = FixedWidthLayout(columns=[{"name": "a", "start": 0, "width": 5000, "type": "int"}])
    # object dtype keeps the Python int (a bare list makes pandas try float and overflow).
    frame = pd.DataFrame(
        {"a": pd.Series([10**4500], dtype=object)}
    )  # 4501 digits, over the 4300 cap
    path = tmp_path / "o.txt"
    with pytest.raises(FixedWidthWriteError) as exc:
        write_fixed_width(frame, layout, str(path))
    assert "too large to render" in str(exc.value)
    # value-free: the giant integer must not appear in the message
    assert "1000" not in str(exc.value)
    assert not path.exists()
