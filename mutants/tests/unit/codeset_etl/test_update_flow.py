"""update_corpus: the fetch -> parse -> validate -> write orchestration.

Every test here injects ``fetch_fn`` -- NONE of them touch the network, per
the slice-2 spec ("do not hit the network in unit tests").
"""

from __future__ import annotations

from datetime import date

import pytest
from codeset_etl._errors import CodesetEtlError, CodesetFetchError, CodesetValidationError
from codeset_etl.parsers import PARSERS
from codeset_etl.update import update_corpus

from ._fixtures import EXPECTED_CODES, build_ndc_zip

_PULLED_ON = date(2026, 7, 21)


class TestFailClosedOnBadDownload:
    def test_short_truncated_download_raises_and_writes_nothing(self, tmp_path):
        def fake_fetch(url: str) -> bytes:
            return b"truncated"  # far under NdcParser.min_source_bytes

        with pytest.raises(CodesetFetchError, match="truncated|bytes"):
            update_corpus("ndc", cache_dir=tmp_path, fetch_fn=fake_fetch, pulled_on=_PULLED_ON)

        assert list(tmp_path.iterdir()) == []

    def test_valid_zip_but_too_few_rows_raises_and_writes_nothing(self, tmp_path, monkeypatch):
        """A well-formed archive that parses to far fewer rows than the known
        source size must still abort -- guards against a silently truncated
        or reformatted upstream file that happens to still be a valid zip."""
        ndc_parser = PARSERS["ndc"]
        monkeypatch.setattr(ndc_parser, "min_source_bytes", 10)  # let the tiny fixture through
        # min_row_count stays at the real 100_000 floor -- the fixture's 4
        # rows must trip it.

        def fake_fetch(url: str) -> bytes:
            return build_ndc_zip()

        with pytest.raises(CodesetValidationError, match="row"):
            update_corpus("ndc", cache_dir=tmp_path, fetch_fn=fake_fetch, pulled_on=_PULLED_ON)

        assert list(tmp_path.iterdir()) == []

    def test_unknown_corpus_name_raises(self, tmp_path):
        with pytest.raises(CodesetEtlError, match="no ETL parser registered"):
            update_corpus("not_a_real_corpus", cache_dir=tmp_path, fetch_fn=lambda url: b"")


class TestEndToEndWithMockedNetwork:
    def test_full_flow_writes_loadable_corpus(self, tmp_path, monkeypatch):
        ndc_parser = PARSERS["ndc"]
        monkeypatch.setattr(ndc_parser, "min_source_bytes", 10)
        monkeypatch.setattr(ndc_parser, "min_row_count", 1)

        fetched_urls: list[str] = []

        def fake_fetch(url: str) -> bytes:
            fetched_urls.append(url)
            return build_ndc_zip()

        result = update_corpus("ndc", cache_dir=tmp_path, fetch_fn=fake_fetch, pulled_on=_PULLED_ON)

        assert fetched_urls == [ndc_parser.source_url]
        assert result.row_count == len(EXPECTED_CODES)
        assert result.path == tmp_path / "ndc.parquet"
        assert result.path.exists()
        assert result.source_version == "pulled-2026-07-21"
        assert result.effective_date == "2026-07-21"

    def test_default_cache_dir_used_when_not_given(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DECOY_CODESET_CACHE_DIR", str(tmp_path))
        ndc_parser = PARSERS["ndc"]
        monkeypatch.setattr(ndc_parser, "min_source_bytes", 10)
        monkeypatch.setattr(ndc_parser, "min_row_count", 1)

        result = update_corpus("ndc", fetch_fn=lambda url: build_ndc_zip(), pulled_on=_PULLED_ON)

        assert result.path == tmp_path / "ndc.parquet"


class TestFetchUrlNeverCalledDirectly:
    def test_real_fetch_url_is_not_imported_by_default_path(self, tmp_path, monkeypatch):
        """Belt-and-suspenders: passing fetch_fn must fully replace the real
        network call, not run alongside it (a partial-mock bug would still
        try to hit the network and time out/fail here in CI)."""
        ndc_parser = PARSERS["ndc"]
        monkeypatch.setattr(ndc_parser, "min_source_bytes", 10)
        calls = {"n": 0}

        def fake_fetch(url: str) -> bytes:
            calls["n"] += 1
            return build_ndc_zip()

        with pytest.raises(CodesetValidationError):
            # min_row_count is left at the real floor deliberately: this
            # proves the call reached parse/validate using ONLY fake_fetch's
            # bytes, never a real download's.
            update_corpus("ndc", cache_dir=tmp_path, fetch_fn=fake_fetch, pulled_on=_PULLED_ON)

        assert calls["n"] == 1
