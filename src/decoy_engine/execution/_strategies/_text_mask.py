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
  sub_floor_span        str | None        Task 5.2 plan P3-final. "redact" or
                                          "synthetic": how to handle an fpe span
                                          match whose domain falls below the FF1
                                          minimum (e.g. a 5-digit us_zip) or that
                                          fails checksum validation. No default;
                                          required if any configured detector's
                                          fpe strategy can produce such a match.
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

from decoy_engine.errors import FpeUnencryptableError
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
        # Task 5.2 plan P3-final: no default. `None` fails closed inside
        # `mask_cell` the first time a sub-floor fpe span is actually matched;
        # a column whose configured detectors never produce one never notices.
        sub_floor_span_policy = cfg.get("sub_floor_span")
        sub_floor_span_policy = (
            str(sub_floor_span_policy) if sub_floor_span_policy is not None else None
        )
        sub_floor_notices: dict[str, int] = {}

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

        try:
            if ner_model is None:
                for pos, value in enumerate(col_values):
                    if null_mask[pos]:
                        continue
                    if not isinstance(value, str):
                        value = str(value)
                    col_values[pos] = mask_cell(
                        value,
                        ctx.mask_key,
                        detector_ids=detector_ids,
                        extra_spans=None,
                        strategy_map=per_detector or None,
                        unmatched_span_policy=policy,
                        token=token,
                        cfg=extra or None,
                        sub_floor_span_policy=sub_floor_span_policy,
                        sub_floor_notices=sub_floor_notices,
                    )
            else:
                # Phase 5 (docs/plans/2026-09-08-p5-ner-batch-helper.md): batch the
                # NER inference through `nlp.pipe` instead of one `nlp(text)` call
                # per cell. Coerce once up front so the SAME string reaches both
                # the NER batch and mask_cell, then infer + apply
                # `_NER_APPLY_WINDOW` rows at a time -- this route is full-frame
                # (the whole column is already in memory), so a bounded window
                # keeps peak RSS from rising materially above the per-cell loop on
                # a wide column, rather than collecting every span list at once.
                from decoy_engine.storm.ner import _NER_APPLY_WINDOW, iter_ner_spans_batch

                non_null_positions = [pos for pos, is_null in enumerate(null_mask) if not is_null]
                for pos in non_null_positions:
                    if not isinstance(col_values[pos], str):
                        col_values[pos] = str(col_values[pos])

                for start in range(0, len(non_null_positions), _NER_APPLY_WINDOW):
                    window = non_null_positions[start : start + _NER_APPLY_WINDOW]
                    window_spans: list[list[Span]] = iter_ner_spans_batch(
                        [col_values[pos] for pos in window],
                        model=ner_model,
                        entities=ner_entities,
                    )
                    for pos, ner_spans in zip(window, window_spans, strict=True):
                        col_values[pos] = mask_cell(
                            col_values[pos],
                            ctx.mask_key,
                            detector_ids=detector_ids,
                            extra_spans=ner_spans,
                            strategy_map=per_detector or None,
                            unmatched_span_policy=policy,
                            token=token,
                            cfg=extra or None,
                            sub_floor_span_policy=sub_floor_span_policy,
                            sub_floor_notices=sub_floor_notices,
                        )
        except FpeUnencryptableError as exc:
            raise StrategyError(
                code="fpe_unencryptable_domain",
                strategy="text_mask",
                message=(
                    f"column {column!r}: {exc}. The engine fails closed rather than "
                    "silently choose a sub_floor_span fallback."
                ),
            ) from exc

        df[column] = pd.Series(col_values, index=df.index, dtype=object)

        warnings: list[QualityWarning] = []
        if sub_floor_notices:
            # Task 5.2 plan P3-final: "visible, not silent". One aggregate,
            # structured warning per column (mask_cell's own per-span log line
            # at WARNING is the always-on signal; this is the QualityWarning
            # channel counterpart, mirroring how the fpe strategy aggregates
            # `fpe_partial_plaintext_disclosure`).
            warnings.append(
                QualityWarning(
                    code="text_mask_sub_floor_span_handled",
                    provider="text_mask",
                    column=column,
                    detail={
                        "policy": sub_floor_span_policy,
                        "by_detector": dict(sub_floor_notices),
                        "total": sum(sub_floor_notices.values()),
                        "note": (
                            "these spans' domain was below the FF1 minimum admissible "
                            "domain, or failed checksum validation, and could not be "
                            "FF1-encrypted; they were handled under the configured "
                            "sub_floor_span policy instead (non-reversible)."
                        ),
                    },
                )
            )
        return df, warnings
