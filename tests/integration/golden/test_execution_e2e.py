"""engine-v2 S9 slice 3d: golden-fixture end-to-end (compile_plan -> run).

The unit tests build plans by hand from `SimpleNamespace`; this exercises the
REAL planning path on the real golden fixtures:

    profile -> compile_plan -> relationship graph + namespace registry
            -> PandasExecutionAdapter.run -> post-mask invariant assertions

per Dennis's S9 end-of-sprint option (a). It does NOT build the S10 validator;
assertions are direct invariant checks over the masked output. Filler columns
(non-key, non-composite) are left out of the config so they pass through; only
the columns each invariant depends on are masked. FK key columns are masked with
a deterministic poolable provider (the masked VALUE is irrelevant to FK
referential integrity, which the resolver preserves via the parent map).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError, ExecutionResult, PandasExecutionAdapter
from decoy_engine.generation.composite import load_locality_table
from decoy_engine.plan import compile_plan
from decoy_engine.profile import ColumnProfile, Profile, Relationship, TableProfile
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import (
    RelationshipGraph,
    build_relationship_graph,
    check_orphan_fk_policy_completeness,
)
from decoy_engine.relationships._namespace import NamespaceRegistry, build_namespace_registry

GOLDEN = Path(__file__).resolve().parent.parent.parent / "fixtures" / "golden"
_VERSION = "0.1.0"


def _column_profile(name: str, df: pd.DataFrame, **flags: Any) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype="object",
        row_count=len(df),
        null_count=int(df[name].isna().sum()),
        distinct_count=int(df[name].nunique()),
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=flags.get("pk", False),
        is_fk=flags.get("fk", False),
        fk_target=flags.get("fk_target"),
        pii_class=None,
    )


def _load_csvs(fixture: str, names: tuple[str, ...]) -> dict[str, pd.DataFrame]:
    # dtype=str keeps IDs/zips stable (no int coercion that would drop leading
    # zeros or split int/float across parent/child).
    root = GOLDEN / fixture
    return {n: pd.read_csv(root / f"{n}.csv", dtype=str) for n in names}


def _sources(frames: dict[str, pd.DataFrame]) -> dict[str, pa.Table]:
    return {t: pa.Table.from_pandas(df, preserve_index=False) for t, df in frames.items()}


def _run(
    profile: Profile, config: dict[str, Any], frames: dict[str, pd.DataFrame]
) -> ExecutionResult:
    plan = compile_plan(config, profile, decoy_engine_version=_VERSION)
    ns_registry: NamespaceRegistry = build_namespace_registry(config, profile)
    if profile.relationships:
        lookup = check_orphan_fk_policy_completeness(config, profile.relationships)
        graph = build_relationship_graph(
            profile.relationships, namespace_registry=ns_registry, orphan_policy_lookup=lookup
        )
    else:
        graph = RelationshipGraph(edges=(), ordering=())
    return PandasExecutionAdapter().run(
        plan,
        _sources(frames),
        registry=get_default_registry(),
        relationship_graph=graph,
        namespace_registry=ns_registry,
    )


def _faker_col(name: str, namespace: str) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": namespace,
    }


# --------------------------------------------------------------------------
# orphan_fk: single-column FK + all four OrphanPolicy values end-to-end.
# --------------------------------------------------------------------------


def _orphan_fk_profile(frames: dict[str, pd.DataFrame]) -> Profile:
    cust, ords = frames["customers"], frames["orders"]
    customers = TableProfile(
        name="customers",
        row_count=len(cust),
        columns=(
            _column_profile("customer_id", cust, pk=True),
            _column_profile("name", cust),
            _column_profile("email", cust),
        ),
    )
    orders = TableProfile(
        name="orders",
        row_count=len(ords),
        columns=(
            _column_profile("order_id", ords, pk=True),
            _column_profile("customer_id", ords, fk=True, fk_target=("customers", "customer_id")),
            _column_profile("order_date", ords),
            _column_profile("amount", ords),
        ),
    )
    return Profile(
        schema_version=1,
        tables=(customers, orders),
        relationships=(
            Relationship(
                parent_table="customers",
                parent_columns=("customer_id",),
                child_table="orders",
                child_columns=("customer_id",),
                namespace="customer_identity",
            ),
        ),
        profiled_at=datetime(2026, 5, 28),
        decoy_engine_version=_VERSION,
    )


def _orphan_fk_config(policy: str) -> dict[str, Any]:
    return {
        "global_settings": {"seed": 7},
        "tables": [
            {"name": "customers", "columns": [_faker_col("customer_id", "customer_identity")]},
            {"name": "orders", "columns": [_faker_col("customer_id", "customer_identity")]},
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["customer_id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": policy,
                "namespace": "customer_identity",
            }
        ],
    }


class TestOrphanFkE2E:
    def _setup(self) -> tuple[Profile, dict[str, pd.DataFrame], list[int], dict[str, str]]:
        frames = _load_csvs("orphan_fk", ("customers", "orders"))
        profile = _orphan_fk_profile(frames)
        cust_ids = set(frames["customers"]["customer_id"])
        orders = list(frames["orders"]["customer_id"])
        orphans = [i for i, c in enumerate(orders) if c not in cust_ids]
        assert orphans, "orphan_fk fixture must contain orphan rows"
        return profile, frames, orphans, {}

    def test_preserve_keeps_orphan_and_preserves_ri(self) -> None:
        profile, frames, orphans, _ = self._setup()
        res = _run(profile, _orphan_fk_config("preserve"), frames)
        cust = res.outputs["customers"].to_pydict()
        ords = res.outputs["orders"].to_pydict()
        src_orders = list(frames["orders"]["customer_id"])
        pmap = dict(zip(frames["customers"]["customer_id"], cust["customer_id"], strict=True))
        for i in range(len(src_orders)):
            if i in orphans:
                assert ords["customer_id"][i] == src_orders[i]  # orphan preserved
            else:
                assert ords["customer_id"][i] == pmap[src_orders[i]]  # masked parent
        assert cust["customer_id"][0] != frames["customers"]["customer_id"].iloc[0]

    def test_warn_emits_one_aggregated_warning(self) -> None:
        profile, frames, orphans, _ = self._setup()
        res = _run(profile, _orphan_fk_config("warn"), frames)
        codes = [w.code for w in res.warnings]
        assert codes.count("orphan_fk") == 1
        assert res.warnings[0].detail["orphan_rows"] == len(orphans)

    def test_fail_raises(self) -> None:
        profile, frames, _, _ = self._setup()
        with pytest.raises(ExecutionError) as exc:
            _run(profile, _orphan_fk_config("fail"), frames)
        assert exc.value.code == "orphan_fk_violation"

    def test_remap_masks_orphan(self) -> None:
        profile, frames, orphans, _ = self._setup()
        res = _run(profile, _orphan_fk_config("remap"), frames)
        ords = res.outputs["orders"].to_pydict()
        src_orders = list(frames["orders"]["customer_id"])
        assert ords["customer_id"][orphans[0]] != src_orders[orphans[0]]

    def test_two_runs_byte_identical(self) -> None:
        profile, frames, _, _ = self._setup()
        a = _run(profile, _orphan_fk_config("preserve"), frames).outputs["orders"].to_pydict()
        b = _run(profile, _orphan_fk_config("preserve"), frames).outputs["orders"].to_pydict()
        assert a == b


# --------------------------------------------------------------------------
# composite_key: composite-PK parent, composite-FK child resolved as one tuple.
# --------------------------------------------------------------------------

_CK = ("member_id", "plan_id", "effective_date")


class TestCompositeKeyE2E:
    def test_every_masked_claim_tuple_is_in_masked_enrollments(self) -> None:
        frames = _load_csvs("composite_key", ("enrollments", "claims"))
        enr, clm = frames["enrollments"], frames["claims"]
        enrollments = TableProfile(
            name="enrollments",
            row_count=len(enr),
            columns=tuple(_column_profile(c, enr, pk=(c in _CK)) for c in enr.columns),
        )
        claims = TableProfile(
            name="claims",
            row_count=len(clm),
            columns=tuple(
                _column_profile(
                    c, clm, fk=(c in _CK), fk_target=("enrollments", c) if c in _CK else None
                )
                for c in clm.columns
            ),
        )
        profile = Profile(
            schema_version=1,
            tables=(enrollments, claims),
            relationships=(
                Relationship(
                    parent_table="enrollments",
                    parent_columns=_CK,
                    child_table="claims",
                    child_columns=_CK,
                    namespace="enrollment_identity",
                ),
            ),
            profiled_at=datetime(2026, 5, 28),
            decoy_engine_version=_VERSION,
        )
        config = {
            "global_settings": {"seed": 7},
            "tables": [
                {"name": "enrollments", "columns": [_faker_col(c, f"ns_{c}") for c in _CK]},
                {"name": "claims", "columns": []},  # composite FK cols are per_group
            ],
            "relationships": [
                {
                    "parent": {"table": "enrollments", "columns": list(_CK)},
                    "children": [{"table": "claims", "columns": list(_CK)}],
                    "orphan_policy": "fail",  # manifest expected_orphans: 0
                    "namespace": "enrollment_identity",
                }
            ],
        }
        res = _run(profile, config, frames)
        eo = res.outputs["enrollments"].to_pydict()
        co = res.outputs["claims"].to_pydict()
        enrollment_tuples = set(
            zip(eo["member_id"], eo["plan_id"], eo["effective_date"], strict=True)
        )
        claim_tuples = list(zip(co["member_id"], co["plan_id"], co["effective_date"], strict=True))
        assert all(t in enrollment_tuples for t in claim_tuples)  # composite FK RI
        assert eo["member_id"][0] != enr["member_id"].iloc[0]  # parent tuple masked


# --------------------------------------------------------------------------
# composite_coherence: post-mask coherence (closes Session 33 JC3 / Session 34 M2
# POST-mask deferral through the real execution path).
# --------------------------------------------------------------------------


def _composite_col(
    name: str, provider: str, coherent_with: tuple[str, ...], ns: str
) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "<composite>",
        "provider": provider,
        "deterministic": True,
        "namespace": ns,
        "coherent_with": list(coherent_with),
    }


class TestCompositeCoherenceE2E:
    def _run_fixture(self) -> ExecutionResult:
        frames = _load_csvs("composite_coherence", ("people", "locations"))
        ppl, loc = frames["people"], frames["locations"]
        profile = Profile(
            schema_version=1,
            tables=(
                TableProfile(
                    name="people",
                    row_count=len(ppl),
                    columns=tuple(_column_profile(c, ppl) for c in ppl.columns),
                ),
                TableProfile(
                    name="locations",
                    row_count=len(loc),
                    columns=tuple(_column_profile(c, loc) for c in loc.columns),
                ),
            ),
            relationships=(),
            profiled_at=datetime(2026, 5, 28),
            decoy_engine_version=_VERSION,
        )
        config = {
            "global_settings": {"seed": 7},
            "tables": [
                {
                    "name": "people",
                    "columns": [
                        _composite_col(
                            "first_name", "composite_name_email", ("last_name", "email"), "ne"
                        ),
                        _composite_col(
                            "last_name", "composite_name_email", ("first_name", "email"), "ne"
                        ),
                        _composite_col(
                            "email", "composite_name_email", ("first_name", "last_name"), "ne"
                        ),
                    ],
                },
                {
                    "name": "locations",
                    "columns": [
                        _composite_col("city", "composite_city_state_zip", ("state", "zip"), "loc"),
                        _composite_col("state", "composite_city_state_zip", ("city", "zip"), "loc"),
                        _composite_col("zip", "composite_city_state_zip", ("city", "state"), "loc"),
                    ],
                },
            ],
        }
        return _run(profile, config, frames)

    def test_email_localpart_is_masked_first_dot_last(self) -> None:
        res = self._run_fixture()
        people = res.outputs["people"].to_pydict()
        for i in range(len(people["email"])):
            local = str(people["email"][i]).split("@", 1)[0]
            expected = f"{people['first_name'][i]}.{people['last_name'][i]}".lower()
            assert local == expected

    def test_location_triples_in_locality_table(self) -> None:
        res = self._run_fixture()
        loc = res.outputs["locations"].to_pydict()
        table = set(load_locality_table())
        triples = list(zip(loc["city"], loc["state"], loc["zip"], strict=True))
        assert all(t in table for t in triples)
