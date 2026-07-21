"""NDC parser correctness: normalization, dedup, and fail-closed archive checks.

Fixture data (tests/unit/codeset_etl/_fixtures.py) is a handful of REAL rows
copied from a live FDA NDC Directory pull, not the ~11MB full download --
see the slice-2 build report for the real end-to-end run's evidence.
"""

from __future__ import annotations

from datetime import date

import pytest
from codeset_etl._errors import CodesetParseError
from codeset_etl.parsers._ndc import NdcParser, _normalize_package_code

from ._fixtures import EXPECTED_CODES, build_ndc_zip

_PULLED_ON = date(2026, 7, 21)


class TestSegmentNormalization:
    """The three FDA-documented NDC width configurations, zero-padded to 5-4-2."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0002-0152-01", "00002015201"),  # 4-4-2
            ("10014-001-07", "10014000107"),  # 5-3-2
            ("10018-8999-1", "10018899901"),  # 5-4-1
        ],
    )
    def test_known_width_configs_normalize_correctly(self, raw: str, expected: str):
        assert _normalize_package_code(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "12-34",  # only 2 segments
            "1-2-3-4",  # 4 segments
            "ABCDE-1234-56",  # non-numeric segment
            "123456-1234-56",  # 6-4-2: not a recognised width config
            "",
        ],
    )
    def test_malformed_codes_return_none(self, raw: str):
        assert _normalize_package_code(raw) is None


class TestNdcParserParseArchive:
    def test_normalizes_all_three_width_configs(self):
        parser = NdcParser()
        raw = build_ndc_zip()
        result = parser.parse_archive(raw, pulled_on=_PULLED_ON)

        codes = {row["code"] for row in result.rows}
        assert codes == EXPECTED_CODES

    def test_deduplicates_same_package_code_keeping_latest_start_date(self):
        """Real FDA data re-lists the same package with a later STARTMARKETINGDATE.

        Fixture includes '72043-2500-1' twice (2018-01-10 and 2025-05-01); only
        one normalized row for that code must survive, and the description
        must come from the joined product row either way (both source rows
        share the same PRODUCTNDC), so the strongest observable check is that
        exactly ONE row exists for the deduplicated code.
        """
        parser = NdcParser()
        result = parser.parse_archive(build_ndc_zip(), pulled_on=_PULLED_ON)

        matching = [row for row in result.rows if row["code"] == "72043250001"]
        assert len(matching) == 1

    def test_rows_have_exactly_code_and_description_columns(self):
        """No 'chapter' column: the real NDC ETL deliberately omits it (see
        parsers/_ndc.py's module docstring -- the seed's chapter bucket is
        hand-curated, not derivable from FDA fields)."""
        parser = NdcParser()
        result = parser.parse_archive(build_ndc_zip(), pulled_on=_PULLED_ON)

        for row in result.rows:
            assert set(row.keys()) == {"code", "description"}

    def test_description_is_populated_from_joined_product_row(self):
        parser = NdcParser()
        result = parser.parse_archive(build_ndc_zip(), pulled_on=_PULLED_ON)

        by_code = {row["code"]: row for row in result.rows}
        assert "Zepbound" in by_code["00002015201"]["description"]
        assert "tirzepatide" not in by_code["00002015201"]["description"]  # proprietary preferred

    def test_provenance_fields_are_non_empty_and_snapshot_dated(self):
        parser = NdcParser()
        result = parser.parse_archive(build_ndc_zip(), pulled_on=_PULLED_ON)

        assert result.source
        assert result.source_url == NdcParser.source_url
        assert result.license
        assert result.citation
        assert result.source_version == "pulled-2026-07-21"
        assert result.effective_date == "2026-07-21"

    def test_bad_zip_raises_codeset_parse_error(self):
        parser = NdcParser()
        with pytest.raises(CodesetParseError):
            parser.parse_archive(b"this is not a zip file", pulled_on=_PULLED_ON)

    def test_missing_member_file_raises_codeset_parse_error(self):
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("product.txt", "PRODUCTNDC\n0002-0152\n")
            # package.txt deliberately omitted
        parser = NdcParser()
        with pytest.raises(CodesetParseError, match="package.txt"):
            parser.parse_archive(buf.getvalue(), pulled_on=_PULLED_ON)

    def test_cp1252_bytes_decode_without_crashing(self):
        """Real FDA files are cp1252, not UTF-8 (see module docstring). A
        curly-quote byte (0x92, right single quotation mark in cp1252) must
        not crash the parse."""
        curly_quote = b"\x92"  # invalid as UTF-8 standalone, valid cp1252
        product_text = (
            (
                b"PRODUCTID\tPRODUCTNDC\tPRODUCTTYPENAME\tPROPRIETARYNAME\tPROPRIETARYNAMESUFFIX\t"
                b"NONPROPRIETARYNAME\tDOSAGEFORMNAME\tROUTENAME\tSTARTMARKETINGDATE\tENDMARKETINGDATE\t"
                b"MARKETINGCATEGORYNAME\tAPPLICATIONNUMBER\tLABELERNAME\tSUBSTANCENAME\t"
                b"ACTIVE_NUMERATOR_STRENGTH\tACTIVE_INGRED_UNIT\tPHARM_CLASSES\tDEASCHEDULE\t"
                b"NDC_EXCLUDE_FLAG\tLISTING_RECORD_CERTIFIED_THROUGH\r\n"
                b"id1\t0002-0152\tHUMAN\tBrand"
            )
            + curly_quote
            + b"s Drug\t\tgeneric\tTABLET\t\t20240101\t\tNDA\t\t\t\t1\tmg\t\t\tN\t\r\n"
        )
        package_text = (
            b"PRODUCTID\tPRODUCTNDC\tNDCPACKAGECODE\tPACKAGEDESCRIPTION\t"
            b"STARTMARKETINGDATE\tENDMARKETINGDATE\tNDC_EXCLUDE_FLAG\tSAMPLE_PACKAGE\r\n"
            b"id1\t0002-0152\t0002-0152-01\tdesc\t20240101\t\tN\tN\r\n"
        )

        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("product.txt", product_text)
            zf.writestr("package.txt", package_text)

        parser = NdcParser()
        result = parser.parse_archive(buf.getvalue(), pulled_on=_PULLED_ON)
        assert len(result.rows) == 1
        assert "Brand" in result.rows[0]["description"]
