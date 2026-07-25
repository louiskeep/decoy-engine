"""engine-v2 S8 slice 2: composite namespace auto-binding + row-8 wiring check.

build_namespace_registry only reads `profile.relationships`, so a duck-typed
SimpleNamespace stands in for Profile here.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from decoy_engine.generation.composite import composite_wiring_consistent
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile._types import Relationship
from decoy_engine.relationships import build_namespace_registry

_NE_GROUP = tuple(sorted(("first_name", "last_name", "email")))
_CSZ_GROUP = tuple(sorted(("city", "state", "zip")))


def _profile(*relationships: Relationship) -> Any:
    return SimpleNamespace(relationships=tuple(relationships))


def _name_email_config(namespace: str | None = None) -> dict[str, Any]:
    cols: list[dict[str, Any]] = [
        {
            "name": "first_name",
            "provider": "composite_name_email",
            "coherent_with": ["last_name", "email"],
        },
        {
            "name": "last_name",
            "provider": "composite_name_email",
            "coherent_with": ["first_name", "email"],
        },
        {
            "name": "email",
            "provider": "composite_name_email",
            "coherent_with": ["first_name", "last_name"],
        },
    ]
    if namespace is not None:
        cols[0]["namespace"] = namespace
    return {"tables": [{"name": "people", "columns": cols}]}


class TestCompositeNamespaceAutoBinding:
    def test_whole_tuple_binds(self) -> None:
        reg = build_namespace_registry(_name_email_config(), _profile())
        assert reg.for_column("people", _NE_GROUP) is not None

    def test_per_column_lookup_returns_none(self) -> None:
        reg = build_namespace_registry(_name_email_config(), _profile())
        assert reg.for_column("people", ("first_name",)) is None

    def test_explicit_namespace_honored(self) -> None:
        reg = build_namespace_registry(_name_email_config(namespace="people_ns"), _profile())
        assert reg.for_column("people", _NE_GROUP) == "people_ns"

    def test_two_composites_independent_namespaces(self) -> None:
        config = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "first_name",
                            "provider": "composite_name_email",
                            "coherent_with": ["last_name", "email"],
                        },
                        {
                            "name": "last_name",
                            "provider": "composite_name_email",
                            "coherent_with": ["first_name", "email"],
                        },
                        {
                            "name": "email",
                            "provider": "composite_name_email",
                            "coherent_with": ["first_name", "last_name"],
                        },
                        {
                            "name": "city",
                            "provider": "composite_city_state_zip",
                            "coherent_with": ["state", "zip"],
                        },
                        {
                            "name": "state",
                            "provider": "composite_city_state_zip",
                            "coherent_with": ["city", "zip"],
                        },
                        {
                            "name": "zip",
                            "provider": "composite_city_state_zip",
                            "coherent_with": ["city", "state"],
                        },
                    ],
                }
            ]
        }
        reg = build_namespace_registry(config, _profile())
        ne = reg.for_column("t", _NE_GROUP)
        csz = reg.for_column("t", _CSZ_GROUP)
        assert ne is not None and csz is not None and ne != csz

    def test_no_regression_fk_and_explicit_namespaces(self) -> None:
        config = {
            "namespaces": {"explicit_ns": {"declared_by": ["people.plain_col"]}},
            "tables": _name_email_config()["tables"],
        }
        rel = Relationship(
            parent_table="t",
            parent_columns=("id",),
            child_table="c",
            child_columns=("tid",),
            namespace="fk_ns",
        )
        reg = build_namespace_registry(config, _profile(rel))
        assert reg.for_column("people", ("plain_col",)) == "explicit_ns"
        assert reg.for_column("t", ("id",)) == "fk_ns"
        assert reg.for_column("c", ("tid",)) == "fk_ns"
        # The composite still binds its whole tuple alongside the FK + explicit ones.
        assert reg.for_column("people", _NE_GROUP) is not None


class TestRow8CompositeWiring:
    def test_clean_config_passes(self) -> None:
        config = _name_email_config()
        reg = build_namespace_registry(config, _profile())
        composite_wiring_consistent(config, reg)  # no raise

    def test_coherent_with_missing_column_raises(self) -> None:
        config = {
            "tables": [
                {
                    "name": "people",
                    "columns": [
                        {
                            "name": "first_name",
                            "provider": "composite_name_email",
                            "coherent_with": ["nonexistent"],
                        }
                    ],
                }
            ]
        }
        reg = build_namespace_registry(config, _profile())
        with pytest.raises(PlanCompileError) as exc:
            composite_wiring_consistent(config, reg)
        assert exc.value.code == "composite_wiring_inconsistent"

    def test_mixed_provider_group_raises(self) -> None:
        config = {
            "tables": [
                {
                    "name": "people",
                    "columns": [
                        {
                            "name": "first_name",
                            "provider": "composite_name_email",
                            "coherent_with": ["last_name", "email"],
                        },
                        {
                            "name": "last_name",
                            "provider": "person_last_name",
                            "coherent_with": ["first_name", "email"],
                        },
                        {
                            "name": "email",
                            "provider": "composite_name_email",
                            "coherent_with": ["first_name", "last_name"],
                        },
                    ],
                }
            ]
        }
        reg = build_namespace_registry(config, _profile())
        with pytest.raises(PlanCompileError) as exc:
            composite_wiring_consistent(config, reg)
        assert exc.value.code == "composite_wiring_inconsistent"

    def test_output_columns_mismatch_raises(self) -> None:
        config = {
            "tables": [
                {
                    "name": "people",
                    "columns": [
                        {
                            "name": "fname",
                            "provider": "composite_name_email",
                            "coherent_with": ["lname"],
                        },
                        {
                            "name": "lname",
                            "provider": "composite_name_email",
                            "coherent_with": ["fname"],
                        },
                    ],
                }
            ]
        }
        reg = build_namespace_registry(config, _profile())
        with pytest.raises(PlanCompileError) as exc:
            composite_wiring_consistent(config, reg)
        assert exc.value.code == "composite_wiring_inconsistent"
