"""Anonymity enforcement: transforms that make a table meet a privacy model.

``decoy_engine.anonymize.microaggregation`` replaces numeric
quasi-identifiers by group means (MDAV) so the table is k-anonymous on them.
Nothing in config, the planner or a run reaches this package; callers import
it.
"""
