"""The copula quality report: a frozen, value-free record of how well synthetic rows from a copula
model match the real table and how close they sit to it.

Marginal goodness of fit, the new-row check, the attack contract and the report disclaimers are
the engine's existing `quality` functions, run on the copula output; this module adds the pairwise
block (`_quality_pairwise`, behind `_quality_gate`) and the DCR block (`_quality_dcr`). Every
statistic of the real frame is released only behind the copula's disclosure floors, rounded to
`RELEASE_DECIMALS`. The report holds column names, counts, scores and tokens, never a value.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import pandas as pd

from decoy_engine.generation.copula import _floors
from decoy_engine.generation.copula._model import KIND_EMPTY, GaussianCopulaModel
from decoy_engine.generation.copula._quality_dcr import (
    DcrBlock,
    DcrSummary,
    HoldoutComparison,
    compute_dcr_block,
)
from decoy_engine.generation.copula._quality_gate import (
    METRIC_CONTINGENCY,
    METRIC_SPEARMAN,
    STATUS_SCORED,
    STATUS_UNDEFINED,
    STATUS_WITHHELD,
    GateContext,
    over_budget,
)
from decoy_engine.generation.copula._quality_inputs import (
    canonical,
    check_arguments,
    discrete_states,
    model_snapshot,
    tie_to_model,
    with_str_names,
)
from decoy_engine.generation.copula._quality_pairwise import PairScore, score_pairs
from decoy_engine.quality.fidelity import compute_fidelity
from decoy_engine.quality.synth_report import (
    assemble_synth_report,
    compute_attack_metrics,
    compute_new_row_synthesis,
)

COPULA_QUALITY_REPORT_SCHEMA_VERSION = "decoy-copula-quality-report/v1"

_DEFAULT_EXTRAS = "decoy_engine_privacy_attacks"
_RESULT_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")
_RESULTS_WITHHELD = "withheld_not_value_free"
_GOF_KEYS = ("ks_complement", "chi_cramers_v")


def _round(value: float) -> float:
    return round(float(value), _floors.RELEASE_DECIMALS) + 0.0


def _mean(values: list[float]) -> float | None:
    return _round(sum(values) / len(values)) if values else None


@dataclass(frozen=True)
class MarginalFit:
    column: str
    gof_method: str | None
    gof_value: float | None


@dataclass(frozen=True, repr=False)
class CopulaQualityReport:
    real_rows: int
    synthetic_rows: int
    holdout_rows: int | None
    marginal: tuple[MarginalFit, ...]
    pairs: tuple[PairScore, ...]
    pairwise_score: float | None
    correlation_score: float | None
    contingency_score: float | None
    dcr: DcrBlock | None
    synth_report: Mapping[str, Any]
    schema_version: str = COPULA_QUALITY_REPORT_SCHEMA_VERSION

    def __repr__(self) -> str:
        return (
            f"CopulaQualityReport(real_rows={self.real_rows}, synthetic_rows={self.synthetic_rows}, "
            f"pairs={len(self.pairs)}, pairwise_score={self.pairwise_score}, "
            f"correlation_score={self.correlation_score}, "
            f"contingency_score={self.contingency_score})"
        )

    def to_dict(self) -> dict[str, Any]:
        """A new JSON-safe dict on every call."""
        counts = {
            s: sum(1 for p in self.pairs if p.status == s)
            for s in (STATUS_SCORED, STATUS_WITHHELD, STATUS_UNDEFINED)
        }
        return {
            "schema_version": self.schema_version,
            "rows": {
                "real": self.real_rows,
                "synthetic": self.synthetic_rows,
                "holdout": self.holdout_rows,
            },
            "marginal": {
                "columns": [
                    {"column": m.column, "gof_method": m.gof_method, "gof_value": m.gof_value}
                    for m in self.marginal
                ]
            },
            "pairs": [
                {
                    "columns": list(p.columns),
                    "metric": p.metric,
                    "status": p.status,
                    "reason": p.reason,
                    "score": p.score,
                }
                for p in self.pairs
            ],
            "pairwise": {
                "score": self.pairwise_score,
                "correlation_score": self.correlation_score,
                "contingency_score": self.contingency_score,
                **counts,
            },
            "dcr": None if self.dcr is None else _dcr_dict(self.dcr),
            "synth_report": _thaw(self.synth_report),
        }


def _summary_dict(summary: DcrSummary) -> dict[str, Any]:
    return {
        "p05": summary.p05,
        "p25": summary.p25,
        "median": summary.median,
        "p75": summary.p75,
        "p95": summary.p95,
        "rows": summary.rows,
        "withheld": dict(summary.withheld),
    }


def _dcr_dict(block: DcrBlock) -> dict[str, Any]:
    holdout: HoldoutComparison | None = block.holdout
    return {
        "columns": list(block.columns),
        "real_rows_sampled": block.real_rows_sampled,
        "synthetic_rows_sampled": block.synthetic_rows_sampled,
        "synthetic_to_real": _summary_dict(block.synthetic_to_real),
        "exact_match_share": block.exact_match_share,
        "real_to_real": _summary_dict(block.real_to_real),
        "holdout": None
        if holdout is None
        else {
            "rows_compared": holdout.rows_compared,
            "closer_to_real_share": holdout.closer_to_real_share,
            "protection_score": holdout.protection_score,
            "synthetic_to_holdout": _summary_dict(holdout.synthetic_to_holdout),
        },
    }


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


def compute_copula_quality_report(
    real: pd.DataFrame,
    synthetic: pd.DataFrame,
    model: GaussianCopulaModel,
    *,
    holdout: pd.DataFrame | None = None,
    dcr_max_rows: int = 5000,
    attack_extras: str | None = None,
) -> CopulaQualityReport:
    """Score `synthetic` (rows drawn from `model`) against `real` (the table `model` was fit on).

    Precondition: `synthetic` must be rows sampled from `model`, never rows the caller chose. With
    chosen rows the report is an oracle on `real`: a repeated probe row shows up in the new-row and
    distance blocks (membership), and chosen column values make a pair score give back the real
    coefficient. A wiring slice must draw `synthetic` itself and not accept it from a user.

    `holdout` is real rows the fit never saw, for the overfitting comparison and the attack
    contract. `attack_extras` names an optional attacks module to run (None: attacks not run).
    """
    check_arguments(real, synthetic, model, holdout, dcr_max_rows, attack_extras)
    real_snapshot, high_cardinality = tie_to_model(with_str_names(real), model)
    real_c, synth_c = canonical(real, model), canonical(synthetic, model)
    holdout_c = None if holdout is None else canonical(holdout, model)

    real_states: dict[str, Any] = {}
    synth_states: dict[str, Any] = {}
    for column in model.columns:
        if column.kind != KIND_EMPTY:
            real_states[column.name], synth_states[column.name] = discrete_states(
                real_c[column.name], synth_c[column.name], column
            )
    ctx = GateContext(model, real_c, real_states, high_cardinality, over_budget(model))
    pairs = score_pairs(ctx, synth_c, synth_states)
    scored = [p for p in pairs if p.status == STATUS_SCORED and p.score is not None]

    attacks = _filter_attack_results(
        # Copies: an extras module that edits its inputs must not reach the caller's frames or
        # the rest of the report.
        compute_attack_metrics(
            real.copy(deep=True),
            synthetic.copy(deep=True),
            enable_attacks=attack_extras is not None,
            extras_module=attack_extras or _DEFAULT_EXTRAS,
            holdout=None if holdout is None else holdout.copy(deep=True),
        )
    )
    synth_report = assemble_synth_report(
        new_row_synthesis=compute_new_row_synthesis(real_c, synth_c), dcr=None, attacks=attacks
    )
    return CopulaQualityReport(
        real_rows=len(real),
        synthetic_rows=len(synthetic),
        holdout_rows=None if holdout is None else len(holdout),
        marginal=_marginal(real_snapshot, synthetic, model, high_cardinality),
        pairs=pairs,
        pairwise_score=_mean([p.score for p in scored if p.score is not None]),
        correlation_score=_mean(
            [p.score for p in scored if p.metric == METRIC_SPEARMAN and p.score is not None]
        ),
        contingency_score=_mean(
            [p.score for p in scored if p.metric == METRIC_CONTINGENCY and p.score is not None]
        ),
        dcr=compute_dcr_block(real_c, synth_c, holdout_c, model, dcr_max_rows),
        synth_report=_freeze(synth_report),
    )


def _marginal(
    real_snapshot: dict[str, Any],
    synthetic: pd.DataFrame,
    model: GaussianCopulaModel,
    high_cardinality: frozenset[str],
) -> tuple[MarginalFit, ...]:
    """Only the snapshot-only goodness-of-fit extras: they read bin edges, bin counts and label
    counts, which for the real side are the model's own marginals. The primary similarity reads
    real quantiles, which the model does not hold, and is not released."""
    synthetic_snapshot = model_snapshot(with_str_names(synthetic), model, high_cardinality)
    fidelity = compute_fidelity(real_snapshot, synthetic_snapshot)
    by_column = {entry["column"]: entry for entry in fidelity["marginal"]["columns"]}
    out: list[MarginalFit] = []
    for column in model.columns:
        extras = by_column.get(column.name, {}).get("extra_metrics") or {}
        gof = next((extras[k] for k in _GOF_KEYS if k in extras), None)
        if not gof:
            out.append(MarginalFit(column.name, None, None))
            continue
        value = gof.get("value") if gof.get("comparable") else None
        out.append(
            MarginalFit(column.name, gof.get("method"), None if value is None else _round(value))
        )
    return tuple(out)


def _filter_attack_results(block: dict[str, Any]) -> dict[str, Any]:
    """Keep extras results only as a flat mapping of plain keys to bools or numbers in [0, 1]."""
    if not block.get("available") or "results" not in block:
        return block
    results = block["results"]
    kept: dict[str, Any] = {}
    clean = isinstance(results, Mapping)
    for key, value in results.items() if clean else ():
        if not isinstance(key, str) or _RESULT_KEY.fullmatch(key) is None:
            clean = False
        elif value is None or isinstance(value, bool):
            kept[key] = value
        elif isinstance(value, (int, float)) and math.isfinite(value) and 0.0 <= value <= 1.0:
            kept[key] = _round(value)
        else:
            clean = False
    out = {k: v for k, v in block.items() if k != "results"}
    if clean:
        out["results"] = kept
    else:
        out["results_status"] = _RESULTS_WITHHELD
    return out
