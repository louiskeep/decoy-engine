"""Shared vocabulary of the chunked FK self-masking gate.

Which strategies a chunk can self-mask without the rest of the table
(`CHUNK_SAFE_STRATEGIES`), which of those need a namespace or are
dtype-invariant, and the coarse dtype family (`_dtype_family`) under which
equal FK keys mask to equal bytes. The compile-time edge checks
(`_chunked_fk_edge_checks.py`), the per-chunk declared-dtype guard
(`_chunked_fk_dtype.py`) and `_chunked.py` all read these; `_chunked_fk.py`
re-exports them so existing imports keep working. See
docs/relationships-memory-scaling.md §2 for the design.
"""

from __future__ import annotations

import re

# Matches a decimal dtype string that carries explicit (precision, scale), in
# either PyArrow's own `str()` form (`str(pa.decimal128(2, 1))` ==
# "decimal128(2, 1)", `str(pa.decimal256(40, 2))` == "decimal256(40, 2)" --
# confirmed empirically) or the SQL-style form an operator might hand-write in
# config ("decimal(10, 2)" / "numeric(10, 2)", no width suffix). Captures
# (precision, scale) so two decimal declarations can be compared on that pair,
# not folded into one bare "decimal" family regardless of scale.
# Sentinel family for an UNPROVABLE decimal declaration (bare `decimal`/`numeric`
# with no precision+scale). The per-chunk runtime guard fails closed on any
# column whose DECLARED family is this sentinel: decimal scale changes the
# canonical bytes and the chunked route cannot verify parent/child scale
# agreement, so a bare decimal FK key cannot preserve RI.
_DECIMAL_UNPROVABLE_FAMILY = "decimal:unprovable"
# Scale may be NEGATIVE (Arrow/Parquet-legal, e.g. `decimal128(4, -1)`), so the
# scale group accepts an optional leading minus; precision is always >= 1.
# Covers every PyArrow decimal width (decimal32/64/128/256, PyArrow 24+) plus a
# SQL-style hand-written `decimal(P, S)`/`numeric(P, S)`. Only the SCALE (group 2,
# may be negative) is used for the RI family; precision (group 1) is captured but
# not folded in (see `_dtype_family`).
_DECIMAL_PRECISION_SCALE_RE = re.compile(
    r"^(?:decimal(?:32|64|128|256)?|numeric)\((\d+)\s*,\s*(-?\d+)\)$"
)

# Value-keyed strategies that produce the same output per (seed, namespace, value)
# regardless of row position or chunk boundary. Defined here and re-exported by
# _chunked.py so that the chunked-FK gate and the general compatibility check share
# one authoritative set without a circular import.
CHUNK_SAFE_STRATEGIES: frozenset[str] = frozenset(
    {
        "hash",
        "fpe",
        "redact",
        "truncate",
        "text_redact",
        "date_shift",
        "bucketize",
        "top_code",
        "passthrough",
        # Phase 4 slice 3 (2026-09-01): value-own-keyed per-cell deterministic
        # span masking; NOT namespace-requiring (below).
        "text_mask",
    }
)

# Subset of CHUNK_SAFE_STRATEGIES whose handlers call derive(job_seed, namespace, ...)
# and raise at execution when plan.namespace is None. Each raises a typed error code
# when the namespace is absent:
#   hash       -- hash_requires_namespace
#   fpe        -- fpe_requires_namespace
#   date_shift -- date_shift_requires_namespace
# The remaining CHUNK_SAFE strategies (redact, truncate, text_redact, bucketize,
# top_code, passthrough, text_mask) do not read plan.namespace and produce
# byte-identical output regardless of whether a namespace is declared; the
# namespace sub-checks in the FK gate are skipped for them. text_mask keys its
# spans on ctx.mask_key globally (RFC 2104 HMAC per span), not a per-column
# namespace, so it belongs in this group despite its richer dispatch.
NAMESPACE_REQUIRING_STRATEGIES: frozenset[str] = frozenset({"hash", "fpe", "date_shift"})

# Strategies whose masked output does NOT depend on the key's value (only on
# its null-ness), so parent and child provably mask to identical values for the
# same logical key REGARDLESS of dtype. `redact` emits a constant replacement
# for every non-null input (condition (e) already forces an identical
# `redact_with` on both sides); a null stays null on both. Every OTHER
# chunk-safe strategy is value-sensitive: its output depends on the raw key's
# representation, which differs across dtype families for equal logical keys
# (int 1 vs float 1.0 stringify/canonicalize/FPE to different bytes), so it can
# only be proven safe when both sides declare the same dtype family.
DTYPE_INVARIANT_STRATEGIES: frozenset[str] = frozenset({"redact"})


def _dtype_family(dtype: str) -> str:
    """Coarse dtype family for the chunked-FK dtype gate (condition (f)).

    Widths within a family (int32 vs int64) reproduce identical masked bytes
    for equal values -- the kernel canonicalizer encodes any-width integers
    as a length-prefixed signed two's complement form (see `_encode_int`; the
    width is `(bit_length+8)//8`, not strictly minimal) regardless of storage
    width (kernel/_canonicalize.py) -- so only the family needs to agree, not
    the exact dtype string. Unrecognized strings pass through lowercased and
    unmodified, so an unknown dtype only ever matches another occurrence of
    that exact same string.

    `decimal`/`numeric` is SCALE-AWARE (Codex gpt-5.6-sol HIGH-1, 2026-07-14):
    unlike int/float, a decimal's canonical bytes depend on its declared
    SCALE, not just its family -- `str(1.0)` for `decimal128(2, 1)` and
    `str(1.00)` for `decimal128(3, 2)` canonicalize to different byte strings
    for the "same" logical value under the hash/truncate/fpe kernels, so two
    decimal columns are only provably interchangeable when they share the same
    SCALE. Precision is IRRELEVANT to RI: the canonicalizer encodes by
    (unscaled_int, scale), so `decimal128(2, 1)` and `decimal128(3, 1)` mask an
    equal key to identical bytes -- folding precision in would over-reject a
    healthy scale-matched / precision-mismatched FK pair. A dtype string that
    carries explicit precision+scale (any PyArrow width `str()` form
    `decimal{32,64,128,256}(P, S)`, or the SQL-style "decimal(P, S)"/
    "numeric(P, S)" an operator might hand-write) parses to the scale-keyed
    family `"decimal(scale=S)"`, so a scale-matched declaration on both sides of
    an FK edge is admitted (regardless of precision or width), and a genuine
    scale MISmatch (parent scale 1 vs child scale 2) is rejected at the COMPILE
    gate the same way an int/float mismatch is (different family strings).

    A BARE `"decimal"` or `"numeric"` declaration (no scale) is UNPROVABLE --
    the chunked route only ever sees one table's dtype at a time, so there is
    no runtime check that can recover the missing scale -- and returns the
    distinct sentinel family `_DECIMAL_UNPROVABLE_FAMILY`. Two bare declarations
    on both sides of an edge compare EQUAL at the compile gate (same sentinel)
    and are admitted there; the rejection happens one layer down, at the
    per-chunk runtime guard
    (`_chunked_fk_dtype.reject_mismatched_chunked_fk_declared_dtype`), which
    fails closed on ANY column whose DECLARED family is the sentinel -- it does
    NOT depend on the real data's family, so it holds even for the negative-scale
    real decimals that the scale regex now parses concretely. Real Arrow decimals
    (any precision/scale, including negative scale) resolve to a concrete
    `"decimal(P,S)"` family, so a correctly-scaled declaration matches its real
    data and is admitted, while a genuine scale mismatch is rejected.
    """
    family = dtype.strip().lower()
    if family.startswith(("int", "uint")):
        return "int"
    if family.startswith(("float", "double")):
        return "float"
    if family.startswith(("decimal", "numeric")):
        match = _DECIMAL_PRECISION_SCALE_RE.match(family)
        if match is None:
            return _DECIMAL_UNPROVABLE_FAMILY
        # RI keys on SCALE ONLY: the canonicalizer encodes a decimal by its
        # (unscaled_int, scale), so two decimals of the same scale produce
        # identical masked bytes for equal logical keys regardless of precision
        # (verified: decimal128(2,1) and decimal128(3,1) mask 1.0 to the same
        # hash). Precision only bounds range, so folding it in would over-reject
        # a healthy scale-matched / precision-mismatched FK pair.
        scale = match.group(2)
        return f"decimal(scale={scale})"
    if family.startswith("bool"):
        return "bool"
    if family.startswith(("str", "string", "object", "utf8", "large_string")):
        return "string"
    # date vs timestamp/datetime are DISTINCT families: date32 and timestamp
    # canonicalize to different bytes for the same instant, so a declared-date /
    # real-timestamp FK key voids RI. Check timestamp/datetime BEFORE date --
    # "datetime" is a prefix-superset of "date".
    if family.startswith(("timestamp", "datetime")):
        return "timestamp"
    if family.startswith("date"):
        return "date"
    if family.startswith(("bytes", "binary", "large_binary", "fixed_size_binary")):
        return "bytes"
    return family
