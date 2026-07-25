"""Composite substrate parity: pandas adapter vs polars adapter.

Audit gap closure (2026-06-12): composite bundles had ZERO parity
coverage -- the substrate-equality guarantee was unverified exactly
where the slot-to-column mapping bug (audit H2) lived. The polars
adapter currently routes composite nodes through its pandas fallback,
so this pins (a) that the fallback routing produces output identical to
the pandas adapter and (b) the BundlePool order contract end-to-end
with a deliberately non-alphabetical bundle declaration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd
import pyarrow as pa

from decoy_engine.execution import PandasExecutionAdapter, PolarsExecutionAdapter
from decoy_engine.plan import compile_plan
from decoy_engine.profile import ColumnProfile, Profile, TableProfile
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import build_namespace_registry

_VERSION = "0.1.0"
_GRAPH = RelationshipGraph(edges=(), ordering=())


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
    out: dict[str, Any] = {
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


def _both(config: dict[str, Any], df: pd.DataFrame, table: str) -> tuple[dict, dict]:
    profile = _profile(df, table)
    plan = compile_plan(config, profile, decoy_engine_version=_VERSION)
    ns_registry = build_namespace_registry(config, profile)
    sources = {table: pa.Table.from_pandas(df, preserve_index=False)}
    registry = get_default_registry()
    pandas_res = PandasExecutionAdapter().run(
        plan, sources, registry=registry, relationship_graph=_GRAPH, namespace_registry=ns_registry
    )
    polars_res = PolarsExecutionAdapter().run(
        plan, sources, registry=registry, relationship_graph=_GRAPH, namespace_registry=ns_registry
    )
    return (
        pandas_res.outputs[table].to_pydict(),
        polars_res.outputs[table].to_pydict(),
    )


class TestCompositeSubstrateParity:
    def test_composite_name_email_parity(self):
        df = pd.DataFrame(
            {
                "first_name": ["Alice", "Bob", "Carol"],
                "last_name": ["A", "B", "C"],
                "email": ["a@x.com", "b@x.com", "c@x.com"],
            }
        )
        cols = ("email", "first_name", "last_name")
        config = {
            "global_settings": {"seed": 7},
            "tables": [
                {
                    "name": "people",
                    "columns": [
                        _composite_col(
                            c, "composite_name_email", tuple(x for x in cols if x != c), "ne"
                        )
                        for c in cols
                    ],
                }
            ],
        }
        pandas_out, polars_out = _both(config, df, "people")
        assert pandas_out == polars_out
        assert all("@" in v for v in pandas_out["email"])

    def test_composite_custom_nonalphabetical_bundle_parity(self):
        # Audit H2 cross-substrate regression: declaration order
        # deliberately disagrees with the sorted coherent group.
        df = pd.DataFrame(
            {
                "z_col": ["s1", "s2", "s3"],
                "a_col": ["t1", "t2", "t3"],
                "m_col": ["u1", "u2", "u3"],
            }
        )
        bundle = [
            {"column": "z_col", "provider": "person_first_name"},
            {"column": "a_col", "provider": "person_last_name"},
            {"column": "m_col", "provider": "person_email"},
        ]
        cols = ("a_col", "m_col", "z_col")
        config = {
            "global_settings": {"seed": 7},
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        _composite_col(
                            c,
                            "composite_custom",
                            tuple(x for x in cols if x != c),
                            "cust",
                            bundle=bundle,
                        )
                        for c in cols
                    ],
                }
            ],
        }
        pandas_out, polars_out = _both(config, df, "t")
        assert pandas_out == polars_out
        # H2's failure mode: emails landing in z_col instead of m_col.
        assert all("@" in v for v in pandas_out["m_col"])
        assert not any("@" in str(v) for v in pandas_out["z_col"])
