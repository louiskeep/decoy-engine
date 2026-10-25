"""text_redact strategy (engine-v2 MG-2, 2026-05-31): span-level PII redaction.

Walks each cell of a free-text column with `storm.detectors.iter_spans(...)`
and replaces matched PII spans with a configurable token. Non-PII text is
preserved byte-for-byte. Deterministic by construction: output is a pure
function of (input, detector_ids, token, label_token).

Config (`provider_config`):
    detectors   list[str] | None  Which detector ids to run.
                                  None or [] = every built-in span detector
                                  (an empty list is fail-safe, never "redact
                                  nothing").
    token       str               Replacement token. Default "[REDACTED]".
    label_token bool              If True, emit "[REDACTED:<detector_id>]"
                                  instead of `token`. When `label_token`
                                  is True, the `token` value is ignored
                                  (the per-detector label takes
                                  precedence).

The token is treated as a string literal: regex metacharacters in it are
NOT interpreted. Non-string tokens fall back to passthrough (the cell is
left unchanged) so a misconfigured plan never crashes the run.

Per-cell cost: O(n_detectors) regex scans over each non-null cell. Bounded
for free-text columns up to ~100k rows of short notes; multi-MB cells need
chunking and are not a V1 target.

Compared to `redact`: `redact` replaces the WHOLE cell with a constant;
`text_redact` replaces only the matched spans. `redact` destroys clinical
content; `text_redact` keeps it.

This strategy fills the HIPAA killer-feature gap: today we can sanitize a
structured EHR table because each cell is one identifier, but we cannot
sanitize a `clinical_notes` column ("Patient John Doe, MRN 12345,
presented with...") without losing the clinical content.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._strategies._ner_config import (
    enforce_ner_model_version,
    resolve_ner_config,
)
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.storm.detectors import Span, iter_spans

_DEFAULT_TOKEN = "[REDACTED]"  # noqa: S105 - redaction placeholder, not a credential


class TextRedactHandler:
    """Replace PII spans in each cell with a fixed token."""

    name: str = "text_redact"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg = provider_config_to_dict(plan.provider_config)
        detectors_cfg = cfg.get("detectors")
        token = cfg.get("token", _DEFAULT_TOKEN)
        label_token = bool(cfg.get("label_token", False))

        if not isinstance(token, str):
            return df, []

        # WS2 (2026-06-12): opt-in NER spans for person_name/location, the
        # categories the regex catalog deliberately cannot cover. F14b
        # (2026-06-26): a spaCy model update between plan compile and run
        # silently changes redaction output for the same config + seed, so fail
        # closed when the installed model version no longer matches the version
        # stamped at compile. Both steps are shared with the out-of-core route
        # through `_ner_config` so the two routes cannot drift (2026-10-25
        # de-duplication); the version guard needs no spaCy pipeline and is a
        # no-op when NER is off or no version was stamped.
        ner_model, ner_entities = resolve_ner_config(cfg)
        enforce_ner_model_version(
            ner_model=ner_model,
            plan_ner_model_version=plan.ner_model_version,
            column=column,
            strategy="text_redact",
        )

        detector_ids: list[str] | None
        if detectors_cfg is None:
            detector_ids = None
        elif isinstance(detectors_cfg, (list, tuple)):
            # Fail-safe (S5c F2): an empty detector list means "all detectors",
            # NOT "redact nothing". iter_spans treats [] as zero detectors, so a
            # cleared/empty selection that reaches the handler from any authoring
            # path (hand-edited YAML, imported manifest) would otherwise leave PHI
            # silently unredacted. Coerce empty to None so every path runs all.
            detector_ids = [str(d) for d in detectors_cfg] or None
        else:
            return df, []

        # SEQ seam, after the config early-returns above so those keep their handler behavior.
        # The RESOLVED detectors go through unchanged: None means all, and () would mean none.
        seq = getattr(ctx, "seq_native", None)
        if seq is not None:
            picked = seq.text_redact_column(
                ctx,
                df,
                column,
                detectors=None if detector_ids is None else tuple(detector_ids),
                token=token,
                label_token=label_token,
            )
            if picked is not None:
                df[column] = pd.Series(picked, index=df.index, dtype=object)
                return df, []

        col = df[column]
        if pd.api.types.is_extension_array_dtype(col.dtype):
            col = col.astype(object)
        else:
            col = col.copy()

        # QA-3 F3 (2026-05-31): collect masked values into a list and
        # write back to the Series in one assignment. The pre-fix loop
        # called `col.at[idx] = ...` per row; pandas' positional setter
        # invalidates the underlying block cache on each write, so for
        # 200-row batches the per-cell setter cost was ~30% of total
        # strategy time. Single Series assignment moves the cost off
        # the hot loop. Positional iteration via to_list() also
        # sidesteps the duplicate-index issue called out in F2.
        col_values = col.to_list()
        # Vectorized null mask catches every pandas null marker uniformly
        # (None, float nan, pd.NA, pd.NaT); the previous None/float-only
        # per-cell guard let pd.NA/pd.NaT fall through to str() and emit
        # the literal '<NA>'/'NaT' into masked output. Series.isna also
        # sidesteps pd.isna's ambiguous result on array-like cells.
        null_mask = col.isna().to_list()

        if ner_model is None:
            for pos, text in enumerate(col_values):
                if null_mask[pos]:
                    continue
                if not isinstance(text, str):
                    text = str(text)
                spans = iter_spans(text, detector_ids, extra_spans=None)
                col_values[pos] = text if not spans else _splice(text, spans, token, label_token)
        else:
            # Phase 5 (docs/plans/2026-09-08-p5-ner-batch-helper.md): batch the
            # NER inference through `nlp.pipe` instead of one `nlp(text)` call
            # per cell. Coerce once up front so the SAME string reaches both
            # the NER batch and `iter_spans`, then infer + apply
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
                for pos, extra in zip(window, window_spans, strict=True):
                    text = col_values[pos]
                    spans = iter_spans(text, detector_ids, extra_spans=extra)
                    col_values[pos] = (
                        text if not spans else _splice(text, spans, token, label_token)
                    )

        df[column] = pd.Series(col_values, index=df.index, dtype=object)
        return df, []


def _splice(
    text: str,
    spans: list[Span],
    token: str,
    label_token: bool,
) -> str:
    """Walk spans + emit surrounding chunks + token. Preserves byte offsets.

    Spans must be non-overlapping and sorted by start (iter_spans guarantees
    both). `token` is emitted as a literal string; if `label_token`, the
    detector id is suffixed inside the brackets (e.g. "[REDACTED:ssn]").
    """
    parts: list[str] = []
    cursor = 0
    for s in spans:
        if s.start > cursor:
            parts.append(text[cursor : s.start])
        if label_token:
            parts.append(f"[REDACTED:{s.detector_id}]")
        else:
            parts.append(token)
        cursor = s.end
    if cursor < len(text):
        parts.append(text[cursor:])
    return "".join(parts)
