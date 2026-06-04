"""decoy_engine.disguises: compliance-bundle definitions.

A Disguise is a YAML file that bundles together (a) the conditions under which
FORECAST should recommend it, and (b) the per-detector masking rules to apply
when the user accepts. Each Disguise is data, not code; adding one is a YAML
diff.

Public entry point:
    load_disguises() -> list[Disguise]   # loads every *.yaml in this package

Bones-only scope for `feature/storm-forecast-mvp`: the loader + schema + two
example bundles (`default.yaml`, `hipaa.yaml`) ship in this PR. The full 8-set
of compliance Disguises (PCI/GLBA/GDPR/CCPA/FERPA/SOX + the rest of HIPAA's
18 identifiers) follows in a separate PR: see DISGUISES.md.
"""

from decoy_engine.disguises.loader import load_disguises
from decoy_engine.disguises.schema import Disguise, FieldRule, TriggerSpec

__all__ = ["Disguise", "FieldRule", "TriggerSpec", "load_disguises"]
