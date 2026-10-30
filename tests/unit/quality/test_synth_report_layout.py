"""The SynthReport metrics keep one import point after the DCR and attack
metrics moved to their own modules: `synth_report` and the lazy
`decoy_engine.quality` package attribute both resolve to the defining
function, so callers (and patch targets on the defining module) see one object.
"""

from __future__ import annotations

import decoy_engine.quality as quality
from decoy_engine.quality import _attacks, _dcr, synth_report


def test_synth_report_reexports_the_defining_functions() -> None:
    assert synth_report.compute_dcr is _dcr.compute_dcr
    assert synth_report.compute_attack_metrics is _attacks.compute_attack_metrics


def test_quality_package_attributes_resolve_to_the_same_objects() -> None:
    assert quality.compute_dcr is _dcr.compute_dcr
    assert quality.compute_attack_metrics is _attacks.compute_attack_metrics
    assert quality.compute_new_row_synthesis is synth_report.compute_new_row_synthesis
