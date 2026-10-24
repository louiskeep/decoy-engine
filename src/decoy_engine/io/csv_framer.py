"""A bounded, quote-aware CSV logical-record framer.

Promoted from the platform (parity E2a) so the CLI and the platform frame CSV
records identically; the platform keeps only its pricing check around it.

Why it exists: PyArrow's ``block_size`` does not cap a CSV record (a probe on
the pinned PyArrow 24.0.0 parsed records of 16 MiB + 1 and 20 MiB with a 16 MiB
block), so the record-size cap has to run before PyArrow sees a record. Spark
and Hadoop's record readers frame records the same way: split the byte stream
at record terminators outside quoted fields, then hand whole records to the
parser.

The dialect is the one the engine's CSV reads accept (PyArrow defaults:
delimiter ``,``, quote ``"``, doubled quote, no escape character) with four
framing states:

* field start,
* inside an unquoted field,
* inside a quoted field,
* just after a quote inside a quoted field.

A quote opens a quoted field only at field start; inside an unquoted field it
is literal. ``""`` inside a quoted field is a literal quote, including when the
pair is split across two read blocks. After the closing quote, any byte other
than another quote or a delimiter or terminator continues the field unquoted,
as PyArrow does. CR, LF and CRLF terminate a record outside quoted fields (CRLF
is one terminator; a CR that ends a block waits for the next byte). Inside a
quoted field CR and LF are data. A UTF-8 BOM at file start is kept in the
record bytes and does not leave the field-start state. Every input byte is
preserved; PyArrow validates UTF-8 after framing.

The scan finds the next quote, delimiter or terminator with ``bytes.find`` and
one compiled regular expression, so the per-byte work runs in C and the Python
work is per record and per quote.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import BinaryIO

DEFAULT_BLOCK_SIZE = 1024 * 1024
_BOM = b"\xef\xbb\xbf"
_TERMINATORS = re.compile(rb"\r\n|\r|\n")
_QUOTE = 0x22
_COMMA = 0x2C
_CR = 0x0D
_LF = 0x0A

_CLEAN = 0  # field start or inside an unquoted field
_QUOTED = 1
_AFTER_QUOTE = 2


class CsvRecordTooLarge(Exception):  # noqa: N818 -- coded condition, name fixed by the plan
    """A logical record passed ``max_record_bytes``. ``index`` is its 0-based
    position (0 is the header)."""

    def __init__(self, index: int) -> None:
        super().__init__(f"csv record {index} exceeds the size cap")
        self.index = index


class CsvUnterminatedQuote(Exception):  # noqa: N818 -- coded condition, name fixed by the plan
    """End of file inside a quoted field."""

    def __init__(self, index: int) -> None:
        super().__init__(f"csv record {index} ends inside a quoted field")
        self.index = index


def iter_csv_records(
    fh: BinaryIO, *, max_record_bytes: int, block_size: int = DEFAULT_BLOCK_SIZE
) -> Iterator[bytes]:
    """Yield each logical record's bytes, terminator included.

    Raises ``CsvRecordTooLarge`` as soon as the record in progress is longer
    than ``max_record_bytes`` (terminator excluded), without reading another
    block, and ``CsvUnterminatedQuote`` at end of file inside a quoted field.
    """
    if block_size < 4:
        raise ValueError("block_size must be at least 4")
    state = _CLEAN
    at_start = True  # in _CLEAN: the next byte begins a field
    pieces: list[bytes] = []  # slices of the record in progress from earlier blocks
    rec_len = 0  # total length of ``pieces``
    index = 0
    pending_cr = False  # a CR ended the previous block; CRLF or a lone CR is not decided yet
    first_block = True

    while True:
        block = fh.read(block_size)
        if not block:
            break
        n = len(block)
        pos = 0
        rec_start = 0
        if first_block:
            first_block = False
            if block.startswith(_BOM):
                pos = len(_BOM)  # kept in the record bytes, still at field start
        if pending_cr:
            pending_cr = False
            if block[0] == _LF:
                terminator, pos, rec_start = b"\r\n", max(pos, 1), 1
            else:
                terminator = b"\r"
            yield b"".join(pieces) + terminator
            pieces, rec_len = [], 0
            index += 1
            at_start = True
            state = _CLEAN
        held_cr = False
        while pos < n:
            if state == _QUOTED:
                q = block.find(b'"', pos)
                if q < 0:
                    pos = n
                    break
                pos = q + 1
                state = _AFTER_QUOTE
                continue
            if state == _AFTER_QUOTE:
                if block[pos] == _QUOTE:
                    state = _QUOTED
                    pos += 1
                else:
                    state = _CLEAN
                    at_start = False  # the closing quote ended the quoted field
                continue
            q = block.find(b'"', pos)
            region_end = n if q < 0 else q
            scan_end = region_end
            if q < 0 and block[n - 1] == _CR:
                scan_end = n - 1  # this CR waits for the next block
                held_cr = True
            cursor = pos
            for match in _TERMINATORS.finditer(block, pos, scan_end):
                start, end = match.span()
                if rec_len + (start - rec_start) > max_record_bytes:
                    raise CsvRecordTooLarge(index)
                if pieces:
                    pieces.append(block[rec_start:end])
                    record = b"".join(pieces)
                    pieces, rec_len = [], 0
                else:
                    record = block[rec_start:end]
                yield record
                index += 1
                rec_start = cursor = end
                at_start = True
            if cursor < scan_end:
                at_start = block[scan_end - 1] == _COMMA
            if q < 0:
                pos = n
                break
            # A quote opens a quoted field only at field start; elsewhere it is literal.
            pos = q + 1
            if at_start:
                state = _QUOTED
            else:
                at_start = False
        body_end = n - 1 if held_cr else n
        if rec_start < body_end:
            pieces.append(block[rec_start:body_end])
            rec_len += body_end - rec_start
        if rec_len > max_record_bytes:
            raise CsvRecordTooLarge(index)
        pending_cr = held_cr

    if pending_cr:
        yield b"".join(pieces) + b"\r"
        return
    if state == _QUOTED:
        raise CsvUnterminatedQuote(index)
    if pieces:
        yield b"".join(pieces)


def split_record_fields(record: bytes) -> list[str] | None:
    """Field values of one framed record (BOM and terminator stripped, quoting
    honoured, same dialect as the framer), or None for invalid UTF-8 or an
    unterminated quote. Used for the header only; the ``csv`` module's field
    size limit (131072 characters) would reject a legal wide header."""
    try:
        text = record.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    text = text.removesuffix("\n").removesuffix("\r")
    fields: list[str] = []
    n = len(text)
    i = 0
    while True:
        if i < n and text[i] == '"':
            parts: list[str] = []
            j = i + 1
            while True:
                k = text.find('"', j)
                if k < 0:
                    return None
                parts.append(text[j:k])
                if k + 1 < n and text[k + 1] == '"':
                    parts.append('"')
                    j = k + 2
                    continue
                j = k + 1
                break
            comma = text.find(",", j)
            end = n if comma < 0 else comma
            fields.append("".join(parts) + text[j:end])
            i = end
        else:
            comma = text.find(",", i)
            end = n if comma < 0 else comma
            fields.append(text[i:end])
            i = end
        if i >= n:
            break
        i += 1
        if i == n:
            fields.append("")
            break
    return fields
