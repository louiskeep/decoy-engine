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

Provenance + scale (HC-1 slice 1, 2026-07-17)
  Every corpus load is validated and cached once per cache key (module-level
  ``_shipped_cache`` for bundled corpora, keyed on resolved path; a bounded
  LRU ``_customer_cache`` for operator-supplied corpora, keyed on resolved
  path + mtime + ctime + size so a file replaced at the same path
  invalidates automatically instead of serving stale rows -- see
  ``_codeset_loader.py``'s module docstring), including a memoized
  ``code -> chapter`` dict built at that same load point -- ``_get_chapter``
  is an O(1) lookup instead of the pre-HC-1 O(n) scan, which is what makes an
  ICD-10-CM-scale (~70k row) corpus viable. Each load also reads the corpus's Parquet
  key/value metadata into a typed ``CodeSetProvenance`` record (see
  ``transforms/_codeset_provenance.py``): a SHIPPED corpus missing required
  provenance fields (source, source_version, effective_date, license) fails
  closed (``code_set_corpus_missing_provenance``, job-fatal); a CUSTOMER
  corpus without provenance only warns (it may legitimately have none), but
  a partial stamp on a customer corpus still fails closed -- see
  ``_validate_provenance``. Provenance is surfaced as evidence ONLY, via
  ``describe_loaded_corpus`` (``ExecutionResult.quality_metrics
  ['code_set_corpora']``, counts + identifiers only, no raw codes); it is
  deliberately never written into the Plan YAML -- a code corpus is data,
  not reproducible plan config (Codex P1 PROVENANCE IS EVIDENCE, NOT PLAN
  STATE remediation).

Pinned-record masking/evidence parity (Codex round-6 P2 MASKING/EVIDENCE
VERSION DIVERGENCE remediation, 2026-07-17)
  The customer cache invalidates on (path, mtime_ns, ctime_ns, size), which is
  exactly right for "don't serve stale rows forever" but means two
  INDEPENDENT cache lookups made moments apart -- one to stamp evidence, one
  per value to mask -- can each resolve a DIFFERENT ``_CorpusRecord`` if the
  file is replaced on disk in between. ``resolve_corpus_record`` is the one
  place a caller that needs BOTH resolves the record; ``describe_loaded_
  corpus`` and ``apply_code_set`` both accept that SAME resolved record via an
  optional parameter so a caller (``CodeSetHandler.run``, the out-of-core
  per-table code_set path) pins it once and threads it through, closing the
  divergence surface entirely rather than re-deriving "are these the same
  version" after the fact. Callers that only need one or the other (tests,
  external code) keep working unchanged -- omitting the parameter resolves
  fresh, exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from decoy_engine.determinism import derive, derive_index
from decoy_engine.internal.crypto import hmac_hex
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms._codeset_loader import (
    _SHIPPED_CORPORA,
    _CorpusRecord,
    _get_corpus_record,
    load_corpus,  # noqa: F401 -- re-exported public API (see below)
    load_corpus_provenance,  # noqa: F401 -- re-exported public API (see below)
)

# Stable salt for HMAC-keyed row derivation. Same purpose as
# reference_tables._KEYED_ACCESS_SALT: determinism, not secrecy.
# RFC 2104: HMAC(key, msg) -- here key = salt, msg = str(input_value).
_KEYED_SALT = b"decoy.code_set.keyed_access.v1"

# `load_corpus` / `load_corpus_provenance` / `_get_corpus_record` and the
# `_SHIPPED_CORPORA` name set live in `transforms/_codeset_loader.py` (split
# out to keep this module under its LOC cap; see that module's docstring).
# Imported above and used/re-exported here unchanged -- `from
# decoy_engine.transforms.code_set import load_corpus` keeps working exactly
# as before the split.


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
    schema checks happen in :func:`_read_corpus_record` at apply time,
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
#
# The file-I/O / caching / provenance-validation machinery
# (``_CorpusRecord``, ``_shipped_cache``, ``_customer_cache``, ``_get_corpus_record``,
# ``load_corpus``, ``load_corpus_provenance``) lives in ``transforms/_codeset_loader.py`` and is
# imported above. What stays here is the one CONFIG-aware wrapper below
# (it needs ``CodeSetConfig``, which would create a circular import if the
# loader depended on it) plus ``_resolve_corpus_path``, which it shares with
# the execution-time apply path.
#
# Codex P1 PROVENANCE IS EVIDENCE, NOT PLAN STATE remediation: this module
# used to also export ``corpus_provenance_for_manifest``, a best-effort
# lookup ``plan/_serialize.py`` called to stamp ``code_set_provenance`` onto
# the Plan YAML. That made the plan artifact non-deterministic (a
# swapped/absent corpus silently changed or dropped the block) and the field
# never round-tripped. Deleted; provenance is surfaced ONLY as execution
# evidence, via ``describe_loaded_corpus`` below
# (``ExecutionResult.quality_metrics['code_set_corpora']``), from the corpus
# actually loaded at run time.


def resolve_corpus_record(config: CodeSetConfig) -> _CorpusRecord:
    """Resolve *config* to its ``_CorpusRecord``, ONE cache lookup.

    Codex round-6 P2 MASKING/EVIDENCE VERSION DIVERGENCE remediation: a
    caller that needs BOTH the evidence summary and per-value masking for the
    same run must call this exactly once and thread the returned record into
    ``describe_loaded_corpus(config, record=...)`` and ``apply_code_set(...,
    corpus_record=...)`` so both draw from the SAME corpus version even if
    the underlying customer file is replaced mid-run -- see the module
    docstring's "Pinned-record masking/evidence parity" section.
    """
    corpus_name, override_path = _resolve_corpus_path(config)
    return _get_corpus_record(corpus_name, override_path, is_shipped=override_path is None)


def describe_loaded_corpus(
    config: CodeSetConfig, *, record: _CorpusRecord | None = None
) -> dict[str, Any]:
    """Return a quality-evidence summary for the corpus *config* resolves to.

    Counts and identifiers only -- NO raw codes -- following the
    SubsetManifest no-raw-data contract. Used by
    ``execution._strategies._code_set.CodeSetHandler`` to stamp
    ``ExecutionResult.quality_metrics['code_set_corpora']`` once per column
    (HC-1 slice 1's evidence-surfacing requirement). Loads (and validates /
    fail-closes) the corpus exactly like :func:`apply_code_set`; shares the
    module cache, so this costs no extra I/O once the column starts masking.

    ``record``, when given, is used AS-IS instead of resolving a fresh one
    (Codex round-6 P2 remediation) -- pass the SAME record returned by
    :func:`resolve_corpus_record` that a subsequent :func:`apply_code_set`
    call also pins, so evidence and masking can never disagree about which
    corpus version was used. Omit it to resolve fresh, as before.
    """
    if record is None:
        record = resolve_corpus_record(config)
    prov = record.provenance
    return {
        "code_set": config.code_set,
        "source": prov.source if prov else "",
        "source_version": prov.source_version if prov else "",
        "effective_date": prov.effective_date if prov else "",
        "license": prov.license if prov else "",
        "is_seed": prov.is_seed if prov else False,
        "row_count": len(record.rows),
    }


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


# ── Chapter derivation ────────────────────────────────────────────────────────


def _get_chapter(code: str, chapter_index: dict[str, str] | None) -> str | None:
    """Look up the chapter for a code via the memoized code->chapter dict.

    Falls back to the first character of the code when the code is not in
    the corpus (unknown input). Returns None when the corpus has no chapter
    column at all (``chapter_index`` is None).

    HC-1 slice 1: O(1) dict lookup, replacing the pre-HC-1 O(n) linear scan
    over every corpus row per call (see ``_CorpusRecord.chapter_index``).
    """
    if chapter_index is None:
        return None

    if code in chapter_index:
        return chapter_index[code]

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
    corpus_record: _CorpusRecord | None = None,
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
        corpus_record: Codex round-6 P2 remediation -- when given, this exact
            ``_CorpusRecord`` is used instead of resolving one from the cache.
            Pass the record :func:`resolve_corpus_record` returned so a caller
            that masks many values off one pinned load (e.g.
            ``CodeSetHandler.run``) cannot have a later value pick up a
            DIFFERENT corpus version than an earlier one (or than the
            evidence stamp) if the underlying file is replaced mid-run.
            Omit it to resolve fresh, as before.

    Returns:
        A real corpus code string.

    Raises:
        PlanCompileError: Corpus not loadable, missing required columns,
            or empty; sole-member chapter bucket (chapter_preserve); missing
            chapter column when chapter_preserve=True; input chapter absent
            from corpus when chapter_preserve=True; namespace is None in gen mode.
        ValueError: Unsupported mode.
    """
    if corpus_record is None:
        corpus_name, override_path = _resolve_corpus_path(config)
        record = _get_corpus_record(corpus_name, override_path, is_shipped=override_path is None)
    else:
        record = corpus_record
    rows = record.rows

    if config.chapter_preserve:
        return _apply_chapter_preserve(
            value,
            rows,
            record.chapter_index,
            mode=mode,
            job_seed=job_seed,
            namespace=namespace,
            row_index=row_index,
        )

    if mode == "mask":
        return _pick_mask(value, rows, mask_key=job_seed, namespace=namespace)
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
    chapter_index: dict[str, str] | None,
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

    # Determine input's chapter. HC-1 slice 1: O(1) dict lookup via the
    # memoized chapter_index built once at corpus load time.
    input_chapter = _get_chapter(value, chapter_index)
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
        return _pick_from_candidates(value, candidates, mask_key=job_seed, namespace=namespace)
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


def _pick_mask(
    value: str, rows: list[dict[str, Any]], *, mask_key: bytes, namespace: str | None
) -> str:
    """HMAC-keyed selection from rows, excluding the input value.

    Builds the candidate set (full corpus minus the input code), then
    picks using a SECRET-derived HMAC key (DE-02) % candidate_count. This
    guarantees output != input regardless of which HMAC index would have landed
    on the input (domain-exclusion idiom, RFC 2104 keying).

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
    return _pick_from_candidates(value, candidates, mask_key=mask_key, namespace=namespace)


def _pick_from_candidates(
    key_value: str,
    candidates: list[dict[str, Any]],
    *,
    mask_key: bytes,
    namespace: str | None,
) -> str:
    """Pick one row from candidates using HMAC(key, key_value) % len(candidates).

    Candidates must be sorted by code (guaranteed by _read_corpus_record).
    HMAC primitive: RFC 2104 / HMAC-SHA256, same as hmac_hex() in
    decoy_engine.internal.crypto.

    DE-02: the HMAC key is SECRET-derived -- `derive(mask_key, namespace,
    _KEYED_SALT)` where `_KEYED_SALT = b"decoy.code_set.keyed_access.v1"` is used
    as the derive() source (no longer as the raw HMAC key). So the code -> code
    remap depends on the run's keyed-mask secret and is not globally reversible
    (Codex BLOCKER 1 / HIGH). `mask_key == job_seed` on the no-secret path, so the
    mapping stays deterministic per (seed, column).
    """
    hmac_key = derive(mask_key, namespace or "code_set", _KEYED_SALT)
    hex_digest = hmac_hex(hmac_key, key_value)
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
