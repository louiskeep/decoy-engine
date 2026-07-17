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
from collections import OrderedDict
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

    Built once per cache key and cached in ``_shipped_cache`` / ``_customer_cache``
    (module-level, HC-1 slice 1). ``chapter_index`` is a ``code -> chapter`` dict built
    once at load time so ``code_set._get_chapter`` is O(1) per lookup instead
    of an O(n) scan per call -- the fix that makes an ICD-10-CM-scale (~70k
    row) corpus viable, since every masked VALUE used to re-scan the whole
    corpus. ``None`` when the corpus has no ``chapter`` column
    (chapter_preserve is then unavailable, same as before HC-1).
    """

    rows: list[dict[str, Any]]
    provenance: CodeSetProvenance | None
    chapter_index: dict[str, str] | None


# Memoized corpus loads. A corpus is read from disk, validated,
# provenance-checked, and chapter-indexed at MOST ONCE per cache key; every
# subsequent apply_code_set call for the same corpus reuses the cached
# record. Without this, every masked VALUE would re-read and re-sort the
# whole Parquet file (fine at 65 rows, a disaster at ICD-10-CM's ~70k).
#
# MEDIUM-1 remediation (HC-1 slice 1 gap): split into two caches with
# different invalidation/eviction policies, because SHIPPED and CUSTOMER
# corpora have different lifetimes.
#
# Shipped corpora are bundled package files, immutable for the life of a
# running process, so a simple resolved-path key is correct AND self-bounds
# in size (CODESET_REGISTRY is a small, fixed set -- 4 corpora today) with
# no eviction needed.
_shipped_cache: dict[str, _CorpusRecord] = {}

# Customer corpora are operator-supplied files at a path the engine does not
# own; the pre-HC-1 code re-read one from disk on every call, so a file
# REPLACED at the same path between jobs was always picked up. Caching by
# path alone (as HC-1 slice 1 originally shipped) silently broke that: a
# replaced file would be served the STALE pre-replacement rows forever
# (correctness), and the cache also grew one entry per distinct path ever
# seen with no eviction (memory, in a long-lived platform worker). Keying on
# (resolved_path, mtime_ns, size) makes a same-path file replacement mint a
# new cache entry automatically (a modified file almost never keeps the same
# mtime+size, and a job that races a mid-run file swap either way was never
# consistent), and bounding it as an LRU (OrderedDict, move-to-end on hit,
# evict-oldest on overflow) caps worst-case memory for a worker that
# processes many distinct customer corpus files over its lifetime.
_CUSTOMER_CACHE_MAX_ENTRIES = 32
_customer_cache: OrderedDict[tuple[str, int, int], _CorpusRecord] = OrderedDict()


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
        List of row dicts with at least a ``code`` key. A fresh list AND
        fresh row dicts per call (NIT-2 remediation: neither the returned
        list nor its row dicts are the cached objects), so mutating the
        returned rows -- the list, or an individual row's fields -- never
        corrupts the shared cache record another caller reads next.

    Raises:
        PlanCompileError: File not found, not readable, missing ``code``
            column, corpus is empty, or (HC-1) a shipped corpus is missing
            required provenance metadata (``code_set_corpus_missing_
            provenance``).
    """
    record = _get_corpus_record(name, path, is_shipped=path is None)
    return [dict(row) for row in record.rows]


def load_corpus_provenance(name: str, path: Path | None = None) -> CodeSetProvenance | None:
    """Return the parsed provenance for a corpus (HC-1 slice 1).

    Shares the module cache with :func:`load_corpus`, so calling this before
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
    """Return the cached (or freshly-read) ``_CorpusRecord`` for this corpus.

    MEDIUM-1 remediation (HC-1 slice 1 gap): shipped corpora cache on the
    resolved path alone (bundled + immutable, see ``_shipped_cache``'s module
    comment); customer corpora additionally key on ``(mtime_ns, size)`` so a
    file replaced at the same path between calls invalidates automatically,
    in a bounded LRU (``_customer_cache``) so the cache cannot grow without
    bound over a long-lived process's lifetime.
    """
    resolved = _resolve_read_path(name, path)
    resolved_str = str(resolved.resolve())
    if is_shipped:
        cached = _shipped_cache.get(resolved_str)
        if cached is not None:
            return cached
        record = _read_corpus_record(name, resolved, is_shipped=True)
        _shipped_cache[resolved_str] = record
        return record

    stat = resolved.stat()
    cache_key = (resolved_str, stat.st_mtime_ns, stat.st_size)
    cached = _customer_cache.get(cache_key)
    if cached is not None:
        _customer_cache.move_to_end(cache_key)  # LRU: mark as most recently used.
        return cached
    record = _read_corpus_record(name, resolved, is_shipped=False)
    _customer_cache[cache_key] = record
    if len(_customer_cache) > _CUSTOMER_CACHE_MAX_ENTRIES:
        _customer_cache.popitem(last=False)  # evict the least-recently-used entry.
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
    # corpus (true of every shipped corpus). LOW-1 remediation: a duplicate
    # code in a customer corpus resolves FIRST-WINS in code-sorted order
    # (`setdefault`, not a dict-comprehension which is last-write-wins) to
    # match the pre-HC-1 linear scan's `for row in rows: if match: return`,
    # which stopped at the first hit -- byte-identical to the old behavior,
    # not merely equivalent in the common no-duplicate case.
    chapter_index: dict[str, str] | None = None
    if rows and "chapter" in rows[0]:
        chapter_index = {}
        for r in rows:
            chapter_index.setdefault(str(r["code"]), str(r["chapter"]))

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
