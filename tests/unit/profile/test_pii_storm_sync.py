"""Cross-module symmetry: PIIClass enum mirrors STORM REGISTERED_DETECTORS.

Resolution of slice-3 B1 from the Dennis review (Session 8). STORM grew
three built-in detectors (first_name, last_name, address) during the V1
detection sprint that were never propagated to PIIClass. Result: those
columns dropped their tags silently when run_pii_detection=True. The
local list inside test_dataclass_shapes.py compared the enum to a
hand-curated set that itself drifted from REGISTERED_DETECTORS; this
test compares the enum directly to the function names in the registry
so future detector additions cannot land silently.

When this test fails, the fix is one of:
  (a) Add the missing PIIClass member to decoy_engine.profile._types
      (most common: STORM grew a detector and the enum lagged).
  (b) Remove the extra PIIClass member (rare: STORM dropped a detector).
  (c) If the STORM function name does not follow detect_<id>, the
      assert on the prefix below will fail; either rename the STORM
      function or special-case it here. Renames are the right call.

A WARNING log from decoy_engine.profile._pii.detect_pii_classes provides
the runtime mirror of this CI check, so a temporary drift between
slice ships is observable in operations rather than silent.
"""

from __future__ import annotations

from decoy_engine.profile import PIIClass
from decoy_engine.storm.detectors import REGISTERED_DETECTORS


def test_every_storm_built_in_detector_has_pii_class_member() -> None:
    pii_class_values = {member.value for member in PIIClass}
    missing: list[str] = []
    for detector_fn in REGISTERED_DETECTORS:
        assert detector_fn.__name__.startswith("detect_"), (
            f"Unexpected STORM detector function name {detector_fn.__name__!r}; "
            f"this test assumes all detectors follow the detect_<id> pattern."
        )
        detector_id = detector_fn.__name__.removeprefix("detect_")
        if detector_id not in pii_class_values:
            missing.append(detector_id)
    assert not missing, (
        f"STORM REGISTERED_DETECTORS includes ids that are not in "
        f"decoy_engine.profile.PIIClass: {missing!r}. Add each as a member "
        f'of PIIClass with NAME = "id". This drift causes run_pii_detection '
        f"tagged columns to silently drop their pii_class. See slice-3 B1 "
        f"in the Dennis review."
    )


def test_every_pii_class_member_corresponds_to_a_storm_detector() -> None:
    """Reverse direction: every PIIClass value matches a STORM detector.

    Prevents accidental enum growth with values STORM does not actually
    emit (aspirational tags that never fire would mislead the planner).
    """
    storm_ids = {
        detector_fn.__name__.removeprefix("detect_") for detector_fn in REGISTERED_DETECTORS
    }
    extras: list[str] = []
    for member in PIIClass:
        if member.value not in storm_ids:
            extras.append(member.value)
    assert not extras, (
        f"PIIClass has members not produced by STORM REGISTERED_DETECTORS: "
        f"{extras!r}. Either add a matching STORM detector or remove these "
        f"members. Aspirational enum entries that never fire mislead the planner."
    )
