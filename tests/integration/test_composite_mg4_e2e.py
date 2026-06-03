"""MG-4 integration (2026-05-31): full pipeline through PandasExecutionAdapter
for the 4 new composites.

Dennis MG-4 gate B1 + H1 close: the runtime composite dispatcher in
`execution/_strategies/_composite.py` AND the post-mask coherence
audit at `validation/post/_checks/_composite_coherence.py` both
needed extension for the new composites. These cells exercise the
real plan-compile + adapter path so a future regression on either
surface hits one of these cells (instead of waiting for a user
report).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd
import pyarrow as pa

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.generation.composite import load_locality_table
from decoy_engine.plan import compile_plan
from decoy_engine.profile import ColumnProfile, Profile, TableProfile
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import (
    RelationshipGraph,
    build_relationship_graph,
    check_orphan_fk_policy_completeness,
)
from decoy_engine.relationships._namespace import (
    build_namespace_registry,
)
from decoy_engine.storm.detectors import _npi_valid

_VERSION = "0.1.0"


def _profile(df: pd.DataFrame, table_name: str) -> Profile:
    return Profile(
        schema_version=1,
        tables=(
            TableProfile(
                name=table_name,
                row_count=len(df),
                columns=tuple(
                    ColumnProfile(
                        name=c,
                        dtype="object",
                        row_count=len(df),
                        null_count=int(df[c].isna().sum()),
                        distinct_count=int(df[c].nunique()),
                        sampled=False,
                        is_candidate_key_sampled=False,
                        declared_pk=False,
                        is_fk=False,
                        fk_target=None,
                        pii_class=None,
                    )
                    for c in df.columns
                ),
            ),
        ),
        relationships=(),
        profiled_at=datetime(2026, 5, 31),
        decoy_engine_version=_VERSION,
    )


def _composite_col(
    name: str, provider: str, coherent_with: tuple[str, ...], ns: str, **extra: Any
) -> dict:
    out = {
        "name": name,
        "strategy": "<composite>",
        "provider": provider,
        "deterministic": True,
        "namespace": ns,
        "coherent_with": list(coherent_with),
    }
    if extra:
        out["provider_config"] = extra
    return out


def _run(profile: Profile, config: dict[str, Any], df: pd.DataFrame, table_name: str):
    plan = compile_plan(config, profile, decoy_engine_version=_VERSION)
    ns_registry = build_namespace_registry(config, profile)
    if profile.relationships:
        lookup = check_orphan_fk_policy_completeness(config, profile.relationships)
        graph = build_relationship_graph(
            profile.relationships,
            namespace_registry=ns_registry,
            orphan_policy_lookup=lookup,
        )
    else:
        graph = RelationshipGraph(edges=(), ordering=())
    return PandasExecutionAdapter().run(
        plan,
        {table_name: pa.Table.from_pandas(df, preserve_index=False)},
        registry=get_default_registry(),
        relationship_graph=graph,
        namespace_registry=ns_registry,
    )


# ── composite_person ─────────────────────────────────────────────────


class TestCompositePersonE2E:
    def test_pipeline_e2e_composite_person_4_columns_coherent(self):
        df = pd.DataFrame(
            {
                "first_name": ["Alice", "Bob", "Carol"],
                "last_name": ["A", "B", "C"],
                "email": ["a@x.com", "b@x.com", "c@x.com"],
                "dob": ["1990-01-01", "1985-06-15", "2000-12-31"],
            }
        )
        cohorts = ("dob", "email", "first_name", "last_name")
        config = {
            "global_settings": {"seed": 7},
            "tables": [
                {
                    "name": "people",
                    "columns": [
                        _composite_col(
                            c,
                            "composite_person",
                            tuple(x for x in cohorts if x != c),
                            "p",
                        )
                        for c in cohorts
                    ],
                }
            ],
        }
        res = _run(_profile(df, "people"), config, df, "people")
        out = res.outputs["people"].to_pydict()
        # 4 outputs present + same length.
        for c in cohorts:
            assert len(out[c]) == 3
        # Email coherence: local-part == {first}.{last} lowercased.
        for i in range(3):
            local = str(out["email"][i]).split("@", 1)[0]
            expected = f"{out['first_name'][i]}.{out['last_name'][i]}".lower()
            assert local == expected


# ── composite_address ────────────────────────────────────────────────


class TestCompositeAddressE2E:
    def test_pipeline_e2e_composite_address_4_columns_coherent(self):
        df = pd.DataFrame(
            {
                "street_address": ["1 Main", "2 Oak", "3 Elm"],
                "city": ["X", "Y", "Z"],
                "state": ["XX", "YY", "ZZ"],
                "zip": ["00001", "00002", "00003"],
            }
        )
        cohorts = ("city", "state", "street_address", "zip")
        config = {
            "global_settings": {"seed": 7},
            "tables": [
                {
                    "name": "addrs",
                    "columns": [
                        _composite_col(
                            c,
                            "composite_address",
                            tuple(x for x in cohorts if x != c),
                            "a",
                        )
                        for c in cohorts
                    ],
                }
            ],
        }
        res = _run(_profile(df, "addrs"), config, df, "addrs")
        out = res.outputs["addrs"].to_pydict()
        for c in cohorts:
            assert len(out[c]) == 3
        # Every triple must be a verbatim locality-table row.
        locality = set(load_locality_table())
        for i in range(3):
            assert (out["city"][i], out["state"][i], out["zip"][i]) in locality


# ── composite_provider ───────────────────────────────────────────────


class TestCompositeProviderE2E:
    def test_pipeline_e2e_composite_provider_3_columns_coherent(self):
        df = pd.DataFrame(
            {
                "npi": ["1234567893", "1679576722", "1000000004"],
                "provider_name": ["Dr. A", "Dr. B", "Dr. C"],
                "practice_address": ["addr1", "addr2", "addr3"],
            }
        )
        cohorts = ("npi", "practice_address", "provider_name")
        config = {
            "global_settings": {"seed": 7},
            "tables": [
                {
                    "name": "providers",
                    "columns": [
                        _composite_col(
                            c,
                            "composite_provider",
                            tuple(x for x in cohorts if x != c),
                            "v",
                        )
                        for c in cohorts
                    ],
                }
            ],
        }
        res = _run(_profile(df, "providers"), config, df, "providers")
        out = res.outputs["providers"].to_pydict()
        for c in cohorts:
            assert len(out[c]) == 3
        # NPI passes the CMS Luhn validator (composite_provider contract).
        for i in range(3):
            assert _npi_valid(str(out["npi"][i]))


# ── composite_custom ─────────────────────────────────────────────────


class TestCompositeCustomE2E:
    def test_pipeline_e2e_composite_custom_3_column_bundle(self):
        df = pd.DataFrame(
            {
                "first": ["a", "b", "c"],
                "last": ["x", "y", "z"],
                "phone": ["111", "222", "333"],
            }
        )
        bundle = [
            {"column": "first", "provider": "person_first_name"},
            {"column": "last", "provider": "person_last_name"},
            {"column": "phone", "provider": "person_phone"},
        ]
        # All three columns are in the same coherent group; each carries
        # the same bundle declaration so the provider_config is uniform.
        cohorts = ("first", "last", "phone")
        config = {
            "global_settings": {"seed": 7},
            "tables": [
                {
                    "name": "people",
                    "columns": [
                        _composite_col(
                            c,
                            "composite_custom",
                            tuple(x for x in cohorts if x != c),
                            "c",
                            bundle=bundle,
                        )
                        for c in cohorts
                    ],
                }
            ],
        }
        res = _run(_profile(df, "people"), config, df, "people")
        out = res.outputs["people"].to_pydict()
        for c in cohorts:
            assert len(out[c]) == 3
        # All 3 outputs present; the values are not the original input.
        assert out["first"] != ["a", "b", "c"]
        assert out["last"] != ["x", "y", "z"]
        assert out["phone"] != ["111", "222", "333"]


# ── post-mask coherence audit ────────────────────────────────────────


class TestPostMaskCoherenceAudits:
    """Dennis H1 close: the 3 new fixed-output composites have post-mask
    coherence audits that emit reports through the validator layer."""

    def test_person_audit_runs(self):
        from decoy_engine.validation.post._checks._composite_coherence import (
            _audit_person,
        )

        df = pd.DataFrame(
            {
                "first_name": ["Alice", "Bob"],
                "last_name": ["A", "B"],
                "email": ["alice.a@x", "bob.b@x"],
                "dob": ["1990-01-01", "1985-06-15"],
            }
        )
        out = pa.Table.from_pandas(df, preserve_index=False)
        report = _audit_person(out)
        assert report.generator == "composite_person"
        # Both rows are coherent (email local matches {first}.{last}).
        assert report.total_rows == 2
        assert report.coherent_rows == 2
        assert report.incoherent_rows == 0

    def test_address_audit_runs(self):
        from decoy_engine.validation.post._checks._composite_coherence import (
            _audit_address,
        )

        locality = load_locality_table()
        first_three = locality[:3]
        df = pd.DataFrame(
            {
                "street_address": ["s1", "s2", "s3"],
                "city": [t[0] for t in first_three],
                "state": [t[1] for t in first_three],
                "zip": [t[2] for t in first_three],
            }
        )
        out = pa.Table.from_pandas(df, preserve_index=False)
        report = _audit_address(out)
        assert report.generator == "composite_address"
        assert report.coherent_rows == 3
        assert report.incoherent_rows == 0

    def test_provider_audit_runs(self):
        from decoy_engine.validation.post._checks._composite_coherence import (
            _audit_provider,
        )

        # 1234567893 is the verified-valid NPI from detectors.py.
        df = pd.DataFrame(
            {
                "npi": ["1234567893"],
                "provider_name": ["Dr. A"],
                "practice_address": ["x"],
            }
        )
        out = pa.Table.from_pandas(df, preserve_index=False)
        report = _audit_provider(out)
        assert report.generator == "composite_provider"
        assert report.coherent_rows == 1
        assert report.incoherent_rows == 0


# ── manifest carry-through ───────────────────────────────────────────


class TestManifest:
    def test_manifest_carries_composite_person_config(self):
        """The plan serializer round-trips composite_person config so a
        downstream consumer can inspect coherent_with + provider."""
        from decoy_engine.plan._serialize import (
            _column_seed_from_dict,
            _column_seed_to_dict,
        )
        from decoy_engine.plan._types import ColumnSeed

        seed_in = ColumnSeed(
            namespace="p",
            strategy="<composite>",
            provider="composite_person",
            backend_type="composite",
            backend_version="composite/v1",
            cardinality_mode="reuse",
            deterministic=True,
            provider_config=(),
            coherent_with=("last_name", "email", "dob"),
        )
        payload = _column_seed_to_dict(seed_in)
        assert payload["provider"] == "composite_person"
        assert set(payload["coherent_with"]) == {"last_name", "email", "dob"}
        seed_out = _column_seed_from_dict(payload)
        assert seed_out.provider == "composite_person"
