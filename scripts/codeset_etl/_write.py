"""Shared normalize -> validate -> write pipeline for ETL-produced corpora.

The ONE place every parser's output becomes a Parquet file on disk, so a
future fifth parser (HCPCS, MS-DRG, ...) gets the determinism/provenance
contract for free instead of re-implementing it. Mirrors
``scripts/build_codesets.py::_write`` (the seed-corpus writer) in shape,
generalized to accept a variable row schema and to target the ETL cache dir
instead of ``src/decoy_engine/codesets/``.

Imports ``CORPUS_METADATA_VERSION`` and ``REQUIRED_PROVENANCE_FIELDS`` from
``decoy_engine.transforms._codeset_provenance`` rather than re-declaring
them, so this pipeline cannot silently drift from what
``_codeset_loader._validate_provenance`` actually checks at load time.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.transforms._codeset_provenance import (
    CORPUS_METADATA_VERSION,
    REQUIRED_PROVENANCE_FIELDS,
)

from ._errors import CodesetValidationError


def write_normalized_corpus(
    name: str,
    rows: list[dict[str, Any]],
    *,
    source: str,
    source_url: str,
    license: str,
    citation: str,
    source_version: str,
    effective_date: str,
    out_dir: Path,
) -> Path:
    """Validate, sort, and atomically write a normalized corpus Parquet file.

    Args:
        name: Corpus name (e.g. "ndc"); also the output filename stem and
            the embedded ``decoy_corpus`` provenance identity.
        rows: Row dicts. Every row MUST have a ``code`` key; every row must
            share the exact same set of keys (a variable schema across rows
            would silently null-pad in Arrow, which the loader's
            per-column non-null checks -- e.g. ``chapter`` -- assume cannot
            happen). At least "code"; "chapter" and "description" are the
            other columns the shipped seed corpora use, but not required.
        source, source_url, license, citation: Attribution fields. Must all
            be non-empty (this function is the identity-marked producer of
            REQUIRED_PROVENANCE_FIELDS below, not just source_version /
            effective_date -- a blank attribution string would round-trip
            through the loader's provenance-completeness check as either a
            silent PASS on the customer path, which is not detectable
            downstream, so reject it here at the source instead).
        source_version, effective_date: Release identifiers (HC-2 D2a shape).
        out_dir: Directory to write ``<name>.parquet`` into (created if
            absent). Pass the ETL cache dir (``_cache.default_cache_dir``),
            never ``src/decoy_engine/codesets/`` -- writing there would
            silently change the SHIPPED default, which slice 2 must not do.

    Returns:
        The path written.

    Raises:
        CodesetValidationError: Empty ``rows``; a row missing ``code``; an
            empty/duplicate code; rows with inconsistent column sets; any
            required attribution/provenance field blank.
    """
    if not rows:
        raise CodesetValidationError(f"{name}: refusing to write an empty corpus (0 rows).")

    for field_name, value in (
        ("source", source),
        ("source_url", source_url),
        ("license", license),
        ("citation", citation),
        ("source_version", source_version),
        ("effective_date", effective_date),
    ):
        if not value or not value.strip():
            raise CodesetValidationError(
                f"{name}: provenance field {field_name!r} is blank. Every field in "
                f"REQUIRED_PROVENANCE_FIELDS {REQUIRED_PROVENANCE_FIELDS} (plus "
                "source_url/citation) must be a real value -- the loader fails "
                "closed on an incomplete stamp, and a blank value written here "
                "would only surface as that failure much later, at masking time."
            )

    columns = set(rows[0].keys())
    if "code" not in columns:
        raise CodesetValidationError(f"{name}: rows are missing the required 'code' key.")
    for i, row in enumerate(rows):
        if set(row.keys()) != columns:
            raise CodesetValidationError(
                f"{name}: row {i} has columns {sorted(row.keys())}, expected "
                f"{sorted(columns)}. Every row must share the same column set "
                "(a ragged schema would silently null-pad in Arrow)."
            )

    codes = [str(row["code"]) for row in rows]
    empty = sum(1 for c in codes if not c.strip())
    if empty:
        raise CodesetValidationError(f"{name}: {empty} row(s) have an empty 'code' value.")
    duplicates = len(codes) - len(set(codes))
    if duplicates:
        raise CodesetValidationError(
            f"{name}: {duplicates} duplicate code value(s) after normalization. "
            "code_set selection assumes a 1:1 code -> row mapping (see "
            "_codeset_loader._check_corpus_schema); the parser's dedup pass has a bug."
        )

    if "chapter" in columns:
        incoherent = sum(
            1 for row in rows if row.get("chapter") is None or str(row["chapter"]).strip() == ""
        )
        if incoherent:
            raise CodesetValidationError(
                f"{name}: {incoherent} row(s) have a null/empty 'chapter' value. A "
                "corpus with a 'chapter' column must populate it for every row."
            )

    # CS.1-CS.9 determinism contract: candidate ordering for HMAC-keyed
    # selection is ascending-by-code. The loader re-sorts on every load
    # regardless (defense in depth), but the ETL output is sorted here too
    # so the ON-DISK file itself already satisfies the contract -- a repo
    # inspection or a diff of the corpus file does not need to trust the
    # loader to know the ordering is right.
    sorted_rows = sorted(rows, key=lambda r: str(r["code"]))

    ordered_columns = sorted(columns)
    table = pa.table(
        {
            col: pa.array([str(row[col]) for row in sorted_rows], type=pa.string())
            for col in ordered_columns
        },
        metadata={
            k.encode(): v.encode()
            for k, v in {
                "decoy_corpus": name,
                "decoy_corpus_version": CORPUS_METADATA_VERSION,
                "source": source,
                "source_url": source_url,
                "license": license,
                "citation": citation,
                "source_version": source_version,
                "effective_date": effective_date,
                # HC-1 slice 2 output is always a full pull from the live
                # source, never the abbreviated seed -- is_seed is hardcoded
                # false here rather than threaded as a parameter so no
                # caller of this ETL pipeline can accidentally mislabel a
                # real pull as a seed (or vice versa).
                "is_seed": "false",
            }.items()
        },
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{name}.parquet"
    # Atomic write: build the file at a temp path in the SAME directory (so
    # the final os.replace is a same-filesystem rename, not a copy) and only
    # replace the real path once the write has fully succeeded. A crash or
    # kill mid pq.write_table then leaves the old corpus (or nothing) at
    # out_path, never a truncated/partial one -- "fail-closed ETL" applies
    # to the write step too, not just fetch/parse.
    tmp_path = out_dir / f".{name}.parquet.tmp"
    pq.write_table(table, str(tmp_path), compression="snappy")
    os.replace(tmp_path, out_path)
    return out_path
