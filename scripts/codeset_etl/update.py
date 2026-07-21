"""Update command: fetch -> parse -> validate -> write, for one named corpus.

Usage (from the repo root, with the dev venv active)::

    python -m scripts.codeset_etl.update ndc
    python -m scripts.codeset_etl.update ndc --cache-dir /custom/path

This is the explicit, opt-in step the slice-2 spec requires: nothing runs
this automatically, nothing it writes is read by the engine unless a
pipeline's ``provider_config.corpus_source`` is pointed at the written
file (``customer:<cache_dir>/<name>.parquet``) -- see the package docstring
for why that means the shipped-seed default is untouched.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from ._cache import default_cache_dir
from ._errors import CodesetEtlError, CodesetFetchError, CodesetValidationError
from ._fetch import fetch_url
from ._write import write_normalized_corpus
from .parsers import PARSERS

FetchFn = Callable[[str], bytes]


@dataclass(frozen=True)
class UpdateResult:
    """What one ``update_corpus`` call produced."""

    name: str
    path: Path
    row_count: int
    source_version: str
    effective_date: str


def update_corpus(
    name: str,
    *,
    cache_dir: Path | None = None,
    fetch_fn: FetchFn | None = None,
    pulled_on: date | None = None,
) -> UpdateResult:
    """Fetch, parse, validate, and write one corpus by name.

    Args:
        name: A key in ``parsers.PARSERS`` (e.g. "ndc").
        cache_dir: Output directory. Defaults to
            ``_cache.default_cache_dir()``.
        fetch_fn: Injectable network fetch, ``url -> bytes``. Defaults to
            ``_fetch.fetch_url``. Tests MUST pass a fake here -- this is the
            one seam that keeps the unit suite off the real network.
        pulled_on: The date to stamp as this pull's snapshot date. Defaults
            to today (UTC). Tests pass a fixed date for reproducible
            assertions.

    Raises:
        CodesetEtlError: ``name`` is not a known parser, or any stage
            (fetch/parse/validate/write) fails closed. No file is written
            on any failure -- ``write_normalized_corpus`` does not run at
            all unless parsing and its row-count floor both succeed.
    """
    parser = PARSERS.get(name)
    if parser is None:
        raise CodesetEtlError(
            f"no ETL parser registered for corpus {name!r}. Available: {sorted(PARSERS)}."
        )

    fetch = fetch_fn or fetch_url
    resolved_cache_dir = cache_dir or default_cache_dir()
    resolved_pull_date = pulled_on or datetime.now(timezone.utc).date()

    raw = fetch(parser.source_url)
    if len(raw) < parser.min_source_bytes:
        raise CodesetFetchError(
            f"{name}: downloaded {len(raw)} bytes, expected at least "
            f"{parser.min_source_bytes}. Treating this as a truncated/short "
            "download rather than parsing a possibly-incomplete archive."
        )

    parsed = parser.parse_archive(raw, pulled_on=resolved_pull_date)

    if len(parsed.rows) < parser.min_row_count:
        raise CodesetValidationError(
            f"{name}: parsed {len(parsed.rows)} row(s), expected at least "
            f"{parser.min_row_count}. Refusing to write a corpus this far under "
            "the known source size -- likely an upstream format change or a "
            "partial parse, not a legitimate small release."
        )

    out_path = write_normalized_corpus(
        name,
        parsed.rows,
        source=parsed.source,
        source_url=parsed.source_url,
        license=parsed.license,
        citation=parsed.citation,
        source_version=parsed.source_version,
        effective_date=parsed.effective_date,
        out_dir=resolved_cache_dir,
    )

    return UpdateResult(
        name=name,
        path=out_path,
        row_count=len(parsed.rows),
        source_version=parsed.source_version,
        effective_date=parsed.effective_date,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", choices=sorted(PARSERS), help="Corpus name to update.")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Output directory (default: XDG cache dir; see _cache.default_cache_dir).",
    )
    args = parser.parse_args(argv)

    try:
        result = update_corpus(args.name, cache_dir=args.cache_dir)
    except CodesetEtlError as exc:
        print(f"codeset ETL failed for {args.name!r}: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {result.path} ({result.row_count} rows)")
    print(f"  source_version={result.source_version!r} effective_date={result.effective_date!r}")
    print(f"  point provider_config.corpus_source at customer:{result.path} to use it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
