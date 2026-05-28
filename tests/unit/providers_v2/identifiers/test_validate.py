"""Compile-check row 9 tests (deterministic_namespace_completeness)."""

from __future__ import annotations

import pytest

from decoy_engine.plan import PlanCompileError
from decoy_engine.providers_v2.identifiers import deterministic_namespace_completeness


class TestRow9Check:
    def test_deterministic_without_namespace_raises(self) -> None:
        config = {
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {
                            "name": "ssn",
                            "deterministic": True,
                            # namespace missing
                        }
                    ],
                }
            ]
        }
        with pytest.raises(PlanCompileError) as excinfo:
            deterministic_namespace_completeness(config)
        assert excinfo.value.code == "deterministic_namespace_missing"

    def test_deterministic_with_empty_namespace_raises(self) -> None:
        config = {
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {
                            "name": "ssn",
                            "deterministic": True,
                            "namespace": "",
                        }
                    ],
                }
            ]
        }
        with pytest.raises(PlanCompileError) as excinfo:
            deterministic_namespace_completeness(config)
        assert excinfo.value.code == "deterministic_namespace_missing"

    def test_deterministic_with_namespace_passes(self) -> None:
        config = {
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {
                            "name": "ssn",
                            "deterministic": True,
                            "namespace": "customer_identity",
                        }
                    ],
                }
            ]
        }
        deterministic_namespace_completeness(config)  # no raise

    def test_non_deterministic_no_namespace_passes(self) -> None:
        config = {
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {
                            "name": "name",
                            "deterministic": False,
                        }
                    ],
                }
            ]
        }
        deterministic_namespace_completeness(config)  # no raise
