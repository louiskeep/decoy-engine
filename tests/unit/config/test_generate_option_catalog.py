"""GEN-CLI E1: `generate_option_catalog` is a names-only view of `GENERATE_OPTIONS`."""

from __future__ import annotations

import dataclasses

import pytest

from decoy_engine.config import GenerateTypeOptions, generate_option_catalog
from decoy_engine.config._generate_options import COMMON_OPTIONS, GENERATE_OPTIONS


def test_the_catalog_covers_every_registered_type_and_option() -> None:
    catalog = generate_option_catalog()
    assert list(catalog) == list(GENERATE_OPTIONS)
    for type_, spec in GENERATE_OPTIONS.items():
        entry = catalog[type_]
        assert entry.type == type_
        assert entry.required == (*spec.required, *spec.plan_required)
        assert entry.required_at_plan == spec.plan_required
        assert set(entry.required) | set(entry.optional) == set(spec.keys)
        assert not set(entry.required) & set(entry.optional)
        assert entry.common == tuple(COMMON_OPTIONS)


def test_keys_checked_at_plan_compile_are_listed_as_required() -> None:
    entry = generate_option_catalog()["derived_aggregate"]
    assert entry.required == ("op", "column")
    assert entry.required_at_plan == ("op", "column")
    assert not set(entry.required_at_plan) & set(entry.optional)
    # `via` is required with `from_table`, which no fixed selector value expresses, so the
    # per-parent check (`config.aggregate_problems`) refuses its absence.
    assert entry.optional == ("from_table", "via", "output_type")


def test_the_record_is_frozen_names_only() -> None:
    entry = generate_option_catalog()["faker"]
    assert isinstance(entry, GenerateTypeOptions)
    assert "faker_type" in entry.required
    assert {"locale", "faker_kwargs"} <= set(entry.optional)
    assert all(isinstance(n, str) for n in (*entry.required, *entry.optional, *entry.common))
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.type = "x"  # type: ignore[misc]
