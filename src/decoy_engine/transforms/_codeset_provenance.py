"""Code-corpus provenance types and the shipped-corpus registry (HC-1 slice 1).

Pattern: Parquet key/value metadata (``pyarrow.Table.schema.metadata``) carries
the provenance block, imitating
:class:`decoy_engine.storm.model_pack.types.ModelPackManifest` -- a small,
frozen, JSON-serializable manifest dataclass with a typed constructor and a
dict-export method. ``ModelPackManifest.from_dict`` reads a manifest.json
dict; :meth:`CodeSetProvenance.from_parquet_metadata` plays the same role for
the metadata dict :func:`scripts.build_codesets._write` embeds directly into
the corpus Parquet file (no sidecar manifest needed; Parquet already carries
key/value metadata natively).

``CODESET_REGISTRY`` is the single source of truth for shipped corpus names
(HC-1 slice 1 item 5). ``transforms.code_set._SHIPPED_CORPORA`` and
``TestShippedCorpora`` (tests/unit/transforms/test_code_set.py) both derive
from it; ``codesets/__init__.py``'s docstring and ``docs/strategies.md``'s
corpus list are checked against it by a drift-guard test so an added corpus
cannot silently go undocumented.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pyarrow as pa

#: Provenance fields every SHIPPED corpus must carry (fail-closed; see
#: transforms/code_set.py's provenance validation at load time). A customer
#: corpus may omit provenance entirely (warn, not fail), but if it supplies
#: any, the same fields are required -- a half-filled provenance block is
#: worse than none, since it looks authoritative but is not.
REQUIRED_PROVENANCE_FIELDS: tuple[str, ...] = (
    "source",
    "source_version",
    "effective_date",
    "license",
)

#: Corpus metadata format version embedded by scripts/build_codesets.py.
#: Bumped 1.0 -> 2.0 for the HC-1 slice-1 schema (source_version,
#: effective_date, is_seed added to the metadata block). Format change,
#: pre-GA, allowed (see decoy_engine.release.is_pre_ga).
CORPUS_METADATA_VERSION = "2.0"

#: The known Parquet schema-metadata keys `CodeSetProvenance.from_parquet_metadata`
#: reads (see `scripts/build_codesets.py::_write`, the writer side). Codex P2
#: PROVENANCE METADATA DECODE CRASH remediation: PyArrow schema metadata is
#: byte-valued and may legally carry opaque binary bytes on keys unrelated to
#: provenance, so `from_parquet_metadata` decodes ONLY these known keys as
#: UTF-8 instead of every key in the metadata dict.
_PROVENANCE_METADATA_KEYS: tuple[str, ...] = (
    "decoy_corpus",
    "decoy_corpus_version",
    "source",
    "source_url",
    "license",
    "citation",
    "source_version",
    "effective_date",
    "is_seed",
)


@dataclass(frozen=True)
class CodeSetProvenance:
    """Provenance stamp for one code_set corpus, read from Parquet metadata.

    All fields are plain strings (plus one bool) so the record is directly
    JSON-serializable for evidence surfacing (``ExecutionResult.quality_metrics
    ['code_set_corpora']``) and Plan-YAML stamping
    (``plan/_serialize.py::_column_seed_to_dict``).

    Attributes:
        corpus: The ``decoy_corpus`` metadata key (corpus name, e.g. "icd10").
        corpus_version: The ``decoy_corpus_version`` metadata format version
            (see CORPUS_METADATA_VERSION), not the source's own release
            version -- that is ``source_version`` below.
        source: Human-readable attribution (e.g. "CMS ICD-10-CM FY2024").
        source_url: Canonical URL for the source, when one exists.
        license: License / public-domain statement.
        citation: Full bibliographic citation.
        source_version: The SOURCE's own release identifier (e.g. "FY2024",
            "Q1 2024", "ISO 18245:2003"). Distinct from corpus_version.
        effective_date: ISO date string (YYYY-MM-DD) the source release took
            effect. Best-effort when the upstream source has no single
            fixed release date (documented per-corpus in build_codesets.py).
        is_seed: True when this file is an abbreviated sample of the full
            public code set (HC-1 slice 1 ships seeds; slice 2 replaces them
            with full corpora). Lets downstream evidence distinguish a seed
            from a complete corpus without inspecting row counts.
        raw_is_seed: The EXACT decoded ``is_seed`` metadata string, or None
            when the key was absent. ``is_seed`` above collapses this to a
            bool (anything != "true" becomes False), which is the right
            evidence shape but LOSES the distinction between an explicit
            "false", an absent key, and a garbled value like "yes". Shipped-
            corpus validation (:meth:`shipped_stamp_defects`) needs that
            distinction so a build artifact whose seed status is unknown fails
            closed instead of silently masquerading as a full corpus
            (is_seed=False). Not surfaced in :meth:`to_json_dict` -- evidence
            keeps the clean bool.
    """

    corpus: str
    corpus_version: str
    source: str
    source_url: str
    license: str
    citation: str
    source_version: str
    effective_date: str
    is_seed: bool = False
    raw_is_seed: str | None = None

    @classmethod
    def from_parquet_metadata(cls, tbl: pa.Table) -> CodeSetProvenance | None:
        """Parse provenance from a loaded corpus table's schema metadata.

        Returns ``None`` when the table carries no metadata at all, or
        metadata that does not include the ``decoy_corpus`` marker key (i.e.
        no attempt at a provenance stamp was made -- the common case for an
        ad-hoc customer corpus). A present-but-incomplete stamp (marker key
        present, one or more required fields empty) still returns a record;
        callers use :meth:`missing_required_fields` to decide fail-closed
        vs. warn.

        Codex P2 PROVENANCE METADATA DECODE CRASH remediation: only the known
        provenance keys (`_PROVENANCE_METADATA_KEYS`) are decoded as UTF-8.
        PyArrow schema metadata is byte-valued and may legally carry opaque
        binary bytes on keys this corpus never intended as provenance (e.g. a
        customer's own tooling stamped an unrelated binary key); decoding
        every key unconditionally raised ``UnicodeDecodeError`` on such an
        otherwise-valid corpus, crashing instead of following the documented
        optional-provenance (warn, not fail) path. A key whose bytes fail to
        decode is treated as absent rather than propagating the decode error,
        since a corrupted single provenance field should degrade to the same
        "no provenance" / warn path as a missing field, not crash.
        """
        raw = tbl.schema.metadata
        if not raw:
            return None
        decoded: dict[str, str] = {}
        for name in _PROVENANCE_METADATA_KEYS:
            raw_value = raw.get(name.encode("utf-8"), raw.get(name))
            if raw_value is None:
                continue
            try:
                value = (
                    raw_value.decode("utf-8") if isinstance(raw_value, bytes) else str(raw_value)
                )
            except UnicodeDecodeError:
                continue
            decoded[name] = value
        if "decoy_corpus" not in decoded:
            return None
        return cls(
            corpus=decoded.get("decoy_corpus", ""),
            corpus_version=decoded.get("decoy_corpus_version", ""),
            source=decoded.get("source", ""),
            source_url=decoded.get("source_url", ""),
            license=decoded.get("license", ""),
            citation=decoded.get("citation", ""),
            source_version=decoded.get("source_version", ""),
            effective_date=decoded.get("effective_date", ""),
            is_seed=(decoded.get("is_seed") or "").strip().lower() == "true",
            raw_is_seed=decoded.get("is_seed"),
        )

    def missing_required_fields(self) -> list[str]:
        """Return the subset of REQUIRED_PROVENANCE_FIELDS that are empty."""
        values = {
            "source": self.source,
            "source_version": self.source_version,
            "effective_date": self.effective_date,
            "license": self.license,
        }
        return [name for name in REQUIRED_PROVENANCE_FIELDS if not values[name]]

    def shipped_stamp_defects(self) -> list[str]:
        """SHIPPED-only strictness beyond REQUIRED_PROVENANCE_FIELDS (HC-1).

        Codex round-7 P2 remediation: ``is_seed`` and ``corpus_version`` are
        not in REQUIRED_PROVENANCE_FIELDS, so a shipped corpus whose metadata
        omits ``is_seed`` (or supplies a non-boolean like "yes") passed
        validation while :meth:`from_parquet_metadata` silently coerced
        ``is_seed`` to False -- evidence then reported an unknown seed status
        as a full corpus. Likewise a shipped file stamped with a stale
        metadata-format ``corpus_version`` was accepted. A shipped corpus is
        the engine's OWN build artifact (scripts/build_codesets.py), so an
        absent/garbled seed flag or a wrong format version is a packaging
        defect that must fail closed, not degrade to a misleading default.
        Returns the list of defects (empty == well-formed). Shipped-only: a
        customer corpus may legitimately omit ``is_seed`` (it defaults False,
        surfaced as-is) and never carries our ``corpus_version``.
        """
        defects: list[str] = []
        if self.corpus_version != CORPUS_METADATA_VERSION:
            defects.append(
                f"corpus_version {self.corpus_version!r} != expected {CORPUS_METADATA_VERSION!r}"
            )
        if self.raw_is_seed is None:
            defects.append("is_seed metadata key is absent")
        elif self.raw_is_seed.strip().lower() not in ("true", "false"):
            defects.append(f"is_seed {self.raw_is_seed!r} is not a boolean (true/false)")
        return defects

    def to_json_dict(self) -> dict[str, Any]:
        """Return a plain JSON-serializable dict (evidence + Plan-YAML shape)."""
        return {
            "corpus": self.corpus,
            "corpus_version": self.corpus_version,
            "source": self.source,
            "source_url": self.source_url,
            "license": self.license,
            "citation": self.citation,
            "source_version": self.source_version,
            "effective_date": self.effective_date,
            "is_seed": self.is_seed,
        }


@dataclass(frozen=True)
class CodeSetSpec:
    """One shipped-corpus registry entry.

    Deliberately minimal: the registry's job is to name the shipped corpora
    (the compile-time gate and the test parametrization), not to duplicate
    provenance data that already lives in each corpus file's own Parquet
    metadata (read back via CodeSetProvenance.from_parquet_metadata).
    """

    name: str
    description: str


#: Single source of truth for shipped code_set corpora (HC-1 slice 1 item 5).
#: Adding a corpus means adding ONE entry here; `_SHIPPED_CORPORA`
#: (transforms/code_set.py) and `TestShippedCorpora`
#: (tests/unit/transforms/test_code_set.py) both iterate this dict instead of
#: hardcoding names. `codesets/__init__.py`'s docstring and the corpus list in
#: docs/strategies.md are documentation copies checked against this registry
#: by a drift-guard test, since neither is generated code.
CODESET_REGISTRY: dict[str, CodeSetSpec] = {
    "icd10": CodeSetSpec(
        name="icd10",
        description="ICD-10-CM diagnosis codes (CDC/NCHS + CMS). Public domain.",
    ),
    "hcpcs": CodeSetSpec(
        name="hcpcs",
        description="HCPCS Level II procedure/supply codes (CMS). Public domain.",
    ),
    "ndc": CodeSetSpec(
        name="ndc",
        description="FDA National Drug Code Directory entries. Public domain.",
    ),
    "mcc": CodeSetSpec(
        name="mcc",
        description="ISO 18245 Merchant Category Codes. Public reference enumeration.",
    ),
}
