"""Keyed corpus selection for ``code_set``: mask mode (HMAC with a hole) and gen mode.

Split from ``code_set`` (module-size cap). Mask mode is RFC 2104 HMAC-SHA256 keyed by a
``derive``d sub-key, modulo the candidate count with the input's own row excluded
(``_codeset_index`` hole math); gen mode is ``derive_index`` (HKDF-bound namespace, SP-09c).
"""

from __future__ import annotations

from typing import Any

from decoy_engine import reasons
from decoy_engine.determinism import derive, derive_index
from decoy_engine.internal.crypto import hmac_hex
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms._codeset_index import hole_candidate_count, hole_resolve
from decoy_engine.transforms._codeset_loader import _CorpusRecord

# Stable salt for HMAC-keyed row derivation. Same purpose as
# reference_tables._KEYED_ACCESS_SALT: determinism, not secrecy.
# RFC 2104: HMAC(key, msg) -- here key = salt, msg = str(input_value).
_KEYED_SALT = b"decoy.code_set.keyed_access.v1"


def _pick_mask(value: str, record: _CorpusRecord, *, mask_key: bytes, namespace: str | None) -> str:
    """HMAC-keyed selection from the corpus, excluding the input value.

    code_index locates the input's hole in O(1), then _pick_from_seq picks
    from the corpus minus that hole (output != input, domain-exclusion idiom).
    SP-06 caveat: candidate_count changes if corpus rows are added/removed.
    """
    try:
        rows = record.rows
        position = record.selection.code_index.get(value)
        candidate_count = hole_candidate_count(len(rows), position)
        if candidate_count == 0:
            # Only one code in corpus and it equals the input -- cannot differ.
            raise PlanCompileError(
                code=reasons.plan.CODE_SET_SINGLE_ROW_CORPUS,
                path="provider_config.code_set",
                message=(
                    f"Corpus has only one code ({value!r}) and it equals the input. "
                    "Mask mode requires at least two distinct codes in the corpus."
                ),
            )
        return _pick_from_seq(
            value, rows, position, candidate_count, mask_key=mask_key, namespace=namespace
        )
    except BaseException:
        del mask_key
        raise


def _pick_from_seq(
    key_value: str,
    seq: list[dict[str, Any]],
    position: int | None,
    candidate_count: int,
    *,
    mask_key: bytes,
    namespace: str | None,
) -> str:
    """Pick one row from seq, excluding the row at ``position``, using
    HMAC(key, key_value) % candidate_count.

    ``seq`` sorted by code (guaranteed by _read_corpus_record). ``position``/
    ``candidate_count`` drive the "index with hole" selection (see
    ``_codeset_index``) that reproduces `[r for r in seq if code != value][idx]`
    without materializing the filtered list. HMAC primitive: RFC 2104. DE-02:
    the HMAC key is SECRET-derived (`derive(mask_key, namespace, _KEYED_SALT)`),
    so the remap is not globally reversible (Codex BLOCKER 1); `mask_key ==
    job_seed` on the no-secret path keeps it deterministic.
    """
    try:
        hex_digest = hmac_hex(derive(mask_key, namespace or "code_set", _KEYED_SALT), key_value)
        if hex_digest is None:
            raise ValueError("hmac_hex returned None for a non-None key_value")
        # First 8 hex chars -> 32-bit int, modulo candidate count.
        idx = int(hex_digest[:8], 16) % candidate_count
        return str(seq[hole_resolve(idx, position)]["code"])
    except BaseException:
        del mask_key
        raise


def _pick_gen(
    rows: list[dict[str, Any]],
    *,
    job_seed: bytes,
    namespace: str,
    row_index: int = 0,
    position: int | None = None,
    candidate_count: int | None = None,
) -> str:
    """Pick one row via ``derive_index`` keyed on namespace and row_index.

    Callers validate namespace first (validate-then-call keeps the hot path
    free of repeated None checks). Deterministic: same job_seed + namespace +
    row_index + corpus -> same code. Decorrelated: different namespaces sharing
    a job_seed produce independent sequences (HKDF binds namespace into the key
    material, SP-09c). ``position``/``candidate_count``, when given, exclude the
    row at ``position`` (chapter_preserve gen exclusion, same hole_resolve math
    as mask); omitted, draws from the full pool.
    """
    try:
        pool_size = len(rows) if candidate_count is None else candidate_count
        idx = derive_index(
            job_seed[:8],
            namespace,
            row_index.to_bytes(8, "big"),
            pool_size=pool_size,
        )
        return str(rows[hole_resolve(idx, position)]["code"])
    except BaseException:
        del job_seed
        raise
