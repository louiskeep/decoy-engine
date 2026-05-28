"""composite_city_state_zip: coherent (city, state, zip) triples (engine-v2 S8).

Membership-by-construction: every output triple is a verbatim row of a US
locality table, so a masked row can never produce a Texas city with a Chicago
ZIP. Deterministic and non-deterministic sampling both route through
`PoolSampler.sample_bundle` (S8 spec §3b/§5): one latent index selects one
triple, so there is no sub-field independence to preserve (unlike
composite_name_email, which needs the sub-byte split).

Locality data source (S8 spec §5, RESOLVED PO Session 33): US Census place
names + USPS ZIP code data, both public domain; the join + curation is engine
IP. The shipped `data/us_localities.csv` is a curated real STARTER set; the
full ~32,000-entry Census/USPS ingestion is a data-population follow-up (the
loader + composite logic are table-size-independent). Reference-table technique
per best-practices §6.2.
"""

from __future__ import annotations

import csv
import os
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from decoy_engine.generation.composite._bundle_pool import BundlePool
from decoy_engine.generation.composite._errors import CompositeError
from decoy_engine.generation.pool._cardinality import CardinalityMode
from decoy_engine.generation.pool._sampler import PoolSampler
from decoy_engine.providers_v2._adapter import ProviderSpec

_LOCALITY_CSV = Path(__file__).parent / "data" / "us_localities.csv"


@lru_cache(maxsize=1)
def load_locality_table() -> tuple[tuple[str, str, str], ...]:
    """Load the (city, state, zip) locality table once (cached for the process)."""
    rows: list[tuple[str, str, str]] = []
    with _LOCALITY_CSV.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append((row["city"], row["state"], row["zip"]))
    if not rows:
        raise CompositeError(
            code="empty_locality_table",
            message=f"Locality table at {_LOCALITY_CSV} is empty.",
        )
    return tuple(rows)


class CompositeCityStateZip:
    """CompositeGenerator for coherent city/state/zip triples."""

    composite_name: str = "composite_city_state_zip"
    output_columns: tuple[str, ...] = ("city", "state", "zip")

    def __init__(self, *, coherent_namespace: str) -> None:
        self.coherent_namespace = coherent_namespace
        self._table = load_locality_table()
        self._sampler = PoolSampler()
        self._pool: BundlePool | None = None

    @property
    def locality_table(self) -> tuple[tuple[str, str, str], ...]:
        return self._table

    def build_pool(self, spec: ProviderSpec, *, size: int | None = None) -> BundlePool:
        """Build a BundlePool whose values are the locality triples.

        The pool IS the locality table (membership-by-construction), so `size`
        is the table length; the `size` arg is accepted for protocol symmetry
        but a smaller pool would exclude triples, so it is ignored here.
        """
        table = self._table
        values = np.empty(len(table), dtype=object)
        for i, triple in enumerate(table):
            values[i] = triple
        values.setflags(write=False)
        return BundlePool(
            values=values,
            provider=self.composite_name,
            locale=spec.locale or "en_US",
            config_hash="locality/v1",
            seed=spec.seed if spec.seed is not None else b"\x00" * 8,
            size=len(table),
            build_time_ms=0.0,
            backend_type="composite",
            backend_version="locality/v1",
            distinct_count=len(table),
            output_columns=self.output_columns,
        )

    def _pool_for(self, spec: ProviderSpec) -> BundlePool:
        if self._pool is None:
            self._pool = self.build_pool(spec)
        return self._pool

    def generate_bundle(
        self,
        spec: ProviderSpec,
        count: int,
        *,
        source: pd.Series | None = None,
        deterministic: bool = False,
    ) -> dict[str, pd.Series]:
        pool = self._pool_for(spec)
        if deterministic:
            if source is None or spec.seed is None:
                raise CompositeError(
                    code="deterministic_requires_source_and_seed",
                    message="Deterministic composite_city_state_zip requires source + seed.",
                )
            return self._sampler.sample_bundle(
                pool,
                count,
                mode=CardinalityMode.REUSE,
                seed=spec.seed,
                source=source,
                namespace=self.coherent_namespace,
                deterministic=True,
            )
        seed = spec.seed if spec.seed is not None else os.urandom(8)
        return self._sampler.sample_bundle(
            pool, count, mode=CardinalityMode.REUSE, seed=seed, deterministic=False
        )


def composite_city_state_zip(*, coherent_namespace: str) -> CompositeCityStateZip:
    """Construct a CompositeCityStateZip generator bound to `coherent_namespace`."""
    return CompositeCityStateZip(coherent_namespace=coherent_namespace)


__all__: list[str] = ["CompositeCityStateZip", "composite_city_state_zip", "load_locality_table"]
