"""ICD-10-PCS parser correctness: shape guard, section/chapter assignment, fail-closed checks.

Fixture data (tests/unit/codeset_etl/_fixtures.py) is a handful of REAL lines
copied from a live CMS icd10pcs_codes_2026.txt pull, not the ~6.6MB full
text file -- chosen to cover several different PCS Sections.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest
from codeset_etl._errors import CodesetParseError
from codeset_etl.parsers._icd10pcs import Icd10PcsParser

from ._fixtures import PCS_EXPECTED_ROWS, PCS_LINES, build_pcs_zip

_PULLED_ON = date(2026, 7, 21)


class TestIcd10PcsParserParseArchive:
    def test_every_fixture_code_gets_the_expected_description_and_chapter(self):
        parser = Icd10PcsParser()
        result = parser.parse_archive(build_pcs_zip(), pulled_on=_PULLED_ON)

        by_code = {row["code"]: row for row in result.rows}
        assert set(by_code) == set(PCS_EXPECTED_ROWS)
        for code, expected in PCS_EXPECTED_ROWS.items():
            assert by_code[code]["description"] == expected["description"]
            assert by_code[code]["chapter"] == expected["chapter"]

    def test_row_count_matches_fixture_line_count(self):
        parser = Icd10PcsParser()
        result = parser.parse_archive(build_pcs_zip(), pulled_on=_PULLED_ON)
        assert len(result.rows) == len(PCS_LINES)

    def test_chapter_is_fully_populated_never_null_or_empty(self):
        parser = Icd10PcsParser()
        result = parser.parse_archive(build_pcs_zip(), pulled_on=_PULLED_ON)

        for row in result.rows:
            assert row["chapter"] not in (None, "")

    def test_rows_have_exactly_code_description_chapter_columns(self):
        parser = Icd10PcsParser()
        result = parser.parse_archive(build_pcs_zip(), pulled_on=_PULLED_ON)

        for row in result.rows:
            assert set(row.keys()) == {"code", "description", "chapter"}

    def test_blank_line_is_skipped(self):
        lines = [*PCS_LINES, ""]
        raw = build_pcs_zip(lines=lines)
        parser = Icd10PcsParser()
        result = parser.parse_archive(raw, pulled_on=_PULLED_ON)
        assert len(result.rows) == len(PCS_LINES)

    def test_provenance_fields_use_fy2026_federal_fiscal_year_convention(self):
        parser = Icd10PcsParser()
        result = parser.parse_archive(build_pcs_zip(), pulled_on=_PULLED_ON)

        assert result.source
        assert result.source_url == Icd10PcsParser.source_url
        assert result.license
        assert result.citation
        assert result.source_version == "FY2026"
        assert result.effective_date == "2025-10-01"

    def test_finds_codes_member_alongside_the_addenda_file(self):
        """A real download always has both files present -- the addenda
        file's name does not contain the "icd10pcs_codes" marker, so the
        lookup resolves the codes file unambiguously."""
        raw = build_pcs_zip(include_addenda=True)
        parser = Icd10PcsParser()
        result = parser.parse_archive(raw, pulled_on=_PULLED_ON)
        assert len(result.rows) == len(PCS_LINES)

    def test_bad_zip_raises_codeset_parse_error(self):
        parser = Icd10PcsParser()
        with pytest.raises(CodesetParseError):
            parser.parse_archive(b"this is not a zip file", pulled_on=_PULLED_ON)

    def test_missing_codes_member_raises_codeset_parse_error(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("codes_addenda_2026.txt", "some other file\r\n")
        parser = Icd10PcsParser()
        with pytest.raises(CodesetParseError, match="icd10pcs_codes"):
            parser.parse_archive(buf.getvalue(), pulled_on=_PULLED_ON)

    def test_ambiguous_multiple_codes_members_raises_codeset_parse_error(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("icd10pcs_codes_2026.txt", "0016070 Bypass\r\n")
            zf.writestr("nested/icd10pcs_codes_2026_copy.txt", "0016070 Bypass\r\n")
        parser = Icd10PcsParser()
        with pytest.raises(CodesetParseError, match="ambiguous"):
            parser.parse_archive(buf.getvalue(), pulled_on=_PULLED_ON)

    def test_empty_codes_member_raises_codeset_parse_error(self):
        raw = build_pcs_zip(lines=[])
        parser = Icd10PcsParser()
        with pytest.raises(CodesetParseError, match="zero data rows"):
            parser.parse_archive(raw, pulled_on=_PULLED_ON)

    def test_wrong_length_code_raises_codeset_parse_error(self):
        raw = build_pcs_zip(lines=["001607 Truncated six-char code"])
        parser = Icd10PcsParser()
        with pytest.raises(CodesetParseError, match="does not match"):
            parser.parse_archive(raw, pulled_on=_PULLED_ON)

    def test_forbidden_letter_i_raises_codeset_parse_error(self):
        """ICD-10-PCS never uses 'I' (confusable with the digit 1) -- a code
        containing it signals a source layout change or garbled bytes."""
        raw = build_pcs_zip(lines=["0I16070 Forbidden I character"])
        parser = Icd10PcsParser()
        with pytest.raises(CodesetParseError, match="does not match"):
            parser.parse_archive(raw, pulled_on=_PULLED_ON)

    def test_forbidden_letter_o_raises_codeset_parse_error(self):
        """ICD-10-PCS never uses 'O' (confusable with the digit 0)."""
        raw = build_pcs_zip(lines=["001607O Forbidden O character"])
        parser = Icd10PcsParser()
        with pytest.raises(CodesetParseError, match="does not match"):
            parser.parse_archive(raw, pulled_on=_PULLED_ON)

    def test_unknown_section_raises_codeset_parse_error(self):
        """A code shaped correctly but starting with a letter outside the 17
        known PCS sections (e.g. 'A', never used as a section) must fail
        closed rather than silently admitting an unrecognized chapter."""
        raw = build_pcs_zip(lines=["A016070 Not a real PCS section"])
        parser = Icd10PcsParser()
        with pytest.raises(CodesetParseError, match="section"):
            parser.parse_archive(raw, pulled_on=_PULLED_ON)
