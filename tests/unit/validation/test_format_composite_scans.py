"""engine-v2 S10 slice 3b: format_rules + composite_coherence scans."""

from __future__ import annotations

from types import SimpleNamespace

import pyarrow as pa

from decoy_engine.generation.composite import load_locality_table
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from decoy_engine.validation.post._checks._composite_coherence import run_composite_coherence
from decoy_engine.validation.post._checks._format_rules import run_format_rules
from decoy_engine.validation.post._scan import ScanContext

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())


def _seed(provider: str) -> ColumnSeed:
    return ColumnSeed(
        namespace="ns",
        strategy="faker",
        provider=provider,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


def _ctx(
    table: str, plan_cols: tuple[tuple[str, ColumnSeed], ...], output: pa.Table
) -> ScanContext:
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x00" * 8,
            per_table=((table, TableSeed(per_column=plan_cols, per_group=())),),
        )
    )
    return ScanContext(
        plan=plan,  # type: ignore[arg-type]
        outputs={table: output},
        sources={table: output},
        profile=SimpleNamespace(tables=()),  # type: ignore[arg-type]
        registry=_REG,
        relationship_graph=_GRAPH,
        namespace_registry=_NS,
    )


class TestFormatRules:
    def test_valid_regulated_format_passes(self) -> None:
        ctx = _ctx(
            "people",
            (("ssn", _seed("synthetic_ssn")),),
            pa.table({"ssn": ["123-45-6789", "000-12-3456"]}),
        )
        outcome = run_format_rules(ctx)
        assert outcome.failed is False
        assert outcome.warnings == ()

    def test_malformed_regulated_format_hard_fails(self) -> None:
        ctx = _ctx(
            "people",
            (("ssn", _seed("synthetic_ssn")),),
            pa.table({"ssn": ["123-45-6789", "not-an-ssn"]}),
        )
        outcome = run_format_rules(ctx)
        assert outcome.failed is True  # synthetic_ssn is regulated (blocklist_validators)
        assert any(w.code == "format_rule_violation" for w in outcome.warnings)

    def test_provider_without_format_regex_skipped(self) -> None:
        ctx = _ctx(
            "people",
            (("email", _seed("person_email")),),  # faker -> format_regex is None
            pa.table({"email": ["literally anything", "x"]}),
        )
        outcome = run_format_rules(ctx)
        assert outcome.failed is False
        assert outcome.warnings == ()


class TestCompositeCoherence:
    def test_name_email_coherent_passes(self) -> None:
        cols = ("first_name", "last_name", "email")
        ctx = _ctx(
            "people",
            tuple((c, _seed("composite_name_email")) for c in cols),
            pa.table(
                {
                    "first_name": ["Anna"],
                    "last_name": ["Lee"],
                    "email": ["anna.lee@x.com"],
                }
            ),
        )
        outcome = run_composite_coherence(ctx)
        assert outcome.failed is False
        report = outcome.composite_coherence["people:composite_name_email"]
        assert report.total_rows == 1 and report.incoherent_rows == 0

    def test_name_email_incoherent_fails(self) -> None:
        cols = ("first_name", "last_name", "email")
        ctx = _ctx(
            "people",
            tuple((c, _seed("composite_name_email")) for c in cols),
            pa.table(
                {"first_name": ["Anna"], "last_name": ["Lee"], "email": ["wrong.handle@x.com"]}
            ),
        )
        outcome = run_composite_coherence(ctx)
        assert outcome.failed is True
        assert outcome.composite_coherence["people:composite_name_email"].incoherent_rows == 1

    def test_city_state_zip_in_table_passes(self) -> None:
        city, state, zip_code = next(iter(load_locality_table()))
        cols = ("city", "state", "zip")
        ctx = _ctx(
            "locations",
            tuple((c, _seed("composite_city_state_zip")) for c in cols),
            pa.table({"city": [city], "state": [state], "zip": [zip_code]}),
        )
        outcome = run_composite_coherence(ctx)
        assert outcome.failed is False
        assert (
            outcome.composite_coherence["locations:composite_city_state_zip"].incoherent_rows == 0
        )

    def test_city_state_zip_not_in_table_fails(self) -> None:
        cols = ("city", "state", "zip")
        ctx = _ctx(
            "locations",
            tuple((c, _seed("composite_city_state_zip")) for c in cols),
            pa.table({"city": ["Nowheresville"], "state": ["ZZ"], "zip": ["00000"]}),
        )
        outcome = run_composite_coherence(ctx)
        assert outcome.failed is True
        assert (
            outcome.composite_coherence["locations:composite_city_state_zip"].incoherent_rows == 1
        )
