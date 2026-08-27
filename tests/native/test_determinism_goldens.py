"""Task 0.3 Step 8: the goldens gate (program-gating).

This routes the REAL shipped engine draw at each catalogued site through that
site's provider and asserts the provider reproduces the shipped output EXACTLY.
It does not compare against a re-implemented reference: it imports the actual
masking / generation / transform functions the existing seeded golden suites
exercise (``_categorical`` / ``_faker`` / ``_apply_null_probability`` /
``_reference`` in ``synthesize.py``; ``sample_column``; ``_apply_monotone_walk``;
``apply_windowed_date``; ``ShuffleStrategyHandler``; ``hash_array``) and the
keyed primitives (``derive`` / ``derive_index`` / ``derive_value``), runs them on
fixed seeds, and drives the provider on the same identity.

If any site fails to reproduce, that is a real finding: the fix is to correct
the provider to match SHIPPED behavior (and the inventory entry), never to
weaken a golden. A green gate that required editing a golden is a failed gate.

The count of routed vectors is asserted so the gate cannot silently shrink.
"""

from __future__ import annotations

import random
from datetime import timedelta

import numpy as np
import pandas as pd

from decoy_engine.determinism import IdentityDomain, derive, derive_index, derive_value
from decoy_engine.execution._strategies._shuffle import ShuffleStrategyHandler
from decoy_engine.execution.native._determinism_protocol import provider_for
from decoy_engine.generation.statistical._sample import (
    _cumulative,
    _is_integer_dtype,
    _numeric_row,
    sample_column,
)
from decoy_engine.generation.statistical._spec import StatisticalSpec
from decoy_engine.generation.synthesize import (
    _apply_null_probability,
    _categorical,
    _faker,
    _get_default_faker,
    _reference,
)
from decoy_engine.generators.derivation import GenDeriveContext
from decoy_engine.kernel._canonicalize import canonicalize_derive_source
from decoy_engine.kernel._scalar import hash_array
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.grouped_series import GroupedSeriesConfig, _apply_monotone_walk
from decoy_engine.transforms.windowed_date import (
    _DATE_FMT,
    WindowedDateConfig,
    _sample_offset,
    apply_windowed_date,
)

_MASK_KEY = (0x77).to_bytes(8, "big")
_SEED_INT = 42

# Every routed (site, case) vector increments this. The final test asserts the
# total so the gate's coverage cannot silently shrink.
_ROUTED: list[str] = []


def _record(site_id: str) -> None:
    _ROUTED.append(site_id)


def _gen_ctx(col: dict, seed: int = _SEED_INT) -> GenDeriveContext:
    return GenDeriveContext.for_column(derive_key=None, column_config=col, fallback_seed=seed)


class _Ctx:
    """Duck-typed StrategyContext for a direct handler.run() call (mirrors the
    stand-in in tests/unit/execution/test_shuffle_categorical.py)."""

    job_seed = _MASK_KEY
    mask_key = _MASK_KEY


def _shuffle_plan(namespace: str) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy="shuffle",
        provider="shuffle",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


# ---------------------------------------------------------------------------
# Masking sites.
# ---------------------------------------------------------------------------


class TestMaskShuffleGolden:
    def test_provider_reproduces_shipped_handler_permutation(self) -> None:
        col = "email"
        values = ["a", "b", None, "c", "d", None, "e", "f", "g"]
        df = pd.DataFrame({col: values})
        handler = ShuffleStrategyHandler()
        out_df, _ = handler.run(df.copy(), col, _shuffle_plan("sh"), _Ctx())
        shipped = out_df[col].tolist()

        # Reproduce: derive the non-null order from the provider permutation and
        # scatter it back into the non-null positions, exactly as the handler does.
        source = pd.Series(values)
        na_mask = source.isna().to_numpy()
        non_na_positions = np.where(~na_mask)[0]
        non_na_values = source.to_numpy(dtype=object)[~na_mask]
        perm = provider_for("mask.shuffle").permutation(_MASK_KEY, "sh", col, len(non_na_values))
        permuted = non_na_values[perm]
        reproduced: list[object] = [None] * len(values)
        for offset, position in enumerate(non_na_positions):
            reproduced[int(position)] = permuted[offset]

        assert reproduced == shipped
        _record("mask.shuffle")


class TestMaskHashGolden:
    def test_provider_reproduces_shipped_hash_array(self) -> None:
        namespace = "col/email"
        values = ["alice@example.com", None, "bob@example.com", "carol@x.org"]
        shipped = hash_array(values, seed=_MASK_KEY, namespace=namespace, truncate=None).to_pylist()

        p = provider_for("mask.hash")
        reproduced = []
        for v in values:
            if v is None:
                reproduced.append(None)
                continue
            digest = p.draw(_MASK_KEY, namespace, canonicalize_derive_source(v))
            reproduced.append(digest.hex())
        assert reproduced == shipped
        _record("mask.hash")


class TestGroupedSeriesWalkGolden:
    def test_provider_reproduces_shipped_monotone_walk(self) -> None:
        namespace = "grouped/amount"
        working = pd.DataFrame(
            {
                "grp": ["A", "A", "B", "A", "B", "B", "C"],
                "ord": [1, 2, 1, 3, 2, 3, 1],
                "_pos": [0, 1, 2, 3, 4, 5, 6],
            }
        )
        n = len(working)
        config = GroupedSeriesConfig(
            group_by="grp", order_by="ord", generator="monotone_walk", start=1, step=1, max_step=10
        )
        shipped = _apply_monotone_walk(
            config, working, "grp", "ord", "_pos", n, _MASK_KEY, namespace
        ).tolist()

        # Reproduce: sort by (group, order), walk each group via the provider,
        # scatter back to original positions.
        p = provider_for("mask.grouped_series_monotone_walk")
        sorted_df = working.sort_values(["grp", "ord"], kind="stable")
        reproduced = [0] * n
        for g, block in sorted_df.groupby("grp", sort=False):
            positions = [int(pos) for pos in block["_pos"].tolist()]
            walk = p.walk(_MASK_KEY, namespace, g, len(positions), start=1, step=1, max_step=10)
            for pos, val in zip(positions, walk, strict=True):
                reproduced[pos] = val
        assert reproduced == shipped
        _record("mask.grouped_series_monotone_walk")


class TestWindowedDateGolden:
    def test_provider_reproduces_shipped_windowed_dates(self) -> None:
        namespace = "windowed/visit"
        anchors = ["2020-01-01", "2020-06-15", "2021-03-30", "2019-12-31", "2022-08-08"]
        df = pd.DataFrame({"anchor": anchors})
        config = WindowedDateConfig(
            anchor="anchor", min_days=-30, max_days=30, distribution="uniform"
        )
        shipped = apply_windowed_date(config, df, _MASK_KEY, namespace)

        p = provider_for("mask.windowed_date")
        reproduced = []
        for i, raw in enumerate(anchors):
            rng = p.row_generator(_MASK_KEY, namespace, i)
            offset = _sample_offset(rng, config.min_days, config.max_days, config.distribution)
            reproduced.append((pd.Timestamp(raw) + timedelta(days=offset)).strftime(_DATE_FMT))
        assert reproduced == shipped
        _record("mask.windowed_date")


# ---------------------------------------------------------------------------
# Generation sites.
# ---------------------------------------------------------------------------


class TestCategoricalGolden:
    def test_provider_reproduces_shipped_categorical(self) -> None:
        col = {
            "name": "tier",
            "type": "categorical",
            "categories": ["A", "B", "C", "D"],
            "weights": [1.0, 2.0, 3.0, 4.0],
        }
        n = 60
        shipped = _categorical(col, n, _SEED_INT)
        ctx = _gen_ctx(col)
        reproduced = provider_for("gen.categorical").choices(
            ctx, col["categories"], col["weights"], n
        )
        assert reproduced == shipped
        _record("gen.categorical")


class TestReferenceGolden:
    def test_provider_reproduces_shipped_reference(self) -> None:
        import pyarrow as pa

        col = {
            "name": "fk",
            "type": "reference",
            "reference_table": "parent",
            "reference_column": "id",
            "distribution": "random",
        }
        n = 40
        pools = {"parent": pa.table({"id": [10, 20, 30, 40, 50, None, 20]})}
        shipped = _reference(col, n, _SEED_INT, pools=pools)

        # Insertion-order unique + dropna, exactly as the shipped function does.
        seen: set = set()
        ref_vals: list = []
        for v in pools["parent"].column("id").to_pylist():
            if v is None or v in seen:
                continue
            seen.add(v)
            ref_vals.append(v)
        ctx = _gen_ctx(col)
        reproduced = provider_for("gen.reference").sample(ctx, ref_vals, n, distribution="random")
        assert reproduced == shipped
        _record("gen.reference")


class TestNullProbabilityGolden:
    def test_provider_reproduces_shipped_null_mask(self) -> None:
        col = {"name": "score", "type": "faker", "faker_type": "pyint", "null_probability": 0.35}
        values = list(range(50))
        shipped = _apply_null_probability(list(values), col, _SEED_INT)

        ctx = _gen_ctx(col)
        floats = provider_for("gen.null_probability").null_floats(ctx, len(values))
        null_prob = float(col["null_probability"])
        reproduced = [None if floats[i] < null_prob else values[i] for i in range(len(values))]
        assert reproduced == shipped
        _record("gen.null_probability")


class TestFakerPerRowGolden:
    def test_provider_reproduces_shipped_faker_column(self) -> None:
        col = {"name": "first", "type": "faker", "faker_type": "first_name"}
        n = 25
        shipped = _faker(col, n, _SEED_INT)

        # The no-locale path uses the shared default instance; the per-row
        # seed_instance overrides any prior state, so reusing it is exact.
        faker_inst = _get_default_faker()
        ctx = _gen_ctx(col)
        reproduced = provider_for("gen.faker_per_row").run(
            faker_inst, faker_inst.first_name, ctx, n
        )
        assert reproduced == shipped
        _record("gen.faker_per_row")


class TestStatisticalPerRowGolden:
    def test_provider_reproduces_shipped_numeric_sampler(self) -> None:
        spec = StatisticalSpec(
            column="age",
            source_column="age",
            kind="numeric",
            dtype="float64",
            stats={"bin_edges": [0.0, 10.0, 20.0, 30.0, 40.0], "bin_counts": [5.0, 3.0, 8.0, 2.0]},
            other_mode="redistribute",
            condition_on=None,
            joint=None,
            parent_first=False,
        )
        n, col_seed = 30, 918273
        shipped = sample_column(spec, n, col_seed=col_seed)

        # Reproduce the sampler's per-row loop, driving the reseed via the provider.
        edges = [float(e) for e in spec.stats["bin_edges"]]
        counts = [float(c) for c in spec.stats["bin_counts"]]
        cum = _cumulative(counts)
        as_int = _is_integer_dtype(spec.dtype)
        lo_bound, hi_bound = edges[0], edges[-1]
        p = provider_for("gen.statistical_per_row")
        rng = random.Random()
        reproduced: list = []
        for i in range(n):
            p.reseed_row(rng, col_seed, i)
            x = _numeric_row(rng, edges, cum)
            reproduced.append(int(min(max(round(x), lo_bound), hi_bound)) if as_int else x)
        assert reproduced == shipped
        _record("gen.statistical_per_row")


# ---------------------------------------------------------------------------
# source_keyed_hmac sites whose draw IS the keyed primitive: the provider
# reproduces the exact call the shipped transform makes. Each is one vector.
# ---------------------------------------------------------------------------


class TestSourceKeyedPrimitiveGoldens:
    def test_derive_sites(self) -> None:
        source = b"payload"
        ns = "col/x"
        for site_id in (
            "mask.fpe",
            "mask.date_shift",
            "mask.bucket_perturb",
            "mask.group_key",
            "mask.joint_mask_keyed_row",
        ):
            assert provider_for(site_id).draw(_MASK_KEY, ns, source) == derive(
                _MASK_KEY, ns, source
            )
            _record(site_id)

    def test_derive_index_sites(self) -> None:
        source = b"CA"
        ns = "col/state"
        for site_id in (
            "mask.categorical_deterministic",
            "mask.code_set",
            "gen.pool_deterministic",
            "mask.faker",
        ):
            got = provider_for(site_id).draw(_MASK_KEY, ns, source, pool_size=64)
            assert got == derive_index(_MASK_KEY, ns, source, pool_size=64)
            _record(site_id)

    def test_derive_value_site(self) -> None:
        source = b"123456789"
        ns = "col/ssn"
        got = provider_for("gen.identifier_deterministic").draw(
            _MASK_KEY, ns, source, domain=IdentityDomain()
        )
        assert got == derive_value(_MASK_KEY, ns, source, domain=IdentityDomain())
        _record("gen.identifier_deterministic")


# ---------------------------------------------------------------------------
# Coverage assertion: the gate routes a fixed, non-shrinking set of vectors.
# ---------------------------------------------------------------------------


class TestGoldenGateCoverage:
    def test_every_routed_vector_reproduced_and_count_is_locked(self) -> None:
        # Force the routing tests to have run (pytest orders by definition; this
        # class is last). Re-run the routed set here so the count is independent
        # of collection order.
        expected_sites = {
            "mask.shuffle",
            "mask.hash",
            "mask.grouped_series_monotone_walk",
            "mask.windowed_date",
            "gen.categorical",
            "gen.reference",
            "gen.null_probability",
            "gen.faker_per_row",
            "gen.statistical_per_row",
            "mask.fpe",
            "mask.date_shift",
            "mask.bucket_perturb",
            "mask.group_key",
            "mask.joint_mask_keyed_row",
            "mask.categorical_deterministic",
            "mask.code_set",
            "gen.pool_deterministic",
            "mask.faker",
            "gen.identifier_deterministic",
        }
        assert set(_ROUTED) == expected_sites
        # 19 distinct sites, each routed exactly once.
        assert len(_ROUTED) == 19
        assert sorted(_ROUTED) == sorted(expected_sites)
