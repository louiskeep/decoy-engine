"""code_set strategy (SP-09 / P5.S.code_set.1/2/3-corpus_source).

Replaces a code column value with a different code drawn from a named corpus
(ICD-10, HCPCS, NDC, MCC, or a customer-supplied file). The strategy is
registered in ``execution._strategies`` as of SP-09b and reachable through
the pandas execution adapter. Two modes:

  Mask mode (mode="mask")
    Deterministically picks a replacement code via HMAC-SHA256 of the input
    value. The candidate set is the full corpus MINUS the input code so that
    output is always different from input (domain-exclusion idiom, same
    principle as FPE in transforms/fpe.py). Candidate rows are sorted by code
    (ascending) at load time; HMAC modulo candidate_count selects one.

  Gen mode (mode="gen")
    Picks one row per-row via ``derive_index`` keyed on the column namespace
    and the row index. Routing through the HKDF+HMAC primitive
    (``determinism._derive``) makes gen-mode columns namespace-bound:
    two columns with different namespaces sharing the same job_seed produce
    decorrelated output sequences. Same namespace + seed + row_index -> same
    code on rerun. Different row indices -> different codes (intra-column
    variation). Covered by SEED_PROTOCOL_VERSION (SP-09c MEDIUM fix).

chapter_preserve (code_set.2)
  When True, restricts the candidate set to corpus rows whose ``chapter``
  value matches the input's chapter. For ICD-10 the chapter is the first
  letter (I21 -> chapter "I"). Fail-closed in two cases:
  - If the input's chapter bucket has only one code (the input itself),
    raises PlanCompileError (sole-member-bucket) because silently returning
    the input would violate the output != input guarantee.
  - If the input's chapter is not present in the corpus at all, raises
    PlanCompileError (code_set_chapter_absent) rather than falling back to
    full-corpus selection, which would silently break the chapter_preserve
    invariant by returning a code from a different chapter.

corpus_source (code_set.3)
  "shipped" (default): loads from ``decoy_engine/codesets/<name>.parquet``.
  "customer:<path>": loads the Parquet file at the given absolute path.
  Customer corpus must have a ``code`` column (string) and, when
  chapter_preserve=True, a ``chapter`` column. A missing ``code`` column or
  an empty corpus raises PlanCompileError (execution-time, pre-mutation,
  fail-closed).

Pattern: HMAC-SHA256-keyed modular selection (RFC 2104,
https://datatracker.ietf.org/doc/html/rfc2104). The same HMAC primitive
used by date_shift, fpe, and joint_mask (SP-08). Gen mode uses
numpy.default_rng for seeded determinism (same as joint_mask SP-08).

SP-06 keyed-access cross-version caveat (inherited from corpus loader):
  Candidate rows are sorted ascending by ``code`` at load time. The HMAC
  digest is reduced modulo candidate_count to select a position in that
  sorted order. This is deterministic WITHIN a corpus version. If rows are
  added or removed (corpus update), candidate_count changes and the same
  input maps to a different output code. Do not assume cross-version output
  stability. See decoy_engine.reference_tables._types.ReferenceTable.keyed_row
  for the general cross-version caveat inherited by this pattern.

Deferred (items remaining after SP-09b handler registration):
  - LOINC, CIP, NUCC, UPC/EAN corpora (P5.S.code_set.3 remainder).
  - CPT/MedDRA bring-your-own documentation.
  - HIPAA pack wiring (P5.PACK.hipaa_tighten, SP-11).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from decoy_engine.determinism import derive_index
from decoy_engine.internal.crypto import hmac_hex
from decoy_engine.plan._errors import PlanCompileError

_LOG = logging.getLogger(__name__)

# Shipped corpora live in this directory.
_CODESETS_DIR = Path(__file__).parent.parent / "codesets"

# Stable salt for HMAC-keyed row derivation. Same purpose as
# reference_tables._KEYED_ACCESS_SALT: determinism, not secrecy.
# RFC 2104: HMAC(key, msg) -- here key = salt, msg = str(input_value).
_KEYED_SALT = b"decoy.code_set.keyed_access.v1"

# Recognised shipped corpus names.  Used for validation only; a name not in
# this set is rejected for the "shipped" source so operators get a clear error
# rather than a FileNotFoundError from deep inside pyarrow.
_SHIPPED_CORPORA = frozenset({"icd10", "hcpcs", "ndc", "mcc"})


@dataclass(frozen=True)
class CodeSetConfig:
    """Configuration for a code_set masking/generation operation.

    Attributes:
        code_set: Corpus name (e.g. "icd10", "hcpcs", "ndc", "mcc") or an
            arbitrary name when corpus_source is a customer path.
        chapter_preserve: When True, restrict replacement candidates to the
            same chapter/category bucket as the input. Requires the corpus to
            have a ``chapter`` column. Defaults to False.
        corpus_source: "shipped" (default) or "customer:<absolute_path>".

    SP-06 keyed-access cross-version caveat:
        Keyed selection is HMAC(...) % candidate_count on the code-sorted
        corpus. Deterministic WITHIN a corpus version. NOT stable across
        corpus versions with different row counts (rows added/removed remaps
        the modular index). Do not assume that a given input maps to the same
        output code after a corpus update.
    """

    code_set: str
    chapter_preserve: bool = False
    corpus_source: str = "shipped"

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> CodeSetConfig:
        """Parse a config dict; raise PlanCompileError on invalid input.

        Structural validation only (code_set name present, corpus_source
        format legal). Corpus loading happens at apply time, pre-mutation,
        so this is a fast compile-time check.

        Args:
            cfg: Config dict with key ``code_set`` (required), plus optional
                ``chapter_preserve`` and ``corpus_source``.

        Raises:
            PlanCompileError: Missing ``code_set``, unrecognised shipped
                corpus name (when corpus_source is "shipped").
        """
        validate_code_set_config(cfg)
        return cls(
            code_set=cfg["code_set"],
            chapter_preserve=bool(cfg.get("chapter_preserve", False)),
            corpus_source=str(cfg.get("corpus_source", "shipped")),
        )


def validate_code_set_config(cfg: dict[str, Any]) -> None:
    """Validate a code_set config dict; raise PlanCompileError on any failure.

    Checks:
      - ``code_set`` is present and non-empty.
      - When ``corpus_source`` is "shipped" (or absent), the name must be a
        known shipped corpus.

    Called at config parse time (fast, no I/O). Corpus loading and deeper
    schema checks happen in :func:`_load_corpus_rows` at apply time,
    pre-mutation, so invalid corpora fail closed before any data is changed.

    Args:
        cfg: Raw config dict.

    Raises:
        PlanCompileError: Any validation failure.
    """
    name = cfg.get("code_set")
    if not name:
        raise PlanCompileError(
            code="code_set_name_missing",
            path="provider_config.code_set",
            message=(
                "'code_set' is required and must name a corpus "
                "(e.g. 'icd10', 'hcpcs', 'ndc', 'mcc', or a customer corpus name "
                "with corpus_source: customer:<path>)."
            ),
        )

    source = str(cfg.get("corpus_source", "shipped"))
    if source == "shipped" and name not in _SHIPPED_CORPORA:
        raise PlanCompileError(
            code="code_set_corpus_not_found",
            path="provider_config.code_set",
            message=(
                f"corpus {name!r} not found in shipped corpora. "
                f"Available: {sorted(_SHIPPED_CORPORA)}. "
                f"To use a custom corpus, set corpus_source: customer:<path>."
            ),
        )


# ── Corpus loading ────────────────────────────────────────────────────────────


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
        List of row dicts with at least a ``code`` key.

    Raises:
        PlanCompileError: File not found, not readable, missing ``code``
            column, or corpus is empty.
    """
    return _load_corpus_rows(name, path)


def _resolve_corpus_path(config: CodeSetConfig) -> tuple[str, Path | None]:
    """Return (corpus_name, optional_override_path) from config."""
    source = config.corpus_source
    if source == "shipped" or not source:
        return config.code_set, None
    if source.startswith("customer:"):
        raw = source[len("customer:") :]
        return config.code_set, Path(raw)
    # Unknown source prefix -- treat as shipped (validated at config time).
    return config.code_set, None


def _load_corpus_rows(name: str, path: Path | None) -> list[dict[str, Any]]:
    """Internal: load and validate a corpus from disk; return sorted row list.

    Validation is execution-time, pre-mutation (fail-closed). No data is
    mutated before this check. Invalid corpora raise PlanCompileError.
    """
    if path is None:
        path = _CODESETS_DIR / f"{name}.parquet"
        if not path.exists():
            raise PlanCompileError(
                code="code_set_corpus_not_found",
                path="provider_config.code_set",
                message=(
                    f"shipped corpus {name!r} not found at {path}. "
                    f"Available: {sorted(_SHIPPED_CORPORA)}."
                ),
            )

    if not path.exists():
        raise PlanCompileError(
            code="code_set_corpus_path_not_found",
            path="provider_config.corpus_source",
            message=f"customer corpus not found at path {path}.",
        )

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
    return rows


# ── Chapter derivation ────────────────────────────────────────────────────────


def _get_chapter(code: str, rows: list[dict[str, Any]]) -> str | None:
    """Look up the chapter for a code from the corpus rows.

    Falls back to the first character of the code when the code is not in
    the corpus (unknown input) and when the corpus has no chapter column.
    Returns None when the chapter cannot be determined.
    """
    if not rows:
        return None

    has_chapter = "chapter" in rows[0]
    if not has_chapter:
        return None

    # Exact code lookup.
    for row in rows:
        if str(row["code"]) == code:
            return str(row["chapter"])

    # Input code not in corpus: derive chapter from first character.
    return code[0] if code else None


# ── Core apply function ───────────────────────────────────────────────────────


def apply_code_set(
    value: str,
    config: CodeSetConfig,
    *,
    mode: str = "mask",
    job_seed: bytes,
    row_index: int = 0,
    namespace: str | None = None,
) -> str:
    """Apply the code_set strategy to a single value.

    Does not mutate ``config``. Returns a real corpus code.

    Validation of the corpus (schema, non-empty) is performed here before any
    selection, so invalid corpora fail closed. This is execution-time,
    pre-mutation: the function does not modify any external state before the
    checks complete.

    Args:
        value: The input code to mask (mask mode) or an ignored hint (gen mode).
        config: Parsed :class:`CodeSetConfig`.
        mode: "mask" (keyed HMAC, output != input) or "gen" (derive_index-keyed).
        job_seed: Entropy input. First 8 bytes are used by gen mode. Same
            seed + same namespace + same row_index -> same output.
        row_index: Zero-based row position. Used by gen mode to vary selection
            per row (intra-column variation). Ignored in mask mode.
        namespace: Column namespace string. Required for gen mode (raises
            PlanCompileError if None); not used by mask mode.

    Returns:
        A real corpus code string.

    Raises:
        PlanCompileError: Corpus not loadable, missing required columns,
            or empty; sole-member chapter bucket (chapter_preserve); missing
            chapter column when chapter_preserve=True; input chapter absent
            from corpus when chapter_preserve=True; namespace is None in gen mode.
        ValueError: Unsupported mode.
    """
    corpus_name, override_path = _resolve_corpus_path(config)
    rows = _load_corpus_rows(corpus_name, override_path)

    if config.chapter_preserve:
        return _apply_chapter_preserve(
            value, rows, mode=mode, job_seed=job_seed, namespace=namespace, row_index=row_index
        )

    if mode == "mask":
        return _pick_mask(value, rows)
    if mode == "gen":
        if namespace is None:
            raise PlanCompileError(
                code="code_set_gen_requires_namespace",
                path="namespace",
                message=(
                    "code_set gen mode requires a column namespace. "
                    "Set namespace on the ColumnSeed or pass namespace= to apply_code_set."
                ),
            )
        return _pick_gen(rows, job_seed=job_seed, namespace=namespace, row_index=row_index)
    raise ValueError(f"code_set: unsupported mode {mode!r}. Use 'mask' or 'gen'.")


def _apply_chapter_preserve(
    value: str,
    rows: list[dict[str, Any]],
    *,
    mode: str,
    job_seed: bytes,
    namespace: str | None = None,
    row_index: int = 0,
) -> str:
    """Apply code_set with chapter_preserve=True.

    Raises PlanCompileError when:
      - The corpus has no 'chapter' column.
      - The input's chapter is not present in the corpus at all (code_set_chapter_absent).
        This is fail-closed: falling back to a different chapter would silently
        break the chapter_preserve invariant, consistent with the sole-member
        bucket posture below.
      - The input's chapter bucket has only the input code (sole-member bucket,
        code_set_sole_member_bucket): no valid alternative exists.
      - mode is "gen" and namespace is None (code_set_gen_requires_namespace).
    """
    if not rows or "chapter" not in rows[0]:
        raise PlanCompileError(
            code="code_set_chapter_column_missing",
            path="provider_config.chapter_preserve",
            message=(
                "chapter_preserve=True requires the corpus to have a 'chapter' column. "
                "The loaded corpus does not have a 'chapter' column."
            ),
        )

    # Determine input's chapter.
    input_chapter = _get_chapter(value, rows)
    if input_chapter is None:
        # Unknown chapter: derive from first char of code.
        input_chapter = value[0] if value else ""

    # Build the bucket: rows in the same chapter.
    bucket = [r for r in rows if str(r.get("chapter", "")) == input_chapter]

    if not bucket:
        # Fail closed: the input's chapter is not present in the corpus.
        # Falling back to full-corpus selection would return a code from a
        # different chapter, silently breaking the chapter_preserve invariant.
        # Consistent posture with the sole-member-bucket raise below.
        raise PlanCompileError(
            code="code_set_chapter_absent",
            path="provider_config.chapter_preserve",
            message=(
                f"chapter_preserve: chapter {input_chapter!r} (derived from input "
                f"{value!r}) is not present in the corpus. Cannot preserve the chapter "
                "invariant: no candidates exist in this chapter. Use a larger corpus "
                "that covers this chapter, or disable chapter_preserve for this field."
            ),
        )

    # Candidate set: bucket MINUS the input code (output != input guarantee).
    candidates = [r for r in bucket if str(r["code"]) != value]

    if not candidates:
        raise PlanCompileError(
            code="code_set_sole_member_bucket",
            path="provider_config.chapter_preserve",
            message=(
                f"chapter_preserve: chapter {input_chapter!r} has only one code "
                f"({value!r}) in the corpus. Cannot produce output != input. "
                f"Use a larger corpus or disable chapter_preserve for this field."
            ),
        )

    if mode == "mask":
        return _pick_from_candidates(value, candidates)
    # Gen mode: draw from candidates (bucket minus input), consistent with the
    # mask path. The input value is not used as a gen hint, but excluding it
    # from the pool keeps the chapter_preserve gen path symmetric with mask.
    if namespace is None:
        raise PlanCompileError(
            code="code_set_gen_requires_namespace",
            path="namespace",
            message=(
                "code_set gen mode requires a column namespace. "
                "Set namespace on the ColumnSeed or pass namespace= to apply_code_set."
            ),
        )
    return _pick_gen(candidates, job_seed=job_seed, namespace=namespace, row_index=row_index)


def _pick_mask(value: str, rows: list[dict[str, Any]]) -> str:
    """HMAC-keyed selection from rows, excluding the input value.

    Builds the candidate set (full corpus minus the input code), then
    picks using HMAC(salt, value) % candidate_count. This guarantees
    output != input regardless of which HMAC index would have landed on
    the input (domain-exclusion idiom, RFC 2104 keying).

    SP-06 cross-version caveat: candidate_count changes if corpus rows are
    added/removed, shifting the modular mapping.
    """
    candidates = [r for r in rows if str(r["code"]) != value]
    if not candidates:
        # Only one code in corpus and it equals the input -- cannot differ.
        raise PlanCompileError(
            code="code_set_single_row_corpus",
            path="provider_config.code_set",
            message=(
                f"Corpus has only one code ({value!r}) and it equals the input. "
                "Mask mode requires at least two distinct codes in the corpus."
            ),
        )
    return _pick_from_candidates(value, candidates)


def _pick_from_candidates(key_value: str, candidates: list[dict[str, Any]]) -> str:
    """Pick one row from candidates using HMAC(salt, key_value) % len(candidates).

    Candidates must be sorted by code (guaranteed by _load_corpus_rows).
    HMAC primitive: RFC 2104 / HMAC-SHA256, same as hmac_hex() in
    decoy_engine.internal.crypto.
    """
    hex_digest = hmac_hex(_KEYED_SALT, key_value)
    if hex_digest is None:
        raise ValueError("hmac_hex returned None for a non-None key_value")
    # First 8 hex chars -> 32-bit int; modulo candidate count.
    idx = int(hex_digest[:8], 16) % len(candidates)
    return str(candidates[idx]["code"])


def _pick_gen(
    rows: list[dict[str, Any]], *, job_seed: bytes, namespace: str, row_index: int = 0
) -> str:
    """Pick one row via ``derive_index`` keyed on namespace and row_index.

    Callers are responsible for validating namespace before calling; this
    function does not check for None (the validate-then-call pattern keeps
    the hot path free of repeated None checks at call sites).

    Deterministic: same job_seed + same namespace + same row_index + same
    corpus -> same code. Decorrelated: two columns with different namespaces
    sharing the same job_seed produce independent sequences because HKDF
    binds the namespace into the key material (SP-09c MEDIUM fix).

    ``job_seed[:8]`` is used so callers may pass the full StrategyContext
    ``job_seed`` (exactly 8 bytes) or a longer test seed without
    re-encoding at call sites.
    """
    idx = derive_index(
        job_seed[:8],
        namespace,
        row_index.to_bytes(8, "big"),
        pool_size=len(rows),
    )
    return str(rows[idx]["code"])
