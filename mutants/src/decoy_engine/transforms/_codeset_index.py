"""Selection-index precompute + index-with-hole arithmetic for code_set (HC-1 slice 2).

Split out of ``transforms/_codeset_loader.py`` and ``transforms/code_set.py`` to
keep both under their module-size caps (``tests/sentry/test_module_size.py``).
This module owns the "how do we select a replacement code in O(1) per masked
value" concern; the loader owns file I/O + caching, and ``code_set.py`` owns the
strategy-level HMAC/derive_index selection that consumes these indices.

Three list comprehensions in ``code_set.py`` used to run PER masked VALUE
(``_pick_mask``'s full-corpus exclusion filter, and ``_apply_chapter_preserve``'s
bucket filter plus its own exclusion filter), making masking a column
O(rows_masked x corpus_size) -- fine at seed scale, a wall at ICD-10-CM's
~74,719 rows. :func:`build_selection_indexes` precomputes, in a single pass over
the already-sorted rows, everything those filters recomputed; the pure
:func:`hole_candidate_count` / :func:`hole_resolve` pair then reproduce
``[r for r in seq if code != value][idx]`` without materializing the filtered
list, so selection is O(1) per value with byte-identical output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SelectionIndexes:
    """The precomputed selection indices for one loaded corpus (HC-1 slice 2).

    Built once per cache key by :func:`build_selection_indexes` and stored on
    ``_codeset_loader._CorpusRecord``. See that dataclass for how each field is
    consumed by ``code_set``'s selection.

    ``code_index``
      ``code -> position in rows``. Locates the input value's "hole" in the
      full corpus for mask mode's output-!=-input exclusion.
    ``chapter_index``
      ``code -> chapter``. O(1) chapter lookup (``code_set._get_chapter``),
      replacing the pre-HC-1 O(n) scan. ``None`` when the corpus has no
      ``chapter`` column (chapter_preserve is then unavailable).
    ``chapter_buckets``
      ``chapter -> the sorted sublist of rows in that chapter``, in the same
      order the pre-perf ``[r for r in rows if r["chapter"] == chapter]``
      filter produced (rows are already sorted ascending by code, and this is
      built by appending in that same order). ``None`` under the same
      condition as ``chapter_index``.
    ``bucket_code_index``
      ``code -> position within that code's OWN chapter bucket`` (not a
      position in ``rows``). Every code belongs to exactly one chapter, so
      this is a flat dict rather than one dict per bucket. ``None`` under the
      same condition as ``chapter_index``.
    """

    code_index: dict[str, int]
    chapter_index: dict[str, str] | None
    chapter_buckets: dict[str, list[dict[str, Any]]] | None
    bucket_code_index: dict[str, int] | None


def build_selection_indexes(rows: list[dict[str, Any]]) -> SelectionIndexes:
    """Precompute all selection indices in one pass over the sorted *rows*.

    ``rows`` must already be sorted ascending by ``code`` (the loader sorts
    before calling this) so ``chapter_buckets`` preserves the exact order the
    old per-value filter produced. Codes are unique by invariant at this point
    -- ``_check_corpus_schema`` rejects a duplicate-code corpus outright -- so
    the ``setdefault`` calls are defensive: at most one row per code, no
    resolution to make.
    """
    has_chapter = bool(rows and "chapter" in rows[0])
    # Built as plain (non-Optional) dicts regardless of has_chapter, so the
    # loop body never needs a None-narrowing check; only the returned fields
    # collapse the chapter-less case to None (matching chapter_index's
    # None-when-no-chapter-column contract).
    code_index: dict[str, int] = {}
    chapter_index: dict[str, str] = {}
    chapter_buckets: dict[str, list[dict[str, Any]]] = {}
    bucket_code_index: dict[str, int] = {}

    for i, r in enumerate(rows):
        code = str(r["code"])
        code_index.setdefault(code, i)
        if has_chapter:
            chapter = str(r["chapter"])
            chapter_index.setdefault(code, chapter)
            bucket = chapter_buckets.setdefault(chapter, [])
            bucket_code_index.setdefault(code, len(bucket))
            bucket.append(r)

    return SelectionIndexes(
        code_index=code_index,
        chapter_index=chapter_index if has_chapter else None,
        chapter_buckets=chapter_buckets if has_chapter else None,
        bucket_code_index=bucket_code_index if has_chapter else None,
    )


def hole_candidate_count(seq_len: int, position: int | None) -> int:
    """Candidate count when excluding the row at ``position`` from a run of
    length ``seq_len``.

    Mirrors ``len([r for r in seq if r is not seq[position]])`` without
    materializing the filtered list. ``position=None`` means "nothing to
    exclude" (the input value is not a member of ``seq``), so every row is a
    candidate. Paired with :func:`hole_resolve`, this is the "index with
    hole" selection code_set.py uses in place of a per-call O(n) filter.
    """
    return seq_len if position is None else seq_len - 1


def hole_resolve(idx: int, position: int | None) -> int:
    """Map a 0-based index into the virtual "seq minus the hole at
    ``position``" sequence back to a real index into ``seq``.

    Reproduces what ``[r for r in seq if r is not seq[position]][idx]``'s
    ``idx`` corresponds to in ``seq`` directly: below the hole, the index is
    unchanged; at or past it, the removed slot shifts every later index up by
    one, so add it back.
    """
    if position is None or idx < position:
        return idx
    return idx + 1
