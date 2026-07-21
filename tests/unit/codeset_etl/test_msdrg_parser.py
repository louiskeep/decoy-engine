"""MS-DRG parser correctness: fixed-width offsets, header skip, fail-closed checks.

Fixture data (tests/unit/codeset_etl/_fixtures.py) is a handful of REAL rows
copied from the CMS ICD-10 MS-DRG Definitions Manual v43.0 Appendix A, not
the ~4.6MB full download -- chosen to exercise a multi-MDC blank-MDC row, a
plain-MDC row, and the two ungroupable (blank MDC AND blank type) rows.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest
from codeset_etl._errors import CodesetParseError
from codeset_etl.parsers._msdrg import MsDrgParser

from ._fixtures import (
    MSDRG_BANNER_LINES,
    MSDRG_EXPECTED_DESCRIPTIONS,
    build_msdrg_zip,
)

_PULLED_ON = date(2026, 7, 21)


class TestMsDrgParserParseArchive:
    def test_every_fixture_code_gets_the_expected_description(self):
        parser = MsDrgParser()
        result = parser.parse_archive(build_msdrg_zip(), pulled_on=_PULLED_ON)

        by_code = {row["code"]: row["description"] for row in result.rows}
        assert by_code == MSDRG_EXPECTED_DESCRIPTIONS

    def test_leading_zero_codes_are_preserved_as_strings(self):
        """DRG "001" must stay the 3-char string "001", not become int 1."""
        parser = MsDrgParser()
        result = parser.parse_archive(build_msdrg_zip(), pulled_on=_PULLED_ON)

        codes = {row["code"] for row in result.rows}
        assert "001" in codes
        assert 1 not in codes

    def test_banner_and_header_lines_are_not_included_as_rows(self):
        """None of MSDRG_BANNER_LINES' text should leak into a row's fields."""
        parser = MsDrgParser()
        result = parser.parse_archive(build_msdrg_zip(), pulled_on=_PULLED_ON)

        assert len(result.rows) == len(MSDRG_EXPECTED_DESCRIPTIONS)
        descriptions = {row["description"] for row in result.rows}
        for banner_line in MSDRG_BANNER_LINES:
            assert banner_line not in descriptions

    def test_multi_mdc_and_ungroupable_blank_mdc_rows_are_included(self):
        """001 (multi-MDC) and 998/999 (ungroupable) all have a blank MDC in
        the real source but must still appear as ordinary rows."""
        parser = MsDrgParser()
        result = parser.parse_archive(build_msdrg_zip(), pulled_on=_PULLED_ON)

        codes = {row["code"] for row in result.rows}
        assert {"001", "998", "999"} <= codes

    def test_rows_have_exactly_code_and_description_columns(self):
        """No 'chapter' column: MDC is blank for ~30 DRGs, and a corpus with
        a 'chapter' column must populate it for every row (see
        parsers/_msdrg.py's module docstring)."""
        parser = MsDrgParser()
        result = parser.parse_archive(build_msdrg_zip(), pulled_on=_PULLED_ON)

        for row in result.rows:
            assert set(row.keys()) == {"code", "description"}

    def test_provenance_fields_use_the_annual_release_id(self):
        parser = MsDrgParser()
        result = parser.parse_archive(build_msdrg_zip(), pulled_on=_PULLED_ON)

        assert result.source
        assert result.source_url == MsDrgParser.source_url
        assert result.license
        assert result.citation
        assert result.source_version == "v43.0"
        assert result.effective_date == "2025-10-01"

    def test_bad_zip_raises_codeset_parse_error(self):
        parser = MsDrgParser()
        with pytest.raises(CodesetParseError):
            parser.parse_archive(b"this is not a zip file", pulled_on=_PULLED_ON)

    def test_missing_appendix_a_member_raises_codeset_parse_error(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("MDC_01_definitions.txt", "some other file\r\n")
        parser = MsDrgParser()
        with pytest.raises(CodesetParseError, match="appendix_a"):
            parser.parse_archive(buf.getvalue(), pulled_on=_PULLED_ON)

    def test_ambiguous_multiple_appendix_a_members_raises_codeset_parse_error(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("appendix_A.txt", "001     P  Heart Transplant\r\n")
            zf.writestr("Appendix_A_v2.txt", "001     P  Heart Transplant\r\n")
        parser = MsDrgParser()
        with pytest.raises(CodesetParseError, match="ambiguous"):
            parser.parse_archive(buf.getvalue(), pulled_on=_PULLED_ON)

    def test_empty_appendix_a_member_raises_codeset_parse_error(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("appendix_A.txt", "\r\n".join(MSDRG_BANNER_LINES) + "\r\n")
        parser = MsDrgParser()
        with pytest.raises(CodesetParseError, match="zero data rows"):
            parser.parse_archive(buf.getvalue(), pulled_on=_PULLED_ON)

    def test_non_ascii_digit_code_is_rejected_fail_closed(self):
        """A line whose first 3 characters pass Python's broad str.isdigit()
        (e.g. a superscript digit) but are not clean ASCII 0-9 must raise
        rather than being silently absorbed as a DRG code -- see the module
        docstring's isdigit()-vs-[0-9] note."""
        bad_line = "0²9     P  Fabricated superscript-digit code"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("appendix_A.txt", bad_line + "\r\n")
        parser = MsDrgParser()
        with pytest.raises(CodesetParseError, match="does not match"):
            parser.parse_archive(buf.getvalue(), pulled_on=_PULLED_ON)
