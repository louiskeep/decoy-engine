"""Opt-in attack-based privacy metrics contract for the SynthReport (D7c).

Membership-inference and shadow-model attacks need heavy ML dependencies,
so they live in the optional `decoy_engine_privacy_attacks` extras package
and run only when the caller opts in AND that package is importable.
Otherwise the block records that no attack was attempted. The engine
carries no ML attack dependency; `synth_report.py` assembles this block into
the report and re-exports `compute_attack_metrics`.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def compute_attack_metrics(
    source: pd.DataFrame,
    output: pd.DataFrame,
    *,
    enable_attacks: bool = False,
    extras_module: str = "decoy_engine_privacy_attacks",
    holdout: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Run optional attack-based privacy metrics (D7c contract).

    Attack-based metrics (Membership Inference Attack, shadow-model
    attacks) require heavy ML dependencies and produce results whose
    interpretation needs care. Per the sprint security requirements:

      "Keep optional metric libraries as extras until approved by
       dependency/security review."

    So this function ONLY runs attack metrics when BOTH:

      1. The caller explicitly passes `enable_attacks=True`
         (gates platform admin policy through the call).
      2. The optional extras package `decoy_engine_privacy_attacks`
         (or `extras_module` if pinned) is importable.

    Either gate failing -> returns an `{available: False, reason: ...}`
    block. The default behavior of this function -- and therefore of
    a `compute_attack_metrics(source, output)` call from the platform
    runner with no opt-in flag -- is to record that attacks were
    skipped, not to silently run them.

    Returned shape when available:

        {
          "metric": "attack_based_metrics",
          "available": True,
          "results": <whatever the extras module returned>,
          "extras_module": str,
        }

    Returned shape when unavailable:

        {
          "metric": "attack_based_metrics",
          "available": False,
          "reason": "extras_not_installed" | "not_enabled_by_caller",
          "extras_module": str,
        }

    The extras package must expose:
      `run_privacy_attacks(source, output, *, holdout=None) -> dict`
    Returned dict shape is the extras module's contract; SynthReport
    persists it under the `results` key without re-shaping. This
    keeps the engine free of any direct ML attack dependency while
    still giving operators a single place to read the results.

    NOTE on the holdout pass-through: some attack metrics (notably
    the MIA family) genuinely require a holdout frame to compute.
    Forwarding it here lets the extras module use it without
    duplicating the holdout-resolution logic the platform already
    does for D7b's DCR.
    """
    if not enable_attacks:
        return _attacks_unavailable("not_enabled_by_caller", extras_module)

    try:
        import importlib

        attacks_pkg = importlib.import_module(extras_module)
    except ImportError:
        return _attacks_unavailable("extras_not_installed", extras_module)

    run_fn = getattr(attacks_pkg, "run_privacy_attacks", None)
    if run_fn is None:
        return _attacks_unavailable(
            "extras_module_missing_entry_point",
            extras_module,
        )

    try:
        results = run_fn(source, output, holdout=holdout)
    except Exception as exc:
        # Defensive: an attack extras module's failure must NOT
        # take down the rest of the SynthReport assembly. Record
        # the failure type for operator visibility, not the raw
        # exception (which could carry sensitive data).
        return {
            "metric": "attack_based_metrics",
            "available": False,
            "reason": "extras_runtime_error",
            "extras_module": extras_module,
            "error_type": type(exc).__name__,
        }

    return {
        "metric": "attack_based_metrics",
        "available": True,
        "results": results,
        "extras_module": extras_module,
    }


def _attacks_unavailable(reason: str, extras_module: str) -> dict[str, Any]:
    """The default attacks block: 'no attack was attempted'.

    Operators reading the SynthReport must be able to tell the
    difference between 'attacks were tried and the synth survived'
    (results dict) and 'no attack was tried' (this block). The
    explicit reason makes that distinction unambiguous in the
    audit trail.
    """
    return {
        "metric": "attack_based_metrics",
        "available": False,
        "reason": reason,
        "extras_module": extras_module,
    }
