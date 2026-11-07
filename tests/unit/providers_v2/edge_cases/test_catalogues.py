"""Catalogue contract for the edge-case providers: types, categories, safety rules, writers."""

from __future__ import annotations

import datetime as dt
import io
import struct
import sys
import unicodedata
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.providers_v2.edge_cases import EDGE_CASE_SPECS, EdgeCase, EdgeCaseSpec
from decoy_engine.providers_v2.edge_cases._spec import DATETIME_MAX, DATETIME_MIN, Kind
from decoy_engine.providers_v2.edge_cases._value_cases import DST_TRANSITIONS

SPECS = {spec.provider: spec for spec in EDGE_CASE_SPECS}
TEXT = ("edge_case_string", "edge_case_unicode")

_CATEGORIES = {
    "edge_case_string": {
        "empty",
        "whitespace",
        "line_breaks",
        "quotes_and_delimiters",
        "null_like_tokens",
        "injection_text",
        "length_boundaries",
    },
    "edge_case_unicode": {
        "accented_latin",
        "normalization_variants",
        "combining_marks",
        "case_mapping",
        "bidi",
        "invisible",
        "emoji_and_astral",
        "scripts",
        "homoglyphs",
        "replacement",
    },
    "edge_case_integer": {
        "zero_and_unit",
        "signed_width_bounds",
        "unsigned_width_bounds",
        "float_precision",
        "decimal_digits",
    },
    "edge_case_float": {"zeros", "subnormal", "extremes", "precision", "rounding", "display"},
    "edge_case_date": {
        "epoch",
        "leap_years",
        "period_ends",
        "rollovers",
        "calendar_history",
        "library_bounds",
        "type_bounds",
        "dst_days",
    },
    "edge_case_datetime": {
        "epoch",
        "rollovers",
        "day_boundaries",
        "subsecond",
        "dst_transitions",
        "library_bounds",
    },
}

_TYPES = {
    "edge_case_string": str,
    "edge_case_unicode": str,
    "edge_case_integer": int,
    "edge_case_float": float,
    "edge_case_date": dt.date,
    "edge_case_datetime": dt.datetime,
}


def _in(provider: str, category: str) -> list:
    return [c.value for c in SPECS[provider].cases if c.category == category]


def test_six_providers_with_their_categories() -> None:
    assert set(SPECS) == set(_CATEGORIES)
    for name, spec in SPECS.items():
        assert {c.category for c in spec.cases} == _CATEGORIES[name] == set(spec.categories)


@pytest.mark.parametrize("name", sorted(SPECS))
def test_values_have_the_exact_declared_type_and_are_distinct(name: str) -> None:
    values = SPECS[name].values
    assert all(type(v) is _TYPES[name] for v in values)
    keys = [struct.pack("<d", v) if name == "edge_case_float" else v for v in values]
    assert len(set(keys)) == len(keys)


def test_text_kinds() -> None:
    assert all(v.isascii() for v in SPECS["edge_case_string"].values)
    assert not any(v.isascii() for v in SPECS["edge_case_unicode"].values)


def test_integer_bounds_are_the_width_limits() -> None:
    values = set(SPECS["edge_case_integer"].values)
    for bits in (8, 16, 32):
        low, high = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
        assert {low, high, low - 1, high + 1} <= values
    assert {-(2**63), 2**63 - 1, 2**32 - 1, 2**32, 2**53 + 1} <= values
    assert all(-(2**63) <= v < 2**63 for v in values)


def test_normalization_pairs_are_equivalent_but_different() -> None:
    norm = _in("edge_case_unicode", "normalization_variants")
    for a, b, form in (
        ("caf\u00e9", "cafe\u0301", "NFC"),
        ("\u00c5", "\u212b", "NFC"),
        ("\u00c5", "A\u030a", "NFC"),
        ("\uac00", "\u1100\u1161", "NFC"),
    ):
        assert a in norm and b in norm and a != b
        assert unicodedata.normalize(form, a) == unicodedata.normalize(form, b)
    assert unicodedata.normalize("NFKC", "\ufb01le") == "file"


def test_bidi_and_astral_cases_contain_what_they_claim() -> None:
    strong = {"R", "AL", "RLO", "RLI", "AN"}
    for v in _in("edge_case_unicode", "bidi"):
        assert {unicodedata.bidirectional(ch) for ch in v} & strong
    for v in _in("edge_case_unicode", "emoji_and_astral"):
        assert any(ord(ch) > 0xFFFF for ch in v)


def test_subnormal_category() -> None:
    values = _in("edge_case_float", "subnormal")
    assert all(0 < abs(v) <= sys.float_info.min for v in values)
    assert sum(abs(v) < sys.float_info.min for v in values) == 3
    zeros = _in("edge_case_float", "zeros")
    assert [struct.pack("<d", z) for z in zeros] == [
        struct.pack("<d", 0.0),
        struct.pack("<d", -0.0),
    ]


def test_dst_instants_are_real_transitions() -> None:
    catalogue = set(_in("edge_case_datetime", "dst_transitions"))
    us = dt.timedelta(microseconds=1)
    for zone, instant in DST_TRANSITIONS:
        tz = ZoneInfo(zone)
        assert (instant - us).astimezone(tz).utcoffset() != instant.astimezone(tz).utcoffset()
        assert {instant, instant - us} <= catalogue


def test_datetimes_are_utc_inside_the_nanosecond_range() -> None:
    for v in SPECS["edge_case_datetime"].values:
        assert v.tzinfo is dt.timezone.utc and DATETIME_MIN <= v <= DATETIME_MAX
    assert pd.Timestamp(DATETIME_MIN) >= pd.Timestamp.min.tz_localize("UTC")
    assert pd.Timestamp(DATETIME_MAX) <= pd.Timestamp.max.tz_localize("UTC")


_ARROW = {
    "edge_case_string": pa.string(),
    "edge_case_unicode": pa.string(),
    "edge_case_integer": pa.int64(),
    "edge_case_float": pa.float64(),
    "edge_case_date": pa.date32(),
    "edge_case_datetime": pa.timestamp("ns", tz="UTC"),
}


@pytest.mark.parametrize("name", sorted(SPECS))
def test_column_type_does_not_depend_on_the_draw(name: str) -> None:
    values = list(SPECS[name].values)
    # Whole catalogue, extremes only, a middle value alone, and each with a null.
    draws = [values, [values[0], values[-1]], [values[len(values) // 2]]]
    for draw in [*draws, *([*d, None] for d in draws)]:
        df = pd.DataFrame({"c": ["x"] * len(draw)})
        df["c"] = draw
        if name == "edge_case_integer" and None in draw:
            continue  # nullable ints become float64: known limit, pinned end to end
        assert pa.Table.from_pandas(df, preserve_index=False).schema.field("c").type == _ARROW[name]


@pytest.mark.parametrize("name", TEXT)
def test_text_round_trips_through_csv_and_parquet(name: str) -> None:
    values = list(SPECS[name].values)
    frame = pd.DataFrame({"v": values, "k": range(len(values))})
    text = frame.to_csv(index=False)
    back = pd.read_csv(io.StringIO(text), keep_default_na=False, dtype=str)
    assert back["v"].tolist() == values
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), buf)
    assert pq.read_table(io.BytesIO(buf.getvalue())).column("v").to_pylist() == values


@pytest.mark.parametrize("name", sorted(set(SPECS) - set(TEXT)))
def test_values_round_trip_through_parquet(name: str) -> None:
    values = list(SPECS[name].values)
    table = pa.table({"v": pa.array(values)})
    buf = io.BytesIO()
    pq.write_table(table, buf)
    back = pq.read_table(io.BytesIO(buf.getvalue())).column("v").to_pylist()
    if name == "edge_case_float":
        assert [struct.pack("<d", v) for v in back] == [struct.pack("<d", v) for v in values]
    else:
        assert back == values


def test_floats_round_trip_through_csv_with_the_round_trip_parser() -> None:
    values = list(SPECS["edge_case_float"].values)
    text = pd.DataFrame({"v": values}).to_csv(index=False)
    back = pd.read_csv(io.StringIO(text), float_precision="round_trip")["v"].tolist()
    assert [struct.pack("<d", v) for v in back] == [struct.pack("<d", v) for v in values]


# -- construction refusals ---------------------------------------------------------------

_REFUSED: list[tuple[Kind, object, str]] = [
    ("ascii_text", "a\x00b", "control character"),
    ("ascii_text", "a\rb", "lone CR"),
    ("ascii_text", "a\x1b[31mb", "control character"),
    ("unicode_text", "a\ud800", "not valid Unicode"),
    ("unicode_text", "\u00e9\ufffe", "noncharacter"),
    ("unicode_text", "\u00e9\ue000", "private-use"),
    ("unicode_text", "\u00e9\U000e0041", "format character outside the allowlist"),
    ("unicode_text", "\u00e9\u206a", "format character outside the allowlist"),
    ("unicode_text", "\u202e\u00e9", "unbalanced bidi"),
    ("unicode_text", "\u2067\u00e9", "unbalanced bidi"),
    ("unicode_text", "\u202c\u00e9\u202e", "unbalanced bidi"),
    ("unicode_text", "\u202c\u00e9", "unbalanced bidi"),  # bare closer
    ("unicode_text", "\u2069\u00e9", "unbalanced bidi"),  # bare isolate closer
    ("ascii_text", "caf\u00e9", "non-ASCII"),
    ("unicode_text", "plain", "ASCII-only"),
    ("integer", True, "not an int"),
    ("integer", 2**63, "outside signed 64-bit"),
    ("float", float("nan"), "not finite"),
    ("float", float("inf"), "not finite"),
    ("float", 1, "not a float"),
    ("date", dt.datetime(2024, 1, 1), "not exactly a datetime.date"),
    ("datetime", dt.datetime(2024, 1, 1), "not UTC-aware"),
    ("datetime", dt.datetime(2024, 1, 1, tzinfo=ZoneInfo("UTC")), "not UTC-aware"),
    ("datetime", dt.datetime(9999, 12, 31, tzinfo=dt.timezone.utc), "nanosecond range"),
    ("datetime", DATETIME_MIN - dt.timedelta(microseconds=1), "nanosecond range"),
    # Injection text that would add an operation of its own.
    ("ascii_text", "' OR '1'='1", "disjunction"),
    ("ascii_text", '" AND ""=""', "tautology"),
    ("ascii_text", "1 AND 1=1", "tautology"),
    ("ascii_text", "x; y", "separator"),
    ("ascii_text", "a|b", "separator"),
    ("ascii_text", "a&b", "separator"),
    ("ascii_text", "x' --", "comment"),
    ("ascii_text", "/* c */", "comment"),
    ("ascii_text", "x' #", "comment"),
    ("ascii_text", "<img src=x>", "resource_attribute"),
    ("ascii_text", "<a href=y>", "resource_attribute"),
    ("ascii_text", "%s", "printf conversion"),
    ("ascii_text", "%d%%", "printf conversion"),
    ("ascii_text", "http://example.invalid", "host_relative_path"),
    ("ascii_text", "${jndi:x}", "lookup"),
    ("ascii_text", "DROP it", "modifying_keyword"),
    ("ascii_text", "rm x", "modifying_keyword"),
    ("ascii_text", "' UNION SELECT password FROM users", "query_keyword"),
    ("ascii_text", "x' SELECT 1", "query_keyword"),
    ("ascii_text", "' AND '1'='2' INTO OUTFILE '/tmp/x'", "file_export"),
    ("ascii_text", "COPY t TO PROGRAM 'id'", "file_export"),
    ("ascii_text", "EXECUTE xp_cmdshell 'dir'", "command_execution"),
    ("ascii_text", "exec x", "command_execution"),
    ("ascii_text", "$(cat /etc/passwd)", "command_substitution"),
    ("ascii_text", "$(touch decoy)", "command_substitution"),
    ("ascii_text", "`id`", "command_substitution"),
    ("ascii_text", "<script>alert(1)</script>", "script_tag"),
    ("ascii_text", "<video poster=x>", "resource_attribute"),
    ("ascii_text", "<body background=x>", "resource_attribute"),
    ("ascii_text", "<iframe srcdoc=x>", "resource_attribute"),
    ("ascii_text", "<div style=background:url(x)>", "css_url"),
    ("ascii_text", "fetch('x')", "fetch_call"),
    ("ascii_text", "<a b=//example.com/x>", "host_relative_path"),
    ("ascii_text", "\\\\example.com\\share", "unc_path"),
    ("ascii_text", "=IMPORTXML(1,2)", "spreadsheet_network"),
    ("ascii_text", "=WEBSERVICE(1)", "spreadsheet_network"),
    ("ascii_text", "=IMAGE(1)", "spreadsheet_network"),
    ("ascii_text", "=HYPERLINK(1)", "spreadsheet_network"),
    ("unicode_text", "\uff24\uff32\uff2f\uff30 it", "modifying_keyword"),  # full-width DROP
    ("unicode_text", "x\uff1b y", "separator"),  # full-width semicolon
]


@pytest.mark.parametrize(("kind", "value", "rule"), _REFUSED, ids=lambda x: repr(x)[:24])
def test_construction_refuses_unsafe_values_without_echoing_them(
    kind: Kind, value: object, rule: str
) -> None:
    with pytest.raises(ValueError) as excinfo:
        EdgeCaseSpec("p", kind, ("c",), (EdgeCase("c", value),))
    message = str(excinfo.value)
    assert rule in message
    assert repr(value) not in message and str(value) not in message


def test_construction_refuses_duplicates_and_bad_categories() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        EdgeCaseSpec("p", "float", ("c",), (EdgeCase("c", 0.5), EdgeCase("c", 0.5)))
    # 0.0 and -0.0 are different edge cases.
    EdgeCaseSpec("p", "float", ("c",), (EdgeCase("c", 0.0), EdgeCase("c", -0.0)))
    with pytest.raises(ValueError, match="has no values"):
        EdgeCaseSpec("p", "integer", ("c", "d"), (EdgeCase("c", 1),))
    with pytest.raises(ValueError, match="undeclared category"):
        EdgeCaseSpec("p", "integer", ("c",), (EdgeCase("d", 1),))


# The reviewed injection set. The guarantee rests on review of exactly these values, so any
# change here needs a fresh review; the pattern list is only a backstop.
_REVIEWED_INJECTION_TEXT = [
    "' AND '1'='2",
    '" AND "1"="2',
    "1 AND 1=2",
    "<img onerror=alert(1)>",
    "{{7*7}}",
    "${7*7}",
    "#{7*7}",
    "100%%",
    "../../../../decoy-nonexistent",
    "=1+1",
    "+1+1",
    "-1+1",
    "@SUM(1,1)",
]


def test_injection_text_is_exactly_the_reviewed_set() -> None:
    assert _in("edge_case_string", "injection_text") == _REVIEWED_INJECTION_TEXT
