"""code_set strategy handler (engine-v2 SP-09b, 2026-06-28).

Thin V2 StrategyHandler wrapping ``decoy_engine.transforms.code_set``.
Core logic (HMAC-keyed code selection, per-row gen-mode seeding, corpus
loading, chapter_preserve, config validation) lives in the transforms
module for testability and reuse outside the execution layer.

Config keys accepted via ``plan.provider_config``:
  code_set       str   Corpus name (e.g. ``"icd10"``, ``"hcpcs"``, ``"ndc"``, ``"mcc"``).
  mode           str   ``"mask"`` (default) or ``"gen"``.
  chapter_preserve bool When True, restrict to the same chapter bucket as input.
  corpus_source  str   ``"shipped"`` (default) or ``"customer:<absolute_path>"``.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.code_set import CodeSetConfig, apply_code_set


class CodeSetHandler:
    """Code-corpus masking: replaces each value with a code drawn from a named corpus.

    Delegates to ``decoy_engine.transforms.code_set.apply_code_set``.
    Config validation runs at execution time, pre-mutation, fail-closed: an invalid
    ``provider_config`` raises ``PlanCompileError`` (via ``CodeSetConfig.from_dict``)
    before any row is processed.

    Gen mode uses per-row variation (row_index threaded into the RNG seed) so
    the output column is not constant for a fixed job_seed.
    """

    name: str = "code_set"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg = provider_config_to_dict(plan.provider_config)
        mode = str(cfg.get("mode", "mask"))
        code_cfg = CodeSetConfig.from_dict(cfg)

        if mode == "gen" and plan.namespace is None:
            raise StrategyError(
                code="code_set_gen_requires_namespace",
                strategy="code_set",
                message=(
                    f"column {column!r} uses code_set gen mode but has no namespace. "
                    "Set namespace on the ColumnSeed so gen-mode columns are "
                    "decorrelated across namespaces via derive_index."
                ),
            )

        source = df[column]
        na_mask = source.isna().to_numpy()
        out: list[object] = []
        for i, value in enumerate(source):
            if na_mask[i]:
                out.append(None)
                continue
            result = apply_code_set(
                str(value),
                code_cfg,
                mode=mode,
                job_seed=ctx.job_seed,
                row_index=i,
                namespace=plan.namespace,
            )
            out.append(result)

        df[column] = out
        return df, []
