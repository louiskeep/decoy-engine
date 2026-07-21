"""HCPCS parser correctness: multi-line join, modifier exclusion, fail-closed checks.

Fixture data (tests/unit/codeset_etl/_fixtures.py) is a handful of REAL rows
copied from a live CMS HCPCS Level II 2026Q3 ANWEB pull, not the ~2.4MB full
download -- chosen to exercise the multi-line long-description join (the
buggy naive-concatenation case included) and the modifier-record exclusion.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest
from codeset_etl._errors import CodesetParseError
from codeset_etl.parsers._hcpcs import HcpcsParser

from ._fixtures import HCPCS_EXPECTED_DESCRIPTIONS, build_hcpcs_zip

_PULLED_ON = date(2026, 7, 21)


class TestHcpcsParserParseArchive:
    def test_every_fixture_code_gets_the_expected_joined_description(self):
        parser = HcpcsParser()
        result = parser.parse_archive(build_hcpcs_zip(), pulled_on=_PULLED_ON)

        by_code = {row["code"]: row["description"] for row in result.rows}
        assert by_code == HCPCS_EXPECTED_DESCRIPTIONS

    def test_multiline_join_inserts_a_space_at_every_continuation_boundary(self):
        """A0384's real continuation lines are NOT re-padded by the export;
        a naive raw-slice concatenation collapses "...defibrillation is" +
        "permitted..." into "ispermitted". This is the case that must not
        regress."""
        parser = HcpcsParser()
        result = parser.parse_archive(build_hcpcs_zip(), pulled_on=_PULLED_ON)

        by_code = {row["code"]: row["description"] for row in result.rows}
        assert "is permitted" in by_code["A0384"]
        assert "ispermitted" not in by_code["A0384"]

    def test_modifier_records_are_excluded(self):
        """RIC='7'/'8' records (2-char modifiers, e.g. "A1") are not billable
        HCPCS Level II procedure codes and must not appear as rows."""
        parser = HcpcsParser()
        result = parser.parse_archive(build_hcpcs_zip(), pulled_on=_PULLED_ON)

        codes = {row["code"] for row in result.rows}
        assert "A1" not in codes
        assert all(len(code) == 5 for code in codes)

    def test_rows_have_exactly_code_and_description_columns(self):
        """No 'chapter' column: HCPCS Level II has no ICD-style chapter
        taxonomy (see parsers/_hcpcs.py's module docstring)."""
        parser = HcpcsParser()
        result = parser.parse_archive(build_hcpcs_zip(), pulled_on=_PULLED_ON)

        for row in result.rows:
            assert set(row.keys()) == {"code", "description"}

    def test_provenance_fields_use_the_quarterly_release_id(self):
        parser = HcpcsParser()
        result = parser.parse_archive(build_hcpcs_zip(), pulled_on=_PULLED_ON)

        assert result.source
        assert result.source_url == HcpcsParser.source_url
        assert result.license
        assert result.citation
        assert result.source_version == "2026Q3"
        assert result.effective_date == "2026-07-01"

    def test_bad_zip_raises_codeset_parse_error(self):
        parser = HcpcsParser()
        with pytest.raises(CodesetParseError):
            parser.parse_archive(b"this is not a zip file", pulled_on=_PULLED_ON)

    def test_missing_codes_member_raises_codeset_parse_error(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("HCPC2026_recordlayout.txt", "some other file\r\n")
        parser = HcpcsParser()
        with pytest.raises(CodesetParseError, match="anweb"):
            parser.parse_archive(buf.getvalue(), pulled_on=_PULLED_ON)

    def test_ambiguous_multiple_codes_members_raises_codeset_parse_error(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("HCPC2026_JUL_ANWEB_06172026.txt", "G0008001003X" + " " * 80)
            zf.writestr("HCPC2026_JAN_ANWEB_01122026.txt", "G0008001003X" + " " * 80)
        parser = HcpcsParser()
        with pytest.raises(CodesetParseError, match="ambiguous"):
            parser.parse_archive(buf.getvalue(), pulled_on=_PULLED_ON)

    def test_empty_codes_member_raises_codeset_parse_error(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("empty_anweb.txt", "\r\n")
        parser = HcpcsParser()
        with pytest.raises(CodesetParseError, match="zero data rows"):
            parser.parse_archive(buf.getvalue(), pulled_on=_PULLED_ON)

    def test_non_hcpcs_shaped_code_raises_codeset_parse_error(self):
        """A 5-digit numeric code (CPT-4/Level I shape) must never be
        silently absorbed into this public-domain corpus -- CPT-4 is
        AMA-copyrighted."""
        bad_line = "12345" + "00100" + "3" + "Fabricated CPT-shaped code".ljust(80)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("HCPC2026_JUL_ANWEB_06172026.txt", bad_line + "\r\n")
        parser = HcpcsParser()
        with pytest.raises(CodesetParseError, match="does not match"):
            parser.parse_archive(buf.getvalue(), pulled_on=_PULLED_ON)

    def test_cdt_d_code_is_rejected_fail_closed(self):
        """CDT dental codes (the D-series, e.g. "D0120") are ADA-copyrighted
        and must never be bundled into this public-domain corpus. The shape
        guard's letter class excludes D specifically so a D-shaped record
        raises rather than passing the letter+4-digit check by accident."""
        d_line = "D0120" + "00100" + "3" + "Periodic oral evaluation".ljust(80)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("HCPC2026_JUL_ANWEB_06172026.txt", d_line + "\r\n")
        parser = HcpcsParser()
        with pytest.raises(CodesetParseError, match="does not match"):
            parser.parse_archive(buf.getvalue(), pulled_on=_PULLED_ON)
