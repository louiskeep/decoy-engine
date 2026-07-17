"""Corpus file I/O, caching, and provenance validation for code_set (HC-1 slice 1).

Split out of ``transforms/code_set.py`` to keep that module under its LOC cap
(``CLAUDE.md``: "Use established methodology" + orchestration-module size
discipline). This module owns the low-level "read a Parquet file off disk,
validate it, cache it" concern; ``code_set.py`` owns the strategy-level
concepts (``CodeSetConfig``, HMAC-keyed selection, chapter_preserve) and the
config-aware wrapper (``describe_loaded_corpus``) that needs both.

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

import pyarrow as pa
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
# (resolved_path, mtime_ns, ctime_ns, size) makes a same-path file
# replacement mint a new cache entry automatically, and bounding it as an
# LRU (OrderedDict, move-to-end on hit, evict-oldest on overflow) caps
# worst-case memory for a worker that processes many distinct customer
# corpus files over its lifetime.
#
# Codex P2 CUSTOMER CACHE SAME-MTIME+SAME-SIZE STALENESS remediation:
# mtime+size alone under-identifies a file. A coarse-timestamp filesystem
# (1s or 2s resolution, common on some network/Windows mounts) or tooling
# that explicitly restores the original mtime after writing (rsync
# --times, some backup/deploy tooling) can produce a replacement file that
# keeps the exact same mtime, and a same-length replacement keeps the same
# size -- the old key then silently reuses the stale cached rows. ``ctime``
# (inode change time; POSIX st_ctime, or Windows' "last metadata change" on
# platforms without POSIX ctime semantics) updates on content write AND on
# metadata-only operations like ``rename``/``utime``, closing the common
# case: even a same-path replace that deliberately re-stamps mtime still
# bumps ctime because the re-stamping utime() call is itself a metadata
# change. This is still best-effort file identity, not a content hash --
# hashing every customer corpus (up to ICD-10-CM scale, ~70k rows) on every
# load would defeat the point of caching it.
_CUSTOMER_CACHE_MAX_ENTRIES = 32
_customer_cache: OrderedDict[tuple[str, int, int, int], _CorpusRecord] = OrderedDict()


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


def _get_corpus_record(
    name: str,
    path: Path | None,
    *,
    is_shipped: bool,
    expected_source_version: str | None = None,
) -> _CorpusRecord:
    """Return the cached (or freshly-read) ``_CorpusRecord`` for this corpus.

    MEDIUM-1 remediation (HC-1 slice 1 gap): shipped corpora cache on the
    resolved path alone (bundled + immutable, see ``_shipped_cache``'s module
    comment); customer corpora additionally key on ``(mtime_ns, ctime_ns,
    size)`` so a file replaced at the same path between calls invalidates
    automatically (Codex P2 remediation: ``ctime`` closes the case where a
    same-size replacement is deliberately re-stamped with the original
    mtime -- see ``_customer_cache``'s module comment), in a bounded LRU
    (``_customer_cache``) so the cache cannot grow without bound over a
    long-lived process's lifetime.

    ``expected_source_version`` (HC-2 D2a) is checked on EVERY call, cache
    hit or miss: the pin is a property of the CALLER's config, not of the
    cached bytes on disk, so two callers hitting the SAME cached record with
    different expectations (or none) must each be checked independently
    rather than only at the point the record is first read from disk.
    """
    resolved = _resolve_read_path(name, path)
    resolved_str = str(resolved.resolve())
    if is_shipped:
        cached = _shipped_cache.get(resolved_str)
        if cached is not None:
            record = cached
        else:
            record = _read_corpus_record(name, resolved, is_shipped=True)
            _shipped_cache[resolved_str] = record
        _check_source_version_pin(name, resolved, record, expected_source_version)
        return record

    # A customer corpus can disappear or become unreadable between
    # `_resolve_read_path`'s existence check and this stat (TOCTOU). Translate
    # the bare OSError to the loader's typed error contract (load_corpus
    # documents PlanCompileError for missing/unreadable corpora) instead of
    # leaking FileNotFoundError/PermissionError.
    try:
        stat = resolved.stat()
    except FileNotFoundError as exc:
        raise PlanCompileError(
            code="code_set_corpus_path_not_found",
            path="provider_config.corpus_source",
            message=f"customer corpus at {resolved} became unavailable after the existence check.",
        ) from exc
    except OSError as exc:
        raise PlanCompileError(
            code="code_set_corpus_read_error",
            path="provider_config.corpus_source",
            message=f"failed to stat customer corpus at {resolved}: {exc}",
        ) from exc
    # Best-effort file identity, not a content hash (see the module-level
    # comment on `_customer_cache` for why ctime is included and a hash is
    # not).
    cache_key = (resolved_str, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    cached = _customer_cache.get(cache_key)
    if cached is not None:
        _customer_cache.move_to_end(cache_key)  # LRU: mark as most recently used.
        record = cached
    else:
        record = _read_corpus_record(name, resolved, is_shipped=False)
        _customer_cache[cache_key] = record
        if len(_customer_cache) > _CUSTOMER_CACHE_MAX_ENTRIES:
            _customer_cache.popitem(last=False)  # evict the least-recently-used entry.
    _check_source_version_pin(name, resolved, record, expected_source_version)
    return record


def _check_source_version_pin(
    name: str,
    path: Path | None,
    record: _CorpusRecord,
    expected_source_version: str | None,
) -> None:
    """Fail closed when a config-pinned corpus release does not match (HC-2 D2a).

    ``corpus_source_version`` (``CodeSetConfig``) is the plan author's
    declared expectation of the corpus's SOURCE release id (e.g. "FY2024"),
    independently verified here against the release actually embedded in the
    loaded file's provenance (``CodeSetProvenance.source_version``). This is
    distinct from ``corpus_version`` / ``CORPUS_METADATA_VERSION``, which is
    the metadata FORMAT, not the source's release. Runs for BOTH shipped and
    customer corpora: a shipped corpus update and a customer swapping in a
    different release must fail exactly the same way. Unset (None) is a
    no-op -- today's unpinned behavior is unchanged.
    """
    if expected_source_version is None:
        return
    actual = record.provenance.source_version if record.provenance else ""
    if actual != expected_source_version:
        at = f" at {path}" if path is not None else ""
        raise PlanCompileError(
            code="code_set_corpus_version_mismatch",
            path="provider_config.corpus_source_version",
            message=(
                f"corpus {name!r}{at}: corpus_source_version pins "
                f"{expected_source_version!r}, but the loaded corpus's embedded "
                f"source_version is {actual!r}. The corpus at this name/path has "
                "changed since the plan was authored (or never carried the pinned "
                "release). Update corpus_source_version to match the corpus you "
                "intend to use, or remove the pin to accept whatever is loaded."
            ),
        )


def _check_corpus_schema(tbl: pa.Table, path: Path) -> None:
    """Generic, corpus-agnostic schema invariants (HC-2 D2c).

    Shared by the load path (:func:`_read_corpus_record`) and the standalone
    :func:`verify_corpus` primitive, so a corpus can never pass one and fail
    the other -- ONE checker, two callers. Deliberately corpus-agnostic: a
    ``code`` column that is present, non-null, non-empty, and unique, plus
    (only when the column exists) a ``chapter`` column that is populated for
    every row. Code-system-specific regexes and a mandatory ``description``
    column are deferred to HC-1 slice 2, when the real full corpora (each
    with its own per-system format conventions) land.
    """
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

    code_col = tbl.column("code")
    if code_col.null_count > 0:
        raise PlanCompileError(
            code="code_set_corpus_null_code",
            path="provider_config.corpus_source",
            message=(
                f"corpus at {path} has {code_col.null_count} null value(s) in the "
                "'code' column. Every row must carry a code."
            ),
        )

    # str() every value up front: codes are treated as strings everywhere
    # downstream (sorting, HMAC keying), so duplicate/empty detection must
    # compare on the same representation, not the raw Arrow-decoded type.
    codes = [str(c) for c in code_col.to_pylist()]

    empty = sum(1 for c in codes if c.strip() == "")
    if empty:
        raise PlanCompileError(
            code="code_set_corpus_empty_code",
            path="provider_config.corpus_source",
            message=(
                f"corpus at {path} has {empty} empty-string value(s) in the 'code' "
                "column. Every code must be a non-empty string."
            ),
        )

    duplicates = len(codes) - len(set(codes))
    if duplicates:
        raise PlanCompileError(
            code="code_set_corpus_duplicate_codes",
            path="provider_config.corpus_source",
            message=(
                f"corpus at {path} has {duplicates} duplicate code value(s). code_set "
                "requires unique codes: HMAC-keyed candidate selection assumes a 1:1 "
                "code -> row mapping, and a duplicate makes selection ambiguous."
            ),
        )

    if "chapter" in tbl.schema.names:
        chapters = tbl.column("chapter").to_pylist()
        incoherent = sum(1 for c in chapters if c is None or str(c).strip() == "")
        if incoherent:
            raise PlanCompileError(
                code="code_set_corpus_incoherent_chapter",
                path="provider_config.corpus_source",
                message=(
                    f"corpus at {path} has {incoherent} row(s) with a null/empty "
                    "'chapter' value. A corpus that has a 'chapter' column must "
                    "populate it for every row (chapter_preserve assumes full coverage)."
                ),
            )


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

    _check_corpus_schema(tbl, path)

    # Build row dicts and sort by code for stable HMAC-keyed access.
    columns = tbl.schema.names
    rows: list[dict[str, Any]] = []
    for i in range(tbl.num_rows):
        row = {col: tbl.column(col)[i].as_py() for col in columns}
        rows.append(row)

    rows.sort(key=lambda r: str(r["code"]))

    # HC-1 slice 1: memoized code -> chapter dict, built once here instead of
    # linear-scanned per _get_chapter call. Codes are unique by invariant at
    # this point -- HC-2's `_check_corpus_schema` (run above, before this block)
    # rejects a duplicate-code corpus outright -- so the `setdefault` is now
    # just defensive: there is at most one row per code, no resolution to make.
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
        # Codex round-5 P2 SHIPPED PROVENANCE IDENTITY UNVERIFIED
        # remediation: a complete provenance stamp is not proof the stamp
        # belongs to THIS corpus. A packaging/metadata mistake (or a
        # deliberate swap) can ship `icd10.parquet` carrying `mcc`'s
        # embedded `decoy_corpus` metadata; the completeness check above
        # passes either way, so `describe_loaded_corpus` would report
        # `code_set: icd10` with MCC's attribution as if it were genuine.
        # Shipped-only: a customer corpus's provenance is optional and may
        # legitimately name a different embedded id (e.g. a customer's own
        # code_set built from a shipped seed), so this identity check does
        # not apply to the customer path.
        if is_shipped and provenance.corpus != name:
            raise PlanCompileError(
                code="code_set_corpus_provenance_identity_mismatch",
                path="provider_config.code_set",
                message=(
                    f"shipped corpus {name!r} at {path} carries provenance "
                    f"for a different corpus ({provenance.corpus!r}). This "
                    "looks like a packaging or metadata swap; the embedded "
                    "provenance.corpus must match the requested corpus name."
                ),
            )
        # Codex round-7 P2 remediation: the required-field check above does
        # not cover is_seed or the metadata format version, so a shipped
        # corpus with all four required fields but an absent/garbled is_seed
        # (silently coerced to False -> evidence reports a seed as a full
        # corpus) or a stale corpus_version slipped through. Shipped corpora
        # are our own build artifacts; those are packaging defects, fail
        # closed. Customer corpora are exempt (they may omit is_seed and never
        # carry our corpus_version).
        if is_shipped:
            stamp_defects = provenance.shipped_stamp_defects()
            if stamp_defects:
                raise PlanCompileError(
                    code="code_set_corpus_provenance_malformed_stamp",
                    path="provider_config.code_set",
                    message=(
                        f"shipped corpus {name!r} at {path} has a malformed "
                        f"provenance stamp ({'; '.join(stamp_defects)}). A "
                        "shipped corpus is a build artifact: is_seed and the "
                        "metadata format version must be explicit and current "
                        "so evidence cannot misreport a seed as a full corpus."
                    ),
                )
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


# ── Standalone verification primitive (HC-2 item 1) ──────────────────────────


@dataclass(frozen=True)
class CorpusVerifyReport:
    """Structured result of validating a corpus Parquet file (HC-2).

    Attributes:
        ok: True when the corpus passed every check.
        path: The verified file's path, as given (for display; not resolved).
        row_count: Row count on success. Always 0 on failure (``ok=False``):
            validation raises at the first failed check, before a row count is
            returned, so a partially-read corpus reports 0 rather than a
            possibly-misleading partial count.
        provenance: A counts/identifiers-only provenance summary -- the same
            shape ``describe_loaded_corpus`` stamps into evidence, never raw
            codes. ``None`` when the corpus carries no provenance stamp, or
            failed before provenance could be read.
        problems: Coded, human-readable failure descriptions
            (``"<PlanCompileError.code>: <message>"``). Empty when ``ok``.
    """

    ok: bool
    path: str
    row_count: int
    provenance: dict[str, Any] | None
    problems: tuple[str, ...] = ()


def verify_corpus(path: Path) -> CorpusVerifyReport:
    """Validate a corpus Parquet file without running a masking job.

    Runs the EXACT same schema check (:func:`_check_corpus_schema`) and
    provenance validation (:func:`_validate_provenance`) the load path runs
    in :func:`_read_corpus_record` -- the single validation source of truth
    the CLI ``codesets verify``/``add`` and the platform upload check call
    (both live in sibling repos; this is the primitive they call into).
    Treats the file as a CUSTOMER corpus (``is_shipped=False``): a file under
    review for upload has no registered shipped name to identity-check
    against, and the shipped-only strictness (identity match, ``is_seed``,
    metadata format version) does not apply to it.

    Unlike :func:`_read_corpus_record`, this NEVER raises
    :class:`PlanCompileError` -- that is the point of a standalone,
    non-job-fatal check: every coded failure is caught and translated into
    ``problems`` instead of propagating. Does not use the module cache (a
    corpus under review is not yet a job's corpus).

    Args:
        path: Path to the corpus Parquet file to verify. A ``str`` is accepted
            and coerced (the "never raises" contract must hold for a path-like
            string too, not only a ``Path``).

    Returns:
        A :class:`CorpusVerifyReport`. ``ok=False`` on any failure (unreadable
        file, missing/empty/duplicate/null code column, incomplete
        provenance, ...); ``problems`` names exactly what failed.
    """
    path = Path(path)
    name = path.stem
    try:
        record = _read_corpus_record(name, path, is_shipped=False)
    except PlanCompileError as exc:
        return CorpusVerifyReport(
            ok=False,
            path=str(path),
            row_count=0,
            provenance=None,
            problems=(f"{exc.code}: {exc.message}",),
        )

    prov = record.provenance
    summary = (
        {
            "source": prov.source,
            "source_version": prov.source_version,
            "effective_date": prov.effective_date,
            "license": prov.license,
            "is_seed": prov.is_seed,
        }
        if prov is not None
        else None
    )
    return CorpusVerifyReport(
        ok=True,
        path=str(path),
        row_count=len(record.rows),
        provenance=summary,
        problems=(),
    )
