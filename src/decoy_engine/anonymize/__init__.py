"""Anonymity enforcement: generalization of quasi-identifiers.

``decoy_engine.anonymize.hierarchies`` builds per-column generalization
hierarchies and the lattice of level vectors that a search walks. Nothing in
config, the planner or a run reaches this package; callers import it.

``type_family`` is the one value-identity rule for the anonymity code: cells
are the same value only when they are equal and of the same type family.
"""

from __future__ import annotations

from decoy_engine.anonymize._values import type_family

__all__ = ["type_family"]
