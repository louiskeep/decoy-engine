"""engine-v2 S9 slice 2c: composite routing through the execution adapter.

A composite WorkNode writes all output columns in one generate_bundle pass; the
adapter resolves the generator via the factory + the whole-tuple namespace. Tests
the S8<->S9 integration: coherence (email local-part == masked first.last;
city/state/zip in the locality table) + reproducibility.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError, ExecutionResult, PandasExecutionAdapter
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._runner import WorkNode
from decoy_engine.execution._strategies._composite import CompositeHandler
from decoy_engine.generation.composite import load_locality_table
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.plan._types import ColumnSeed, GroupSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceBinding, NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_SEED = (0xABCDEF).to_bytes(8, "big")


def _col(
    provider: str,
    coherent_with: tuple[str, ...],
    provider_config: tuple[tuple[str, Any], ...] = (),
) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="<composite>",
        provider=provider,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=provider_config,
        coherent_with=coherent_with,
    )


def _group_per_column(
    provider: str, cols: tuple[str, ...], pc: tuple[tuple[str, Any], ...] = ()
) -> tuple[tuple[str, ColumnSeed], ...]:
    """A per_column tuple binding every column in `cols` to one composite provider."""
    return tuple((c, _col(provider, tuple(x for x in cols if x != c), pc)) for c in cols)


def _ns_registry(table: str, columns: tuple[str, ...], namespace: str) -> NamespaceRegistry:
    group = tuple(sorted(columns))
    return NamespaceRegistry(
        bindings=(NamespaceBinding(namespace=namespace, declared_by=((table, group),)),)
    )


def _plan(table: str, per_column: tuple[tuple[str, ColumnSeed], ...]) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=((table, TableSeed(per_column=per_column, per_group=())),),
        )
    )


def _run(plan: Any, table: pa.Table, ns_registry: NamespaceRegistry) -> ExecutionResult:
    return PandasExecutionAdapter().run_single(
        plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=ns_registry
    )


def _name_email_setup() -> tuple[Any, NamespaceRegistry]:
    cols = ("email", "first_name", "last_name")
    per_column = (
        ("first_name", _col("composite_name_email", ("last_name", "email"))),
        ("last_name", _col("composite_name_email", ("first_name", "email"))),
        ("email", _col("composite_name_email", ("first_name", "last_name"))),
    )
    plan = _plan("people", per_column)
    ns = _ns_registry("people", cols, "ne_ns")
    return plan, ns


class TestCompositeNameEmailRouting:
    def test_email_coherent_with_masked_name(self) -> None:
        src = pa.table(
            {"first_name": ["X", "Y"], "last_name": ["P", "Q"], "email": ["a@b.com", "c@d.com"]}
        )
        plan, ns = _name_email_setup()
        out = _run(plan, src, ns).output.to_pydict()
        for i in range(2):
            first = str(out["first_name"][i]).lower()
            last = str(out["last_name"][i]).lower()
            assert str(out["email"][i]).startswith(f"{first}.{last}@")

    def test_reproducible_across_runs(self) -> None:
        src = pa.table(
            {"first_name": ["X", "Y"], "last_name": ["P", "Q"], "email": ["a@b.com", "c@d.com"]}
        )
        plan, ns = _name_email_setup()
        out1 = _run(plan, src, ns).output.to_pydict()
        out2 = _run(plan, src, ns).output.to_pydict()
        assert out1 == out2


class TestCompositeCityStateZipRouting:
    def test_triples_in_locality_table(self) -> None:
        table_set = set(load_locality_table())
        cols = ("city", "state", "zip")
        per_column = (
            ("city", _col("composite_city_state_zip", ("state", "zip"))),
            ("state", _col("composite_city_state_zip", ("city", "zip"))),
            ("zip", _col("composite_city_state_zip", ("city", "state"))),
        )
        plan = _plan("locations", per_column)
        ns = _ns_registry("locations", cols, "loc_ns")
        src = pa.table({"city": ["Old", "Town"], "state": ["AA", "BB"], "zip": ["00000", "11111"]})
        out = _run(plan, src, ns).output.to_pydict()
        triples = list(zip(out["city"], out["state"], out["zip"], strict=True))
        assert all(t in table_set for t in triples)


class TestCompositeOutputColumnMissing:
    def test_missing_output_column_raises(self) -> None:
        # M1: a composite whose bundle includes a column absent from the source
        # frame must raise, not silently drop it. Source omits last_name.
        plan, ns = _name_email_setup()
        src = pa.table({"first_name": ["X"], "email": ["a@b.com"]})
        with pytest.raises(ExecutionError) as exc:
            _run(plan, src, ns)
        assert exc.value.code == "composite_output_column_missing"


def _ctx(ns_registry: NamespaceRegistry) -> StrategyContext:
    return StrategyContext(
        registry=_REG,
        pool_cache=PoolCache(),
        relationship_graph=_GRAPH,
        namespace_registry=ns_registry,
        job_seed=_SEED,
    )


class TestCompositeGuardCodes:
    """Machine-readable error codes for the handler's fail-closed guards."""

    def test_unresolved_namespace_code(self) -> None:
        # No namespace binding for the group: the handler must fail closed rather
        # than derive off a missing namespace.
        cols = ("a", "b")
        node = WorkNode(
            table="t",
            columns=cols,
            kind="composite",
            strategy="<composite>",
            provider="composite_name_email",
            plan_slice=_col("composite_name_email", ("b",)),
        )
        with pytest.raises(ExecutionError) as exc:
            CompositeHandler().run(
                pd.DataFrame({"a": ["x"], "b": ["y"]}), node, _ctx(NamespaceRegistry(bindings=()))
            )
        assert exc.value.code == "composite_namespace_unresolved"

    def test_non_columnseed_plan_slice_code(self) -> None:
        # A composite node must carry a ColumnSeed slice; a GroupSeed is rejected.
        cols = ("a", "b")
        node = WorkNode(
            table="t",
            columns=cols,
            kind="composite",
            strategy="<composite>",
            provider="composite_name_email",
            plan_slice=GroupSeed(namespace="ns", coherent_columns=cols),
        )
        with pytest.raises(ExecutionError) as exc:
            CompositeHandler().run(
                pd.DataFrame({"a": ["x"], "b": ["y"]}), node, _ctx(_ns_registry("t", cols, "ns"))
            )
        assert exc.value.code == "unsupported_strategy"

    def test_unknown_provider_code(self) -> None:
        # A provider name the handler does not route is a wiring error.
        cols = ("a", "b")
        node = WorkNode(
            table="t",
            columns=cols,
            kind="composite",
            strategy="<composite>",
            provider="composite_bogus",
            plan_slice=_col("composite_bogus", ("b",)),
        )
        with pytest.raises(ExecutionError) as exc:
            CompositeHandler().run(
                pd.DataFrame({"a": ["x"], "b": ["y"]}), node, _ctx(_ns_registry("t", cols, "ns"))
            )
        assert exc.value.code == "unsupported_strategy"

    def test_custom_bundle_not_list_code(self) -> None:
        # composite_custom's bundle declaration must be a list; a scalar is rejected.
        cols = ("a", "b")
        node = WorkNode(
            table="t",
            columns=cols,
            kind="composite",
            strategy="<composite>",
            provider="composite_custom",
            plan_slice=_col("composite_custom", ("b",), (("bundle", "notalist"),)),
        )
        with pytest.raises(ExecutionError) as exc:
            CompositeHandler().run(
                pd.DataFrame({"a": ["x"], "b": ["y"]}), node, _ctx(_ns_registry("t", cols, "ns"))
            )
        assert exc.value.code == "composite_custom_bundle_shape"


class TestCompositePersonRouting:
    def test_person_email_coherent_and_dob_present(self) -> None:
        # composite_person routing: the email local-part echoes the masked name,
        # which no other composite would produce for this column set.
        cols = ("dob", "email", "first_name", "last_name")
        per_column = _group_per_column("composite_person", cols)
        plan = _plan("people", per_column)
        ns = _ns_registry("people", cols, "p_ns")
        src = pa.table(
            {
                "dob": ["1", "2"],
                "email": ["a@b.com", "c@d.com"],
                "first_name": ["X", "Y"],
                "last_name": ["P", "Q"],
            }
        )
        out = _run(plan, src, ns).output.to_pydict()
        for i in range(2):
            first = str(out["first_name"][i]).lower()
            last = str(out["last_name"][i]).lower()
            assert str(out["email"][i]).startswith(f"{first}.{last}@")
            assert out["dob"][i] is not None


class TestCompositeAddressRouting:
    def test_city_state_zip_triple_in_locality(self) -> None:
        # composite_address routing: the (city, state, zip) it writes is a real
        # locality triple and it also fills street_address.
        table_set = set(load_locality_table())
        cols = ("city", "state", "street_address", "zip")
        per_column = _group_per_column("composite_address", cols)
        plan = _plan("locations", per_column)
        ns = _ns_registry("locations", cols, "a_ns")
        src = pa.table(
            {
                "city": ["Old", "Town"],
                "state": ["AA", "BB"],
                "street_address": ["1 A", "2 B"],
                "zip": ["00000", "11111"],
            }
        )
        out = _run(plan, src, ns).output.to_pydict()
        triples = list(zip(out["city"], out["state"], out["zip"], strict=True))
        assert all(t in table_set for t in triples)
        assert all(street for street in out["street_address"])


class TestCompositeProviderRouting:
    def test_provider_bundle_written_and_reproducible(self) -> None:
        # composite_provider routing: all three declared columns get non-empty,
        # run-stable values (a mis-route would raise output_column_missing).
        cols = ("npi", "practice_address", "provider_name")
        per_column = _group_per_column("composite_provider", cols)
        plan = _plan("providers", per_column)
        ns = _ns_registry("providers", cols, "pr_ns")
        src = pa.table(
            {"npi": ["1", "2"], "practice_address": ["a", "b"], "provider_name": ["c", "d"]}
        )
        out1 = _run(plan, src, ns).output.to_pydict()
        out2 = _run(plan, src, ns).output.to_pydict()
        assert out1 == out2
        assert all(out1["npi"])
        assert all(out1["provider_name"])
        assert all(out1["practice_address"])


class TestCompositeCustomRouting:
    def test_custom_bundle_produces_declared_columns(self) -> None:
        # composite_custom routing: the declared slot columns are written from the
        # bundle's per-slot providers, run-stable.
        bundle = [
            {"column": "a", "provider": "person_first_name"},
            {"column": "b", "provider": "person_last_name"},
        ]
        cols = ("a", "b")
        pc = (("bundle", bundle),)
        per_column = _group_per_column("composite_custom", cols, pc)
        plan = _plan("t", per_column)
        ns = _ns_registry("t", cols, "c_ns")
        src = pa.table({"a": ["x", "y"], "b": ["p", "q"]})
        out1 = _run(plan, src, ns).output.to_pydict()
        out2 = _run(plan, src, ns).output.to_pydict()
        assert out1 == out2
        assert all(out1["a"])
        assert all(out1["b"])


class _StubAdapter:
    """A BackendAdapter that generates one constant sentinel, reusing the real
    capability matrix so the pool path accepts it as a drop-in sub-provider."""

    backend_type = "test_stub"
    backend_version = "stub-1"

    def __init__(self, value: str) -> None:
        self._value = value

    def generate(self, provider: str, *, spec: Any, source_value: Any = None) -> str:
        return self._value

    def generate_batch(self, provider: str, *, spec: Any, count: int) -> list[str]:
        return [self._value] * count

    def capability_matrix(self, provider: str) -> Any:
        return get_default_registry().get_capabilities(provider)


def _ctx_with(registry: Any, ns_registry: NamespaceRegistry) -> StrategyContext:
    return StrategyContext(
        registry=registry,
        pool_cache=PoolCache(),
        relationship_graph=_GRAPH,
        namespace_registry=ns_registry,
        job_seed=_SEED,
    )


class TestCompositeHonorsCustomRegistry:
    """Each route consumes `ctx.registry` (a public per-pipeline knob via
    `run_pipeline(registry=...)` -> ProviderRegistry.override). Overriding a
    sub-provider with a sentinel adapter must surface in the composite output;
    a `registry=None` regression at a factory call site would silently fall
    back to the default registry and ignore the override."""

    @pytest.mark.parametrize(
        ("provider", "cols", "src_cols", "sub_provider", "out_col", "pc"),
        [
            (
                "composite_name_email",
                ("email", "first_name", "last_name"),
                {
                    "first_name": ["X", "Y"],
                    "last_name": ["P", "Q"],
                    "email": ["a@b.com", "c@d.com"],
                },
                "person_first_name",
                "first_name",
                (),
            ),
            (
                "composite_person",
                ("dob", "email", "first_name", "last_name"),
                {
                    "dob": ["1", "2"],
                    "email": ["a@b.com", "c@d.com"],
                    "first_name": ["X", "Y"],
                    "last_name": ["P", "Q"],
                },
                "person_first_name",
                "first_name",
                (),
            ),
            (
                "composite_address",
                ("city", "state", "street_address", "zip"),
                {
                    "city": ["Old", "Town"],
                    "state": ["AA", "BB"],
                    "street_address": ["1 A", "2 B"],
                    "zip": ["00000", "11111"],
                },
                "address_street",
                "street_address",
                (),
            ),
            (
                "composite_provider",
                ("npi", "practice_address", "provider_name"),
                {"npi": ["1", "2"], "practice_address": ["a", "b"], "provider_name": ["c", "d"]},
                "person_name",
                "provider_name",
                (),
            ),
            (
                "composite_custom",
                ("a", "b"),
                {"a": ["x", "y"], "b": ["p", "q"]},
                "person_first_name",
                "a",
                (
                    (
                        "bundle",
                        [
                            {"column": "a", "provider": "person_first_name"},
                            {"column": "b", "provider": "person_last_name"},
                        ],
                    ),
                ),
            ),
        ],
    )
    def test_route_output_reflects_registry_override(
        self,
        provider: str,
        cols: tuple[str, ...],
        src_cols: dict[str, list[str]],
        sub_provider: str,
        out_col: str,
        pc: tuple[tuple[str, Any], ...],
    ) -> None:
        node = WorkNode(
            table="t",
            columns=cols,
            kind="composite",
            strategy="<composite>",
            provider=provider,
            plan_slice=_col(provider, tuple(c for c in cols if c != out_col), pc),
        )
        ns = _ns_registry("t", cols, "reg_ns")
        stub = _StubAdapter("ZZZ")
        registry = get_default_registry().override(
            sub_provider, stub, stub.capability_matrix(sub_provider)
        )
        df = pd.DataFrame(src_cols)
        out, _ = CompositeHandler().run(df, node, _ctx_with(registry, ns))
        assert out[out_col].tolist() == ["ZZZ"] * len(df)


class TestCompositeSourceKeying:
    def test_output_depends_on_first_sorted_column(self) -> None:
        # Deterministic mode keys the bundle on the first sorted column (email);
        # holding the other columns fixed and varying only email must change the
        # output, which pins both the source-column index and the deterministic
        # (source-keyed, not pooled) path.
        plan, ns = _name_email_setup()
        base = {"first_name": ["Fa", "Fb"], "last_name": ["La", "Lb"]}
        src_a = pa.table({**base, "email": ["a@b.com", "c@d.com"]})
        src_b = pa.table({**base, "email": ["e@f.com", "g@h.com"]})
        out_a = _run(plan, src_a, ns).output.to_pydict()
        out_b = _run(plan, src_b, ns).output.to_pydict()
        assert out_a != out_b


class TestCompositeProviderConfigFlow:
    def test_email_format_from_provider_config(self) -> None:
        # provider_config flows into the generator via ProviderSpec.extra; an
        # email_format override changes the join character between first and last.
        cols = ("email", "first_name", "last_name")
        pc = (("email_format", "{first}_{last}@{domain}"),)
        per_column = (
            ("first_name", _col("composite_name_email", ("last_name", "email"), pc)),
            ("last_name", _col("composite_name_email", ("first_name", "email"), pc)),
            ("email", _col("composite_name_email", ("first_name", "last_name"), pc)),
        )
        plan = _plan("people", per_column)
        ns = _ns_registry("people", cols, "ne_ns")
        src = pa.table(
            {"first_name": ["X", "Y"], "last_name": ["P", "Q"], "email": ["a@b.com", "c@d.com"]}
        )
        out = _run(plan, src, ns).output.to_pydict()
        for i in range(2):
            first = str(out["first_name"][i]).lower()
            last = str(out["last_name"][i]).lower()
            assert str(out["email"][i]).startswith(f"{first}_{last}@")
