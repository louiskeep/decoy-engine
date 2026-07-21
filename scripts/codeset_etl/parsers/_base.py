"""Parser result type and interface every codeset source implements."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol


@dataclass(frozen=True)
class ParsedCorpus:
    """A parsed, not-yet-written corpus: rows plus their provenance fields.

    Attributes:
        rows: Row dicts, each with a ``code`` key (string) and whatever
            other columns this source supports (``chapter``, ``description``,
            ...). Not required to be sorted or deduplicated -- that is
            ``_write.write_normalized_corpus``'s job, run once, the same way,
            for every parser.
        source, source_url, license, citation: Attribution fields, same
            shape as ``scripts/build_codesets.py``'s seed-corpus writer.
        source_version, effective_date: The SOURCE's own release identity
            (HC-2 D2a shape) -- NOT the corpus metadata format version
            (``CORPUS_METADATA_VERSION``), which ``_write.py`` stamps
            independently.
    """

    rows: list[dict[str, Any]]
    source: str
    source_url: str
    license: str
    citation: str
    source_version: str
    effective_date: str


class CorpusParser(Protocol):
    """One code-set source: knows its own fetch URL and archive format.

    Deliberately narrow: a parser turns raw bytes into a :class:`ParsedCorpus`
    and states its own sanity floors. It does not fetch (``update.py`` calls
    the injectable fetch function and hands the parser only the bytes -- this
    is what lets tests exercise ``parse_archive`` and the fail-closed floors
    without a real network call) and does not write (``_write.py``).
    """

    #: Corpus name (matches ``CODESET_REGISTRY`` when this source is also a
    #: shipped seed; an ETL-only source not yet promoted to a seed may use a
    #: name outside that registry).
    name: str

    #: HTTPS URL this parser's ``update`` step downloads.
    source_url: str

    #: Fail-closed floor: a download smaller than this is treated as
    #: truncated/short, never parsed. Set well below the real source's
    #: known size so routine size drift (the source adds a few rows) never
    #: false-positives, but a network truncation or an HTML error page
    #: swapped in for the real payload does.
    min_source_bytes: int

    #: Fail-closed floor: a parsed row count below this aborts before
    #: writing (do not silently ship an incomplete universe). Set well
    #: below the real source's known row count for the same reason.
    min_row_count: int

    def parse_archive(self, raw: bytes, *, pulled_on: date) -> ParsedCorpus:
        """Parse *raw* archive bytes into a :class:`ParsedCorpus`.

        Args:
            raw: The full downloaded archive.
            pulled_on: The date this fetch happened, for sources (like NDC)
                whose upstream directory has no single fixed release id --
                ``source_version``/``effective_date`` anchor to the pull
                date instead, explicitly labeled as a snapshot rather than
                an official release identifier.

        Raises:
            CodesetParseError: *raw* is not the expected archive format
                (bad zip, missing member file, unparseable row layout).
        """
        ...
