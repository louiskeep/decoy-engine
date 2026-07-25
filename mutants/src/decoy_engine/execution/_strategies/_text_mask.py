"""text_mask strategy handler (engine-v2 SP-07, 2026-06-28).

Thin V2 StrategyHandler that wraps ``decoy_engine.transforms.text_mask``.
Core logic (HMAC-keyed span masking, dispatch table, unmatched_span_policy)
lives in the transforms module so it can be reused from outside the execution
layer without pulling in the full adapter dependencies.

Config keys accepted via ``plan.provider_config``:
  detectors             list[str] | None  Detector IDs to run. None = all span detectors.
  per_detector_strategy dict[str, str]    Per-detector strategy overrides.
  unmatched_span_policy str               "redact" (default), "passthrough",
                                          "replace_with_token".
  token                 str               Replacement token. Default "[REDACTED]".
  min_days              int               Date-shift lower bound. Default -365.
  max_days              int               Date-shift upper bound. Default 365.
  ner                   bool | dict       TX-2 (2026-07-20): opt-in NER spans for
                                          person_name/location, mirroring
                                          `_text_redact.TextRedactHandler.run`
                                          exactly (same config shape, same
                                          `ner_model_version_mismatch` fail-closed
                                          guard). `True` or `{model, entities}`.
                                          Off by default: model load + per-cell
                                          inference is a real cost.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.storm.detectors import Span
from decoy_engine.transforms.text_mask import mask_cell


class TextMaskHandler:
    """Span-level PII masking with per-detector strategy dispatch (SP-07).

    Implements the V2 StrategyHandler protocol. Iterates over non-null column
    cells and delegates each to ``mask_cell``, passing ``ctx.mask_key`` as the
    HMAC key for cross-cell determinism (DE-02: the keyed span mapping draws from
    the mask key, not the generation seed).
    """

    name: str = "text_mask"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg = provider_config_to_dict(plan.provider_config)

        # Resolve detector list (None = all built-in span detectors).
        detectors_raw = cfg.get("detectors")
        detector_ids: list[str] | None
        if isinstance(detectors_raw, (list, tuple)):
            detector_ids = [str(d) for d in detectors_raw] or None
        else:
            detector_ids = None

        per_detector: dict[str, str] = dict(cfg.get("per_detector_strategy") or {})
        policy = str(cfg.get("unmatched_span_policy", "redact"))
        token = str(cfg.get("token", "[REDACTED]"))

        # Pass date-shift bounds through to mask_cell via the cfg dict.
        extra: dict[str, Any] = {}
        for key in ("min_days", "max_days"):
            if key in cfg:
                extra[key] = cfg[key]

        # TX-2 (2026-07-20): mirrors `_text_redact.TextRedactHandler.run` (WS2)
        # verbatim in shape -- resolve the `ner` config, then fail closed if the
        # installed spaCy model no longer matches the version stamped at compile
        # (plan.ner_model_version). Runs before any iter_ner_spans call, so it
        # needs no spaCy pipeline; skipped when no version was stamped.
        ner_cfg = cfg.get("ner")
        ner_model: str | None = None
        ner_entities: list[str] | None = None
        if ner_cfg:
            from decoy_engine.storm.ner import DEFAULT_NER_MODEL

            if isinstance(ner_cfg, dict):
                ner_model = str(ner_cfg.get("model") or DEFAULT_NER_MODEL)
                raw_entities = ner_cfg.get("entities")
                if isinstance(raw_entities, (list, tuple)) and raw_entities:
                    ner_entities = [str(e) for e in raw_entities]
            else:
                ner_model = DEFAULT_NER_MODEL

        if ner_model is not None and plan.ner_model_version is not None:
            from decoy_engine.storm.ner import installed_model_version

            current_version = installed_model_version(ner_model)
            if current_version is not None and current_version != plan.ner_model_version:
                raise StrategyError(
                    code="ner_model_version_mismatch",
                    strategy="text_mask",
                    message=(
                        f"column {column!r}: NER model {ner_model!r} is installed at "
                        f"version {current_version!r} but the plan was compiled against "
                        f"{plan.ner_model_version!r}. spaCy model updates change masking "
                        f"output for the same config + seed; recompile the plan against the "
                        f"installed model (or pin the model version) to keep masked output "
                        f"reproducible."
                    ),
                )

        col = df[column]
        if pd.api.types.is_extension_array_dtype(col.dtype):
            col = col.astype(object)
        else:
            col = col.copy()

        null_mask = col.isna().to_list()
        col_values = col.to_list()

        for pos, value in enumerate(col_values):
            if null_mask[pos]:
                continue
            if not isinstance(value, str):
                value = str(value)
            ner_spans: list[Span] | None = None
            if ner_model is not None:
                from decoy_engine.storm.ner import iter_ner_spans

                ner_spans = iter_ner_spans(value, model=ner_model, entities=ner_entities)
            col_values[pos] = mask_cell(
                value,
                ctx.mask_key,
                detector_ids=detector_ids,
                extra_spans=ner_spans,
                strategy_map=per_detector or None,
                unmatched_span_policy=policy,
                token=token,
                cfg=extra or None,
            )

        df[column] = pd.Series(col_values, index=df.index, dtype=object)
        return df, []
