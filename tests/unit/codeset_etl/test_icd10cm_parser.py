"""ICD-10-CM parser correctness: chapter range table and fail-closed archive checks.

Fixture data (tests/unit/codeset_etl/_fixtures.py) is a handful of REAL lines
copied from a live CDC NCHS icd10cm-codes-2026.txt pull, not the ~6MB full
text file -- chosen to exercise every chapter-range boundary case.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest
from codeset_etl._errors import CodesetParseError
from codeset_etl.parsers._icd10cm import Icd10CmParser, _chapter_for_category

from ._fixtures import ICD10CM_EXPECTED_CHAPTERS, build_icd10cm_zip

_PULLED_ON = date(2026, 7, 21)


class TestChapterRangeTable:
    """The 22-chapter category-range table, including its trickiest boundaries."""

    @pytest.mark.parametrize(
        ("category", "expected_chapter"),
        [
            ("A00", "I"),
            ("B99", "I"),
            ("C00", "II"),  # letter-spanning range II starts at C
            ("C99", "II"),  # all of C is chapter II regardless of the 2-digit suffix
            ("D49", "II"),  # ... and D up to D49 is still chapter II
            ("D50", "III"),  # D50 is the chapter III cutover
            ("D89", "III"),
            ("O99", "XV"),  # plain 2-digit suffix just below the alphanumeric edge
            ("O9A", "XV"),  # the alphanumeric boundary itself: chapter XV's own top
            ("QA0", "XVII"),  # FY2026 addition (verified via live download)
            ("S00", "XIX"),  # letter-spanning range XIX starts at S
            ("T88", "XIX"),  # ... and ends mid-T
            ("V00", "XX"),  # letter-spanning range XX starts at V
            ("Y99", "XX"),  # ... and ends at Y
            ("Z00", "XXI"),
            ("U00", "XXII"),
            ("U07", "XXII"),  # sits out of alphabetical chapter order, after Z
            ("U85", "XXII"),
        ],
    )
    def test_known_boundaries_map_to_expected_chapter(self, category, expected_chapter):
        assert _chapter_for_category(category) == expected_chapter

    @pytest.mark.parametrize("category", ["T89", "QA1", "U86", "ZZZ"])
    def test_categories_outside_every_range_return_none(self, category):
        assert _chapter_for_category(category) is None


class TestIcd10CmParserParseArchive:
    def test_every_fixture_code_gets_the_expected_chapter(self):
        parser = Icd10CmParser()
        result = parser.parse_archive(build_icd10cm_zip(), pulled_on=_PULLED_ON)

        by_code = {row["code"]: row for row in result.rows}
        assert set(by_code) == set(ICD10CM_EXPECTED_CHAPTERS)
        for code, expected_chapter in ICD10CM_EXPECTED_CHAPTERS.items():
            assert by_code[code]["chapter"] == expected_chapter

    def test_description_is_split_and_trailing_whitespace_stripped(self):
        parser = Icd10CmParser()
        result = parser.parse_archive(build_icd10cm_zip(), pulled_on=_PULLED_ON)

        by_code = {row["code"]: row for row in result.rows}
        assert (
            by_code["A000"]["description"] == "Cholera due to Vibrio cholerae 01, biovar cholerae"
        )
        assert by_code["Z0000"]["description"] == (
            "Encounter for general adult medical examination without abnormal findings"
        )

    def test_rows_have_exactly_code_description_chapter_columns(self):
        parser = Icd10CmParser()
        result = parser.parse_archive(build_icd10cm_zip(), pulled_on=_PULLED_ON)

        for row in result.rows:
            assert set(row.keys()) == {"code", "description", "chapter"}

    def test_provenance_fields_use_fy2026_federal_fiscal_year_convention(self):
        parser = Icd10CmParser()
        result = parser.parse_archive(build_icd10cm_zip(), pulled_on=_PULLED_ON)

        assert result.source
        assert result.source_url == Icd10CmParser.source_url
        assert result.license
        assert result.citation
        assert result.source_version == "FY2026"
        assert result.effective_date == "2025-10-01"

    def test_finds_codes_member_by_suffix_when_nested_in_a_subfolder(self):
        """The zip layout is not guaranteed flat across FY releases -- the
        member is located by basename suffix, not a hardcoded full path."""
        parser = Icd10CmParser()
        raw = build_icd10cm_zip(member="nested/dir/icd10cm-codes-2026.txt")
        result = parser.parse_archive(raw, pulled_on=_PULLED_ON)
        assert len(result.rows) == len(ICD10CM_EXPECTED_CHAPTERS)

    def test_bad_zip_raises_codeset_parse_error(self):
        parser = Icd10CmParser()
        with pytest.raises(CodesetParseError):
            parser.parse_archive(b"this is not a zip file", pulled_on=_PULLED_ON)

    def test_missing_codes_member_raises_codeset_parse_error(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("icd10cm-order-2026.txt", "some other file\r\n")
        parser = Icd10CmParser()
        with pytest.raises(CodesetParseError, match="codes-2026.txt"):
            parser.parse_archive(buf.getvalue(), pulled_on=_PULLED_ON)

    def test_empty_codes_member_raises_codeset_parse_error(self):
        raw = build_icd10cm_zip(lines=[])
        parser = Icd10CmParser()
        with pytest.raises(CodesetParseError, match="zero data rows"):
            parser.parse_archive(raw, pulled_on=_PULLED_ON)

    def test_code_with_unmappable_category_raises_codeset_parse_error(self):
        raw = build_icd10cm_zip(lines=["ZZZ000  Not a real chapter category"])
        parser = Icd10CmParser()
        with pytest.raises(CodesetParseError, match="chapter"):
            parser.parse_archive(raw, pulled_on=_PULLED_ON)
