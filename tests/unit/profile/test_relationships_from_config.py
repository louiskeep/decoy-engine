"""profile_relationships_from_config: the public config-to-Relationship converter (CLI-QA1 E-b)."""

from __future__ import annotations

import pytest

import decoy_engine.profile as profile_pkg
from decoy_engine.profile import Relationship, profile_relationships_from_config


def _rel(**over):
    base = {
        "parent": {"table": "patients", "columns": ["id"]},
        "children": [{"table": "visits", "columns": ["patient_id"]}],
    }
    base.update(over)
    return base


def test_exported_and_private_name_gone():
    from decoy_engine.profile import _source

    assert "profile_relationships_from_config" in profile_pkg.__all__
    assert not hasattr(_source, "_build_relationships")
    import decoy_engine
    import decoy_engine.profiling
    import decoy_engine.sdk

    for mod in (decoy_engine, decoy_engine.sdk, decoy_engine.profiling):
        assert not hasattr(mod, "profile_relationships_from_config")
    assert (
        profile_pkg.profile_relationships_from_config is _source.profile_relationships_from_config
    )


def test_empty_returns_empty_tuple():
    assert profile_relationships_from_config([]) == ()


def test_returns_tuple_of_relationships_one_per_child():
    cfg = [
        _rel(
            namespace="ns",
            children=[
                {"table": "visits", "columns": ["patient_id"]},
                {"table": "labs", "columns": ["pid"]},
            ],
        )
    ]
    out = profile_relationships_from_config(cfg)
    assert isinstance(out, tuple)
    assert out == (
        Relationship("patients", ("id",), "visits", ("patient_id",), namespace="ns"),
        Relationship("patients", ("id",), "labs", ("pid",), namespace="ns"),
    )


def test_missing_namespace_is_none():
    (r,) = profile_relationships_from_config([_rel()])
    assert r.namespace is None


def test_malformed_entries_are_skipped():
    cfg = ["x", {"parent": "p", "children": []}, {"parent": {"table": 3}, "children": []}, _rel()]
    assert len(profile_relationships_from_config(cfg)) == 1


def test_unequal_composite_columns_raise_value_error():
    bad = _rel(
        parent={"table": "patients", "columns": ["a", "b"]},
        children=[{"table": "visits", "columns": ["a"]}],
    )
    with pytest.raises(ValueError):
        profile_relationships_from_config([bad])


def test_empty_column_lists_raise_value_error():
    bad = _rel(
        parent={"table": "patients", "columns": []},
        children=[{"table": "visits", "columns": []}],
    )
    with pytest.raises(ValueError):
        profile_relationships_from_config([bad])
