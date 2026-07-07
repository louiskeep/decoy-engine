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

Sprint 2 honesty pack (2026-07-04, D7/S6): ``apply_code_set`` can raise
``PlanCompileError`` PER VALUE under ``chapter_preserve`` (the input's
chapter absent from the corpus, or a sole-member chapter bucket with no
alternative code). Before this sprint one bad value killed the whole job
with no row attribution. ``run`` now catches exactly those two per-value
codes (``code_set_chapter_absent``, ``code_set_sole_member_bucket``) and
records a ``RowError`` (trigger ``"mask_error"``) instead, leaving the
original value in the frame (trap T4). Every OTHER ``PlanCompileError`` code
(corpus not loadable, missing/empty corpus, missing chapter column, single-
row corpus) is a config/corpus-level defect that affects every row, not a
row defect, and is re-raised unchanged: the discriminator is the exception's
``.code`` attribute, not a string match on its message (trap T8 -- no
heuristic re-derivation of what "corpus-level" means).
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._row_errors import RowError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.code_set import CodeSetConfig, apply_code_set

# PlanCompileError codes that are PER-VALUE defects (this specific input
# code cannot be masked under chapter_preserve), not corpus/config-level
# defects. Exhaustive per transforms/code_set.py's chapter_preserve raises;
# every other code (corpus load/schema/emptiness, missing chapter column,
# single-row corpus) affects every row and stays job-fatal.
_PER_VALUE_CODE_SET_ERRORS: frozenset[str] = frozenset(
    {"code_set_chapter_absent", "code_set_sole_member_bucket"}
)


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
            try:
                result = apply_code_set(
                    str(value),
                    code_cfg,
                    mode=mode,
                    job_seed=ctx.job_seed,
                    row_index=i,
                    namespace=plan.namespace,
                )
            except PlanCompileError as exc:
                if exc.code not in _PER_VALUE_CODE_SET_ERRORS:
                    raise  # corpus/config-level defect: still job-fatal
                # Sprint 2 honesty pack (D7): a per-value defect. Record it
                # and keep the ORIGINAL value in the frame (trap T4); the
                # pipeline-level rule (D8) guarantees this row never reaches
                # the main output.
                ctx.row_errors.append(
                    RowError(
                        column=column,
                        row_index=i,
                        trigger="mask_error",
                        reason=f"code_set could not mask this value ({exc.code})",
                    )
                )
                out.append(value)
                continue
            out.append(result)

        df[column] = out
        return df, []
