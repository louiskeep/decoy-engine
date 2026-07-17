"""Corpus file I/O, caching, and provenance validation for code_set (HC-1 slice 1).

Split out of ``transforms/code_set.py`` to keep that module under its LOC cap
(``CLAUDE.md``: "Use established methodology" + orchestration-module size
discipline). This module owns the low-level "read a Parquet file off disk,
validate it, cache it" concern; ``code_set.py`` owns the strategy-level
concepts (``CodeSetConfig``, HMAC-keyed selection, chapter_preserve) and the
config-aware wrappers (``corpus_provenance_for_manifest``,
``describe_loaded_corpus``) that need both.

Pattern: Parquet key/value metadata carries provenance (see
``transforms/_codeset_provenance.py`` for the ``CodeSetProvenance`` type and
its ``ModelPackManifest``-imitating shape).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms._codeset_provenance import CODESET_REGISTRY, CodeSetProvenance

_LOG = logging.getLogger(__name__)

# Shipped corpora live in this directory.
_CODESETS_DIR = Path(__file__).parent.parent / "codesets"

# Recognised shipped corpus names, derived from the single-source-of-truth
# registry (HC-1 slice 1 item 5; transforms/_codeset_provenance.py).
_SHIPPED_CORPORA = frozenset(CODESET_REGISTRY)


@dataclass(frozen=True)
class _CorpusRecord:
    """A loaded, validated corpus: rows, provenance, and a memoized chapter index.

    Built once per resolved file path and cached in ``_corpus_cache`` (module-
    level, HC-1 slice 1). ``chapter_index`` is a ``code -> chapter`` dict built
    once at load time so ``code_set._get_chapter`` is O(1) per lookup instead
    of an O(n) scan per call -- the fix that makes an ICD-10-CM-scale (~70k
    row) corpus viable, since every masked VALUE used to re-scan the whole
    corpus. ``None`` when the corpus has no ``chapter`` column
    (chapter_preserve is then unavailable, same as before HC-1).
    """

    rows: list[dict[str, Any]]
    provenance: CodeSetProvenance | None
    chapter_index: dict[str, str] | None


# Memoized corpus loads, keyed by the resolved corpus file path (str). A
# corpus is read from disk, validated, provenance-checked, and chapter-
# indexed at MOST ONCE per process; every subsequent apply_code_set call for
# the same path reuses the cached record. Without this, every masked VALUE
# would re-read and re-sort the whole Parquet file (fine at 65 rows, a
# disaster at ICD-10-CM's ~70k).
_corpus_cache: dict[str, _CorpusRecord] = {}


def load_corpus(name: str, path: Path | None = None) -> list[dict[str, Any]]:
    """Load a corpus Parquet file and return rows as a sorted list of dicts.

    Rows are sorted ascending by ``code`` to establish a stable, file-order-
    independent ordering for HMAC-keyed access (same principle as the
    ReferenceTable.keyed_row id-sort in SP-06).

    Args:
        name: Corpus name (e.g. "icd10"). Used only for error messages when
            path is provided by the caller.
        path: Override path. When None, loads from the shipped codesets dir.

    Returns:
        List of row dicts with at least a ``code`` key. A fresh list object
        per call (the cached rows are not returned by reference), so
        mutating the returned list never corrupts the shared cache.

    Raises:
        PlanCompileError: File not found, not readable, missing ``code``
            column, corpus is empty, or (HC-1) a shipped corpus is missing
            required provenance metadata (``code_set_corpus_missing_
            provenance``).
    """
    record = _get_corpus_record(name, path, is_shipped=path is None)
    return list(record.rows)


def load_corpus_provenance(name: str, path: Path | None = None) -> CodeSetProvenance | None:
    """Return the parsed provenance for a corpus (HC-1 slice 1).

    Shares ``_corpus_cache`` with :func:`load_corpus`, so calling this before
    or after ``load_corpus`` for the same corpus costs no extra I/O. Same
    fail-closed (shipped) / warn (customer) validation as corpus loading in
    general -- see :func:`_validate_provenance`.

    Args:
        name: Corpus name (e.g. "icd10").
        path: Override path. When None, loads from the shipped codesets dir.

    Returns:
        The corpus's ``CodeSetProvenance``, or ``None`` if the corpus (a
        customer corpus only -- a shipped corpus without provenance raises)
        has no provenance metadata at all.
    """
    record = _get_corpus_record(name, path, is_shipped=path is None)
    return record.provenance


def _resolve_read_path(name: str, path: Path | None) -> Path:
    """Resolve the corpus path to read.

    Preserves the pre-HC-1 error semantics exactly: a missing SHIPPED corpus
    raises ``code_set_corpus_not_found``; a missing CUSTOMER path raises
    ``code_set_corpus_path_not_found``. Called on every access (a cheap
    ``Path.exists()`` stat), independent of the cache, so a corpus deleted
    between calls is still caught -- only the expensive read+parse+sort is
    memoized.
    """
    if path is None:
        shipped = _CODESETS_DIR / f"{name}.parquet"
        if not shipped.exists():
            raise PlanCompileError(
                code="code_set_corpus_not_found",
                path="provider_config.code_set",
                message=(
                    f"shipped corpus {name!r} not found at {shipped}. "
                    f"Available: {sorted(_SHIPPED_CORPORA)}."
                ),
            )
        return shipped

    if not path.exists():
        raise PlanCompileError(
            code="code_set_corpus_path_not_found",
            path="provider_config.corpus_source",
            message=f"customer corpus not found at path {path}.",
        )
    return path


def _get_corpus_record(name: str, path: Path | None, *, is_shipped: bool) -> _CorpusRecord:
    """Return the cached (or freshly-read) ``_CorpusRecord`` for this corpus."""
    resolved = _resolve_read_path(name, path)
    cache_key = str(resolved.resolve())
    cached = _corpus_cache.get(cache_key)
    if cached is not None:
        return cached
    record = _read_corpus_record(name, resolved, is_shipped=is_shipped)
    _corpus_cache[cache_key] = record
    return record


def _read_corpus_record(name: str, path: Path, *, is_shipped: bool) -> _CorpusRecord:
    """Internal: read, validate, and index a corpus from disk (uncached).

    Validation is execution-time, pre-mutation (fail-closed). No data is
    mutated before this check. Invalid corpora raise PlanCompileError.
    """
    try:
        tbl = pq.read_table(str(path))  # type: ignore[no-untyped-call, unused-ignore]
    except Exception as exc:
        raise PlanCompileError(
            code="code_set_corpus_read_error",
            path="provider_config.corpus_source",
            message=f"failed to read corpus Parquet at {path}: {exc}",
        ) from exc

    if "code" not in tbl.schema.names:
        raise PlanCompileError(
            code="code_set_corpus_missing_code_column",
            path="provider_config.corpus_source",
            message=(
                f"corpus at {path} is missing required 'code' column. "
                f"Customer corpora must have a 'code' (string) column. "
                f"Available columns: {tbl.schema.names}"
            ),
        )

    if tbl.num_rows == 0:
        raise PlanCompileError(
            code="code_set_corpus_empty",
            path="provider_config.corpus_source",
            message=f"corpus at {path} has 0 rows. Corpus must be non-empty.",
        )

    # Build row dicts and sort by code for stable HMAC-keyed access.
    columns = tbl.schema.names
    rows: list[dict[str, Any]] = []
    for i in range(tbl.num_rows):
        row = {col: tbl.column(col)[i].as_py() for col in columns}
        rows.append(row)

    rows.sort(key=lambda r: str(r["code"]))

    # HC-1 slice 1: memoized code -> chapter dict, built once here instead of
    # linear-scanned per _get_chapter call. Assumes codes are unique within a
    # corpus (true of every shipped corpus; a duplicate code in a customer
    # corpus resolves to its LAST occurrence in code-sorted order, same
    # ambiguity a linear "first match" scan would have had for an unordered
    # customer file anyway).
    chapter_index: dict[str, str] | None = None
    if rows and "chapter" in rows[0]:
        chapter_index = {str(r["code"]): str(r["chapter"]) for r in rows}

    provenance = CodeSetProvenance.from_parquet_metadata(tbl)
    _validate_provenance(name, path, provenance, is_shipped=is_shipped)

    return _CorpusRecord(rows=rows, provenance=provenance, chapter_index=chapter_index)


def _validate_provenance(
    name: str, path: Path, provenance: CodeSetProvenance | None, *, is_shipped: bool
) -> None:
    """Fail-closed for shipped corpora; warn for customer corpora (HC-1 slice 1).

    A shipped corpus without a complete provenance stamp (source,
    source_version, effective_date, license) is a packaging defect: the
    engine ships it, so it must be able to say where the codes came from.
    A customer corpus may legitimately have no provenance metadata at all
    (it is not required for the strategy to function); if the operator did
    stamp it, the stamp must be complete -- a half-filled provenance block
    is worse than none, since it looks authoritative but is not.
    """
    if provenance is None:
        if is_shipped:
            raise PlanCompileError(
                code="code_set_corpus_missing_provenance",
                path="provider_config.code_set",
                message=(
                    f"shipped corpus {name!r} at {path} has no provenance metadata "
                    "(source, source_version, effective_date, license). This is a "
                    "packaging defect, not a customer-corpus configuration issue."
                ),
            )
        _LOG.warning(
            "customer code_set corpus %r at %s has no provenance metadata "
            "(source, source_version, effective_date, license). Provenance is "
            "optional for customer corpora but recommended for audit evidence.",
            name,
            path,
        )
        return

    missing = provenance.missing_required_fields()
    if not missing:
        return
    if is_shipped:
        raise PlanCompileError(
            code="code_set_corpus_missing_provenance",
            path="provider_config.code_set",
            message=(
                f"shipped corpus {name!r} at {path} has incomplete provenance "
                f"metadata (missing: {', '.join(missing)})."
            ),
        )
    raise PlanCompileError(
        code="code_set_corpus_missing_provenance",
        path="provider_config.corpus_source",
        message=(
            f"customer corpus {name!r} at {path} has partial provenance metadata "
            f"(missing: {', '.join(missing)}). Either remove the partial stamp or "
            "complete it; a half-filled provenance block is not surfaced as evidence."
        ),
    )
