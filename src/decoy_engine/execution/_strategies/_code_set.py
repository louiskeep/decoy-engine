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

HC-1 slice 1 (2026-07-17): ``run`` stamps one corpus-provenance entry into
``ctx.code_set_corpora`` per (table, column), before the per-value loop
(``describe_loaded_corpus``). ``PandasExecutionAdapter`` surfaces the
accumulated sink into ``ExecutionResult.quality_metrics['code_set_corpora']``
at the end of the job -- counts and identifiers only (source, source_version,
effective_date, license, row_count), never raw codes. Keyed by (table,
column) rather than bare column name so two tables with a same-named
code_set column bound to different corpora each surface their own evidence
entry instead of one overwriting the other.

Codex P2 FAIL-CLOSED VALIDATION BYPASSED BY A ZERO-MATCH `when` GATE
remediation (2026-07-17): ``run``'s eager corpus load/validation only fires
when ``run`` is actually invoked, but ``execution._when_gate.run_with_
when_gate`` never calls ``run`` for a `when:`-gated column whose predicate
matches zero rows -- it short-circuits to a passthrough. That let a
code_set column referencing a missing/invalid corpus succeed silently
whenever its gate happened to match nothing. ``CodeSetHandler.preflight``
(an optional hook the when-gate calls unconditionally, before its
zero-match short-circuit) re-runs the same load/validate call so the
corpus is validated regardless of match count, while evidence stamping
(NIT-1: only on ``masked_any``) stays exclusively ``run``'s job.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._row_errors import RowError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.code_set import CodeSetConfig, apply_code_set, describe_loaded_corpus

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

    def preflight(self, plan: ColumnSeed, ctx: StrategyContext) -> None:
        """Load and validate this column's corpus, regardless of row count.

        Codex P2 FAIL-CLOSED VALIDATION BYPASSED BY A ZERO-MATCH `when` GATE
        remediation: `run()`'s eager `describe_loaded_corpus` call below
        (the fail-closed corpus check) never executes when this column has a
        `when:` gate that matches zero rows, because
        `execution._when_gate.run_with_when_gate` short-circuits to a
        passthrough before calling `run()` at all. That let a code_set
        column referencing a missing or invalid corpus succeed silently
        whenever its gate happened to match nothing. `run_with_when_gate`
        calls this optional hook unconditionally, before its zero-match
        short-circuit, so the corpus is always validated. Deliberately does
        NOT touch `ctx.code_set_corpora` -- NIT-1 (evidence stamped only
        when the column actually masks >= 1 value) is `run()`'s job alone;
        this method exists purely to make the load raise when the corpus is
        bad, not to record evidence.
        """
        cfg = provider_config_to_dict(plan.provider_config)
        code_cfg = CodeSetConfig.from_dict(cfg)
        describe_loaded_corpus(code_cfg)

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

        # HC-1 slice 1: eagerly load (and fail-close/warn on) the corpus up
        # front, same as the first per-value apply_code_set call would have,
        # just earlier -- catches a corpus-level defect even for an all-null
        # (or, under a `when` gate, fully-suppressed-subset) column that would
        # otherwise never dispatch a single value below. `evidence` is the
        # candidate provenance-evidence entry for this column; NIT-1
        # remediation (HC-1 slice 1 gap) below only commits it into the
        # shared sink if this column actually masked >=1 value, so a
        # column that validates cleanly but never dispatches (all-null, or
        # when-gated to zero live rows in this subset) does not falsely
        # report its corpus as "used."
        evidence = describe_loaded_corpus(code_cfg)

        source = df[column]
        na_mask = source.isna().to_numpy()
        out: list[object] = []
        masked_any = False
        for i, value in enumerate(source):
            if na_mask[i]:
                out.append(None)
                continue
            try:
                result = apply_code_set(
                    str(value),
                    code_cfg,
                    mode=mode,
                    job_seed=ctx.mask_key,
                    row_index=i,
                    namespace=plan.namespace,
                )
            except PlanCompileError as exc:
                if exc.code not in _PER_VALUE_CODE_SET_ERRORS:
                    raise  # corpus/config-level defect: still job-fatal
                # Sprint 2 honesty pack (D7): a per-value defect. Record it
                # and keep the ORIGINAL value in the frame (trap T4); the
                # pipeline-level rule (D8) guarantees this row never reaches
                # the main output. Not a successful mask, so it does not set
                # masked_any (the raw value stays put, same as a null row).
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
            masked_any = True

        if masked_any:
            # Once-per-(table, column) stamp (not once-per-value): a column
            # dispatched more than once (e.g. re-run under a when_gate across
            # repeated calls is not a real code path today, but this stays
            # idempotent either way) just overwrites with the same evidence
            # dict. Keyed by (table, column) rather than a bare column name --
            # two tables can legally declare a same-named code_set column
            # bound to different corpora (Codex P2 MULTI-TABLE EVIDENCE
            # COLLISION); a bare-column key let the second table's stamp
            # silently overwrite the first's. `table`/`column` ride along in
            # the evidence dict itself so the flattened metrics list (which
            # discards the sink's keys) still records which table+column used
            # which corpus.
            #
            # Codex round-4 P2 NESTED CODE_SET MIS-KEYED EVIDENCE
            # remediation: when this handler is running as a nested
            # strategy's child, `column` is the synthetic `_nested_leaves`
            # batch-collection name, not a real column on the frame --
            # `NestedStrategyHandler` stamps the real outer column onto
            # `ctx.nested_outer_column` before dispatching here. Prefer that
            # when set so evidence attributes to the column the operator
            # actually configured, and so two nested code_set columns in one
            # table key on their distinct outer columns instead of both
            # colliding on `_nested_leaves`.
            evidence_column = ctx.nested_outer_column or column
            ctx.code_set_corpora[(ctx.current_table, evidence_column)] = {
                **evidence,
                "table": ctx.current_table,
                "column": evidence_column,
            }

        df[column] = out
        return df, []
