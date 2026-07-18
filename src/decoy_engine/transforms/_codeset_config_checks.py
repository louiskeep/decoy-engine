"""Config-time validation for the code_set strategy.

Split out of ``transforms/code_set.py`` (HC-2) to keep that module under its
LOC cap: ``validate_code_set_config``'s gate list grew as HC-2 added the
reserved-licensed-name refusal and the corpus_source_version type check, and
``code_set.py`` already owns strategy-level concepts (config dataclass, apply,
chapter derivation) that do not belong in a validation module.

Operates on a RAW config dict, not a ``CodeSetConfig`` -- so it has no
dependency on ``code_set.py`` and cannot create an import cycle. Called at
config parse time (fast, no I/O). Corpus loading and deeper schema checks
happen in ``_codeset_loader._read_corpus_record`` at apply time, pre-mutation,
so invalid corpora fail closed before any data is changed.

Re-exported from ``transforms/code_set.py`` so
``from decoy_engine.transforms.code_set import validate_code_set_config`` keeps
working unchanged.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms._codeset_loader import _SHIPPED_CORPORA
from decoy_engine.transforms._codeset_provenance import RESERVED_LICENSED_NAMES


def validate_code_set_config(cfg: dict[str, Any]) -> None:
    """Validate a code_set config dict; raise PlanCompileError on any failure.

    Checks:
      - ``code_set`` is present, non-empty, and a string.
      - ``corpus_source_version`` (when present) is a scalar release id
        (str or an unquoted-YAML number), not a bool/list/dict.
      - The name is not a reserved licensed corpus (e.g. "cpt") requested
        with ``corpus_source`` "shipped" (or absent) -- those are upload-only
        (HC-2 D2b). Checked BEFORE the shipped-corpus lookup below so the
        error is the specific licensing one, not the generic "not found"
        (reserved names are deliberately absent from ``_SHIPPED_CORPORA``).
      - When ``corpus_source`` is not a ``customer:`` prefix (i.e. a shipped
        load), the name must be a known shipped corpus.

    Args:
        cfg: Raw config dict.

    Raises:
        PlanCompileError: Any validation failure.
    """
    name = cfg.get("code_set")
    if not name or not isinstance(name, str):
        # `not isinstance(name, str)` guards a free-form provider_config that
        # supplies a non-string (e.g. `code_set: [icd10]`): the membership
        # tests below would otherwise raise a raw `TypeError: unhashable type`
        # instead of a coded compile error.
        raise PlanCompileError(
            code="code_set_name_missing",
            path="provider_config.code_set",
            message=(
                "'code_set' is required and must be a string naming a corpus "
                "(e.g. 'icd10', 'hcpcs', 'ndc', 'mcc', or a customer corpus name "
                "with corpus_source: customer:<path>)."
            ),
        )

    # A corpus_source_version pin must be a scalar release id (str, or an
    # unquoted-YAML number). A bool/list/dict is not a version; reject it with
    # a coded error rather than let `... or None` silently DISABLE the pin
    # (a `false` pin failing open is a silent wrong-corpus risk -- Codex HIGH).
    version = cfg.get("corpus_source_version")
    # bool checked first: bool is a subclass of int, so `False` would otherwise
    # pass the `isinstance(..., (str, int))` allow-list.
    if isinstance(version, bool) or (version is not None and not isinstance(version, (str, int))):
        raise PlanCompileError(
            code="code_set_corpus_source_version_invalid",
            path="provider_config.corpus_source_version",
            message=(
                f"corpus_source_version must be a string or numeric release id "
                f"(got {version!r} of type {type(version).__name__}). Quote the "
                "release, e.g. corpus_source_version: '2024'."
            ),
        )

    source = str(cfg.get("corpus_source", "shipped"))
    # Match `_resolve_corpus_path`'s routing exactly: ANY source that is not a
    # `customer:` prefix loads a shipped corpus. Keying only on `== "shipped"`
    # let malformed sources ("Shipped", " shipped ", None) skip the reserved-
    # name + not-found gates at compile while still resolving as shipped at
    # apply time (Codex/Dennis MEDIUM). Kept in sync deliberately.
    is_shipped_source = not source.startswith("customer:")

    if str(name).strip().lower() in RESERVED_LICENSED_NAMES and is_shipped_source:
        raise PlanCompileError(
            code="code_set_reserved_licensed_name",
            path="provider_config.code_set",
            message=(
                f"{name!r} is a licensed code set the engine never ships "
                "(AMA CPT license / proprietary grouper). It is upload-only: "
                "set corpus_source: customer:<path> to your own licensed copy."
            ),
        )

    if is_shipped_source and name not in _SHIPPED_CORPORA:
        raise PlanCompileError(
            code="code_set_corpus_not_found",
            path="provider_config.code_set",
            message=(
                f"corpus {name!r} not found in shipped corpora. "
                f"Available: {sorted(_SHIPPED_CORPORA)}. "
                f"To use a custom corpus, set corpus_source: customer:<path>."
            ),
        )
