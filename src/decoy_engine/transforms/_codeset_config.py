"""``CodeSetConfig``: the parsed ``code_set`` provider config.

Split from ``code_set`` (module-size cap) so the keyed corpus selection, the catalogued
``mask.code_set`` draw site, stays in ``code_set.py``; re-exported there unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from decoy_engine.transforms._codeset_config_checks import validate_code_set_config


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
        corpus_source_version: Optional expected SOURCE release id (e.g.
            "FY2024"), independently verified (HC-2 D2a) against the loaded
            corpus's embedded ``CodeSetProvenance.source_version`` at load
            time. The engine fails closed (``code_set_corpus_version_mismatch``)
            if the loaded corpus's ``source_version`` differs from this pin
            or is absent. Distinct from ``corpus_version`` /
            ``CORPUS_METADATA_VERSION``, which is the metadata FORMAT
            version, not the source's own release. ``None`` (default) means
            no pin: today's unversioned behavior, unchanged.

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
    corpus_source_version: str | None = None

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> CodeSetConfig:
        """Parse a config dict; raise PlanCompileError on invalid input.

        Structural validation only (code_set name present, corpus_source
        format legal). Corpus loading happens at apply time, pre-mutation,
        so this is a fast compile-time check.

        Args:
            cfg: Config dict with key ``code_set`` (required), plus optional
                ``chapter_preserve``, ``corpus_source``, and
                ``corpus_source_version``.

        Raises:
            PlanCompileError: Missing ``code_set``, unrecognised shipped
                corpus name (when corpus_source is "shipped"), or a reserved
                licensed name (e.g. "cpt") requested with corpus_source
                "shipped"/absent.
        """
        validate_code_set_config(cfg)
        raw_version = cfg.get("corpus_source_version")
        return cls(
            code_set=cfg["code_set"],
            chapter_preserve=bool(cfg.get("chapter_preserve", False)),
            corpus_source=str(cfg.get("corpus_source", "shipped")),
            # Coerce to str so an unquoted-YAML numeric release id
            # (corpus_source_version: 2024 -> int) compares equal to the
            # corpus's always-string embedded source_version ("2024"); an
            # absent/empty pin stays None (unpinned). bool/list/dict are
            # rejected by validate_code_set_config above, so they never reach
            # here to be str()'d into a bogus pin (a `false` pin must fail
            # closed, not silently disable verification).
            corpus_source_version=(str(raw_version) if raw_version not in (None, "") else None),
        )
