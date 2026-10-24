"""Framer grammar (ported from the platform's B5 tests when the framer moved
into the engine, parity E2a)."""

from __future__ import annotations

import io

import pytest

from decoy_engine.io import csv_framer as framer

MIB = 1024 * 1024


def records(data: bytes, *, cap: int = 10 * MIB, block: int = MIB) -> list[bytes]:
    return list(framer.iter_csv_records(io.BytesIO(data), max_record_bytes=cap, block_size=block))


def test_preserves_every_byte_and_splits_on_all_terminators() -> None:
    data = b"a,b\nc,d\r\ne,f\rg,h"
    recs = records(data)
    assert b"".join(recs) == data
    assert [r.rstrip(b"\r\n") for r in recs] == [b"a,b", b"c,d", b"e,f", b"g,h"]


def test_quoted_delimiters_doubled_quotes_and_newlines() -> None:
    data = b'id,txt\n1,"a,b"\n2,"say ""hi"""\n3,"line1\nline2"\r\n4,plain\n'
    recs = records(data)
    assert b"".join(recs) == data
    assert len(recs) == 5


def test_literal_quote_inside_an_unquoted_field() -> None:
    assert len(records(b'a,b\nfoo"bar,2\nx,y\n')) == 3


def test_quote_opens_only_at_field_start() -> None:
    recs = records(b'a,b\n1x"y\n2,"q\nr"\n')
    assert len(recs) == 3 and recs[1] == b'1x"y\n'


@pytest.mark.parametrize("block", [4, 5, 7, 16])
def test_block_size_invariant(block: int) -> None:
    data = b'\xef\xbb\xbf"id","t"\r\n1,"a""b"\r\n2,"x\r\ny"\r3,caf\xc3\xa9\n"4",""\n5,"end"'
    assert records(data, block=block) == records(data, block=len(data) + 10)


def test_crlf_split_across_the_boundary_is_one_terminator() -> None:
    assert records(b"abc\r\ndef\r\n", block=4) == [b"abc\r\n", b"def\r\n"]


def test_bom_keeps_field_start_state() -> None:
    assert len(records(b'\xef\xbb\xbf"a,1",b\nx,y\n')) == 2


def test_unterminated_quote_at_eof_raises() -> None:
    with pytest.raises(framer.CsvUnterminatedQuote):
        records(b'a,b\n1,"never closed\n2,3\n')


def test_record_cap_raises_before_reading_further_blocks() -> None:
    class Counting(io.BytesIO):
        def __init__(self, data: bytes) -> None:
            super().__init__(data)
            self.reads: list[int] = []

        def read(self, n: int = -1) -> bytes:  # type: ignore[override]
            out = super().read(n)
            self.reads.append(len(out))
            return out

    block = 1024
    fh = Counting(b"a,b\n" + b'1,"' + b"x" * (50 * block) + b'"\n2,3\n')
    with pytest.raises(framer.CsvRecordTooLarge):
        list(framer.iter_csv_records(fh, max_record_bytes=4 * block, block_size=block))
    assert len(fh.reads) <= 6


def test_cap_applies_to_the_header_record() -> None:
    with pytest.raises(framer.CsvRecordTooLarge):
        records(b"h" * 5000 + b",b\n1,2\n", cap=1000, block=256)


def test_split_record_fields_handles_quotes_and_bom() -> None:
    assert framer.split_record_fields(b'\xef\xbb\xbf"a,1",b,"c""d"\r\n') == ["a,1", "b", 'c"d']
    assert framer.split_record_fields(b"a,b,\n") == ["a", "b", ""]
    assert framer.split_record_fields(b'"open\n') is None
