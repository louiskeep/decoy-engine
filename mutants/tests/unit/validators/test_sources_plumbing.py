"""S1 (Sprint 2 honesty pack): `sources` plumbing through the validator framework.

TDD: written before the implementation. `validate()` and the registry callable
contract gain an additive, keyword-only `sources` parameter (D2). Existing
validators must ignore it byte-identically; a new validator can read it.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.validators import validate
from decoy_engine.validators._registry import _REGISTRY


class TestSourcesReachStubValidator:
    """A validator registered in-test receives the sources dict intact."""

    def test_stub_validator_sees_sources(self) -> None:
        seen: dict[str, Any] = {}

        def _stub(
            outputs: dict[str, pa.Table],
            entry: dict[str, Any],
            config: dict[str, Any],
            *,
            sources: dict[str, pa.Table] | None = None,
        ) -> tuple[Any, ...]:
            seen["sources"] = sources
            return ()

        _REGISTRY["_stub_sources_test"] = _stub
        try:
            outputs = {"t": pa.table({"x": [1]})}
            sources = {"t": pa.table({"x": [0]})}
            config: dict[str, Any] = {"validators": [{"name": "_stub_sources_test"}]}
            report = validate(outputs, config, sources=sources)
            assert report.passed is True
            assert seen["sources"] is sources
        finally:
            del _REGISTRY["_stub_sources_test"]

    def test_stub_validator_sees_none_when_sources_omitted(self) -> None:
        seen: dict[str, Any] = {"sources": "unset"}

        def _stub(
            outputs: dict[str, pa.Table],
            entry: dict[str, Any],
            config: dict[str, Any],
            *,
            sources: dict[str, pa.Table] | None = None,
        ) -> tuple[Any, ...]:
            seen["sources"] = sources
            return ()

        _REGISTRY["_stub_sources_test2"] = _stub
        try:
            outputs = {"t": pa.table({"x": [1]})}
            config: dict[str, Any] = {"validators": [{"name": "_stub_sources_test2"}]}
            validate(outputs, config)
            assert seen["sources"] is None
        finally:
            del _REGISTRY["_stub_sources_test2"]


class TestExistingValidatorsIgnoreSources:
    """The six pre-existing validators accept and ignore the sources kwarg."""

    def test_luhn_identical_report_with_and_without_sources(self) -> None:
        outputs = {"t": pa.table({"cc": ["4532015112830366"]})}
        config: dict[str, Any] = {"validators": [{"name": "luhn", "columns": {"t": ["cc"]}}]}
        without = validate(outputs, config)
        with_sources = validate(outputs, config, sources={"t": pa.table({"cc": ["x"]})})
        assert without.passed == with_sources.passed == True  # noqa: E712
        assert without.findings == with_sources.findings

    def test_fk_intact_identical_report_with_and_without_sources(self) -> None:
        outputs = {
            "orders": pa.table({"id": ["1", "2"]}),
            "items": pa.table({"order_id": ["1", "2"]}),
        }
        config: dict[str, Any] = {
            "validators": [{"name": "fk_intact"}],
            "relationships": [
                {
                    "parent": {"table": "orders", "columns": ["id"]},
                    "children": [{"table": "items", "columns": ["order_id"]}],
                    "orphan_policy": "fail",
                }
            ],
        }
        without = validate(outputs, config)
        with_sources = validate(outputs, config, sources={})
        assert without.passed == with_sources.passed == True  # noqa: E712
