"""Shared NER config resolution + model-version enforcement for text_redact.

De-duplication (2026-10-25): the full-frame `TextRedactHandler`
(`_text_redact.py`) and the out-of-core Group (b) kernel
(`out_of_core/_mask_group_b.text_redact_array`) each carried their own mirrored
copy of two steps: resolve the optional `ner` provider_config into a (model,
entities) pair, and run the compile-vs-runtime `ner_model_version_mismatch`
guard. Two copies of the same logic drift: a fix to one route's resolution or
guard silently diverges the other. This module is the single home both call, so
the resolution and the version pin can no longer disagree between routes.

Deliberately NOT consolidated (see `storm/ner.py`): the two SPAN extractors
`storm.ner.iter_ner_spans` (single-cell) and `iter_ner_spans_batch` (nlp.pipe
over a batch) stay independent, because each is the other's differential oracle.
Only this config/version plumbing is shared; the span logic is not.

`strategy` is a parameter rather than hard-coded because the same two steps also
appear in the `text_mask` handlers (`_text_mask.py` /
`out_of_core/_mask_group_c.py`), which raise the identical
`ner_model_version_mismatch` code under `strategy="text_mask"`. Those sites can
adopt this module without a behavior change; this slice wires the text_redact
pair only (its stated scope).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from decoy_engine.execution._errors import StrategyError

if TYPE_CHECKING:
    from collections.abc import Mapping


def resolve_ner_config(cfg: Mapping[str, Any]) -> tuple[str | None, list[str] | None]:
    """Resolve the optional `ner` provider_config into `(model, entities)`.

    Returns `(None, None)` when NER is off (no `ner` key, or a falsy one): the
    caller then runs the regex catalog alone. `ner: true` selects the default
    model with every mapped entity; `ner: {model: ..., entities: [...]}` selects
    a named model and an optional detector-id filter. An empty or non-list
    `entities` resolves to `None` (all mapped entities), matching the handlers'
    long-standing fail-safe (never "find nothing").
    """
    ner_cfg = cfg.get("ner")
    if not ner_cfg:
        return None, None
    from decoy_engine.storm.ner import DEFAULT_NER_MODEL

    ner_entities: list[str] | None = None
    if isinstance(ner_cfg, dict):
        ner_model = str(ner_cfg.get("model") or DEFAULT_NER_MODEL)
        raw_entities = ner_cfg.get("entities")
        if isinstance(raw_entities, (list, tuple)) and raw_entities:
            ner_entities = [str(e) for e in raw_entities]
    else:
        ner_model = DEFAULT_NER_MODEL
    return ner_model, ner_entities


def enforce_ner_model_version(
    *,
    ner_model: str | None,
    plan_ner_model_version: str | None,
    column: str | None,
    strategy: str,
) -> None:
    """Fail closed when the installed NER model version no longer matches the
    version stamped onto the plan at compile time.

    spaCy NER output is deterministic only per model VERSION (greedy decoding,
    no sampling), so a model upgrade between plan compile and run silently
    changes which spans are found for the same config and seed -- a determinism
    break on the highest false-negative strategy. The plan compiler stamps the
    installed version onto the `ColumnSeed` (`plan.ner_model_version`); this
    guard refuses to run rather than emit different output when they diverge.

    A no-op when NER is off (`ner_model is None`) or no version was stamped
    (`plan_ner_model_version is None`); it reads the installed version by
    `importlib.metadata` only, with no spaCy import or model load, so it is safe
    in an extras-free environment.
    """
    if ner_model is None or plan_ner_model_version is None:
        return
    from decoy_engine.storm.ner import installed_model_version

    current_version = installed_model_version(ner_model)
    if current_version is not None and current_version != plan_ner_model_version:
        where = f"column {column!r}: " if column is not None else ""
        raise StrategyError(
            code="ner_model_version_mismatch",
            strategy=strategy,
            message=(
                f"{where}NER model {ner_model!r} is installed at version "
                f"{current_version!r} but the plan was compiled against "
                f"{plan_ner_model_version!r}. spaCy model updates change output for the "
                "same config + seed; recompile the plan against the installed model (or "
                "pin the model version) to keep the output reproducible."
            ),
        )


__all__ = ["enforce_ner_model_version", "resolve_ner_config"]
