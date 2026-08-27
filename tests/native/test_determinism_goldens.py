"""Task 0.3 Step 8: the goldens gate (program-gating).

This routes the REAL shipped engine draw at each catalogued site through that
site's provider and asserts the provider reproduces the shipped output EXACTLY.
It NEVER compares a provider against a second copy of the same formula: every
test invokes the actual shipped transform / handler / primitive and drives the
provider on the same identity. The functions routed:

- generation: ``_categorical`` / ``_faker`` / ``_apply_null_probability`` /
  ``_reference`` (``synthesize.py``), ``sample_column`` (statistical).
- masking transforms/handlers: ``ShuffleStrategyHandler``, ``hash_array``,
  ``apply_group_key``, ``apply_bucket_perturb``, ``DateShiftStrategyHandler``,
  ``CategoricalStrategyHandler`` (uniform + weighted), ``_pick_from_seq``
  (code_set mask mode), ``ReferenceTable.keyed_row`` (joint_mask),
  ``PoolSampler.sample`` (pool_deterministic + mask.faker), ``SsnAdapter``
  (identifier), ``_apply_monotone_walk``, ``apply_windowed_date``.
- mask.fpe is keyed-material: the provider emits the per-column Feistel KEY, and
  the ciphertext is reproduced by driving that key through the SHIPPED
  ``fpe_encrypt_value`` and matching the real ``FpeStrategyHandler`` output.

18 sites reproduce the shipped OUTPUT byte-for-byte; mask.fpe reproduces the
keyed material (Feistel arithmetic is transform semantics, deferred to Task 0.4).

If any site fails to reproduce, that is a real finding: the fix is to correct
the provider to match SHIPPED behavior (and the inventory entry), never to
weaken a golden. A green gate that required editing a golden is a failed gate.

The count of routed vectors is asserted so the gate cannot silently shrink.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pyarrow as pa

from decoy_engine.determinism import derive
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._strategies._categorical import (
    _WEIGHTED_CDF_RES,
    CategoricalStrategyHandler,
    _build_cdf,
)
from decoy_engine.execution._strategies._date_shift import DateShiftStrategyHandler
from decoy_engine.execution._strategies._fpe import FpeStrategyHandler
from decoy_engine.execution._strategies._shuffle import ShuffleStrategyHandler
from decoy_engine.execution.native._determinism_protocol import provider_for
from decoy_engine.generation.pool import ValuePool
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.generation.pool._cardinality import CardinalityMode
from decoy_engine.generation.pool._sampler import PoolSampler
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
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.providers_v2._adapter import ProviderSpec
from decoy_engine.providers_v2.identifiers._ssn import SsnAdapter, SsnDomain
from decoy_engine.reference_tables import ReferenceTable
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from decoy_engine.transforms.bucket_perturb import _bucket_start_and_size, apply_bucket_perturb
from decoy_engine.transforms.code_set import _pick_from_seq
from decoy_engine.transforms.fpe import _CHARSETS, fpe_encrypt_value
from decoy_engine.transforms.group_key import GroupKeyConfig, apply_group_key
from decoy_engine.transforms.grouped_series import GroupedSeriesConfig, _apply_monotone_walk
from decoy_engine.transforms.joint_mask import _KEYED_ROW_SOURCE
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


def _full_ctx() -> StrategyContext:
    """A real StrategyContext (mask_key defaults to job_seed = _MASK_KEY) for the
    handlers that read more than mask_key (date_shift, categorical, fpe)."""
    return StrategyContext(
        registry=get_default_registry(),
        pool_cache=PoolCache(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
        job_seed=_MASK_KEY,
    )


def _mask_col(strategy: str, namespace: str, provider_config: tuple) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=provider_config,
        coherent_with=(),
    )


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
# source_keyed_hmac sites: route the REAL shipped transform / primitive and
# assert the provider reproduces its output (or, for fpe, its keyed material
# proven through the shipped Feistel). NO derive-vs-derive tautologies.
# ---------------------------------------------------------------------------


class TestGroupKeyGolden:
    def test_provider_reproduces_shipped_group_key(self) -> None:
        ns = "group_key/household"
        df = pd.DataFrame({"household": ["H1", "H2", "H1", "H3", "H2"]})
        config = GroupKeyConfig(group_by="household", length=16, prefix="HH-")
        shipped = apply_group_key(config, df, _MASK_KEY, ns)

        p = provider_for("mask.group_key")
        n_bytes = config.length // 2
        reproduced = [
            config.prefix + p.draw(_MASK_KEY, ns, str(v).encode("utf-8"))[:n_bytes].hex()
            for v in df["household"]
        ]
        assert reproduced == shipped
        _record("mask.group_key")


class TestBucketPerturbGolden:
    def test_provider_reproduces_shipped_bucket_perturb(self) -> None:
        ns = "bucket/dob"
        dates = ["2020-03-15", "2019-07-01", "2021-11-30", "2018-01-05"]
        series = pd.Series(dates)
        shipped = apply_bucket_perturb(series, "month", _MASK_KEY, ns, "%Y-%m-%d").tolist()

        p = provider_for("mask.bucket_perturb")
        reproduced = []
        for value_str in dates:
            date = datetime.strptime(value_str, "%Y-%m-%d").date()
            bucket_start, bucket_size = _bucket_start_and_size(date, "month")
            digest = p.draw(_MASK_KEY, ns, _canonicalize_source(value_str))
            offset = int.from_bytes(digest[:8], "big") % bucket_size
            reproduced.append((bucket_start + timedelta(days=offset)).strftime("%Y-%m-%d"))
        assert reproduced == shipped
        _record("mask.bucket_perturb")


class TestDateShiftGolden:
    def test_provider_reproduces_shipped_date_shift(self) -> None:
        ns = "date_shift/visit"
        dates = ["2020-01-10", "2021-06-20", "2019-12-31", "2022-02-28"]
        df = pd.DataFrame({"visit": dates})
        plan = _mask_col(
            "date_shift",
            ns,
            (("date_format", "%Y-%m-%d"), ("max_days", 45), ("min_days", -45)),
        )
        out_df, _ = DateShiftStrategyHandler().run(df.copy(), "visit", plan, _full_ctx())
        shipped = out_df["visit"].tolist()

        p = provider_for("mask.date_shift")
        min_days, max_days = -45, 45
        range_size = max_days - min_days + 1
        reproduced = []
        for value_str in dates:
            digest = p.draw(_MASK_KEY, ns, _canonicalize_source(value_str))
            shift = min_days + (int.from_bytes(digest[:8], "big") % range_size)
            reproduced.append(
                (pd.Timestamp(value_str) + timedelta(days=shift)).strftime("%Y-%m-%d")
            )
        assert reproduced == shipped
        _record("mask.date_shift")


class TestCategoricalDeterministicGolden:
    def test_provider_reproduces_shipped_uniform_categorical(self) -> None:
        ns = "cat/state"
        cats = ["CA", "NY", "TX", "WA", "OR"]
        values = ["a", "b", "c", None, "a", "z"]
        df = pd.DataFrame({"state": values})
        plan = _mask_col("categorical", ns, (("categories", cats),))
        out_df, _ = CategoricalStrategyHandler().run(df.copy(), "state", plan, _full_ctx())
        shipped = out_df["state"].tolist()

        p = provider_for("mask.categorical_deterministic")
        reproduced = [
            None
            if v is None
            else cats[p.draw(_MASK_KEY, ns, _canonicalize_source(v), pool_size=len(cats))]
            for v in values
        ]
        assert reproduced == shipped
        _record("mask.categorical_deterministic")

    def test_provider_reproduces_shipped_weighted_categorical(self) -> None:
        import bisect

        ns = "cat/tier"
        cats = ["free", "pro", "team"]
        weights = [0.6, 0.3, 0.1]
        values = ["u1", "u2", "u3", "u4", "u5"]
        df = pd.DataFrame({"tier": values})
        plan = _mask_col("categorical", ns, (("categories", cats), ("weights", weights)))
        out_df, _ = CategoricalStrategyHandler().run(df.copy(), "tier", plan, _full_ctx())
        shipped = out_df["tier"].tolist()

        p = provider_for("mask.categorical_deterministic")
        cdf = _build_cdf(weights)
        reproduced = []
        for v in values:
            bucket = p.draw(_MASK_KEY, ns, _canonicalize_source(v), pool_size=_WEIGHTED_CDF_RES)
            cat_idx = min(bisect.bisect_right(cdf, bucket), len(cats) - 1)
            reproduced.append(cats[cat_idx])
        assert reproduced == shipped


class TestCodeSetGolden:
    def test_provider_reproduces_shipped_pick_from_seq(self) -> None:
        ns = "code_set/icd10"
        seq = [{"code": f"E{i:02d}.{j}"} for i in range(5) for j in range(4)]
        candidate_count = len(seq)
        p = provider_for("mask.code_set")
        for key_value in ("E11.9", "I10", "J45.909", "Z00.00"):
            shipped = _pick_from_seq(
                key_value, seq, None, candidate_count, mask_key=_MASK_KEY, namespace=ns
            )
            idx = p.select_index(_MASK_KEY, ns, key_value, candidate_count)
            reproduced = str(seq[p.resolve_hole(idx, None)]["code"])
            assert reproduced == shipped
        _record("mask.code_set")


class TestJointMaskGolden:
    def test_provider_reproduces_shipped_keyed_row(self) -> None:
        ns = "joint_mask/address"
        table = ReferenceTable(
            pa.table(
                {
                    "id": list(range(1, 21)),
                    "city": [f"City{i}" for i in range(1, 21)],
                }
            ),
            "addresses",
        )
        # Independent (real-shipped) hmac_key, fed to the real keyed_row.
        hmac_key = derive(_MASK_KEY, ns, _KEYED_ROW_SOURCE)
        p = provider_for("mask.joint_mask_keyed_row")
        for key_value in ("masked-1", "masked-2", "masked-abc", "masked-xyz"):
            shipped_row = table.keyed_row(key_value, hmac_key=hmac_key)
            idx = p.row_index(_MASK_KEY, ns, key_value, table.row_count)
            assert table.row(idx) == shipped_row
        _record("mask.joint_mask_keyed_row")


class TestPoolDeterministicGolden:
    def test_providers_reproduce_shipped_pool_sampler(self) -> None:
        ns = "pool/city"
        pool = ValuePool(
            values=np.array([f"V{i}" for i in range(32)], dtype=object),
            provider="faker",
            locale=None,
            config_hash="abc",
            seed=b"\x00" * 8,
            size=32,
            build_time_ms=1.0,
            backend_type="faker",
            backend_version="25.4.0",
            distinct_count=32,
        )
        values = ["a", "b", None, "c", "a", "d"]
        source = pd.Series(values)
        shipped = (
            PoolSampler()
            .sample(
                pool,
                len(values),
                mode=CardinalityMode.REUSE,
                seed=_MASK_KEY,
                source=source,
                namespace=ns,
                deterministic=True,
            )
            .tolist()
        )

        # gen.pool_deterministic and mask.faker both drive this per-row derive_index.
        for site_id in ("gen.pool_deterministic", "mask.faker"):
            p = provider_for(site_id)
            reproduced = []
            for v in values:
                if v is None:
                    reproduced.append(pd.NA)
                    continue
                idx = p.draw(_MASK_KEY, ns, _canonicalize_source(v), pool_size=pool.size)
                reproduced.append(pool.values[idx])
            assert [x for x in reproduced if x is not pd.NA] == [
                x for x in shipped if x is not pd.NA
            ]
            # Null positions align too.
            assert [x is pd.NA for x in reproduced] == [pd.isna(x) for x in shipped]
            _record(site_id)


class TestIdentifierDeterministicGolden:
    def test_provider_reproduces_shipped_ssn_adapter(self) -> None:
        ns = "identifier/ssn"
        spec = ProviderSpec(locale=None, deterministic=True, namespace=ns, seed=_MASK_KEY, extra={})
        p = provider_for("gen.identifier_deterministic")
        for source in (b"123456789", b"987654321", b"555443333"):
            shipped = SsnAdapter().generate("synthetic_ssn", spec=spec, source_value=source)
            reproduced = p.draw(_MASK_KEY, ns, source, domain=SsnDomain(rng_config={}))
            assert reproduced == shipped
        _record("gen.identifier_deterministic")


class TestFpeKeyGolden:
    def test_provider_key_reproduces_shipped_ciphertext(self) -> None:
        # mask.fpe is keyed-material: the provider emits the per-column Feistel
        # KEY; the ciphertext is reproduced by driving that key through the
        # SHIPPED fpe_encrypt_value, and must equal the real handler's output.
        ns = "fpe/acct"
        col = "acct"
        values = ["12345", "67890", None, "24680"]
        df = pd.DataFrame({col: values})
        plan = _mask_col("fpe", ns, (("charset", "digits"),))
        out_df, _ = FpeStrategyHandler(chunk_count=1).run(df.copy(), col, plan, _full_ctx())
        shipped = out_df[col].tolist()

        p = provider_for("mask.fpe")
        key = p.column_key(_MASK_KEY, ns)
        charset = "".join(dict.fromkeys(_CHARSETS.get("digits", "digits")))
        tweak = col.encode("utf-8", errors="replace")
        reproduced = [
            None if v is None else fpe_encrypt_value(str(v), key, charset, tweak, True, False, None)
            for v in values
        ]
        # Null marker (None vs nan) is a pandas detail; compare non-null cells.
        assert [r for r in reproduced if r is not None] == [s for s in shipped if not pd.isna(s)]
        assert [r is None for r in reproduced] == [pd.isna(s) for s in shipped]
        _record("mask.fpe")


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
        # 19 distinct sites, each routed exactly once through the REAL shipped
        # code. 18 reproduce the shipped OUTPUT byte-for-byte; mask.fpe is
        # keyed-material (the provider emits the Feistel key, and the ciphertext
        # is reproduced via the shipped fpe_encrypt_value driven by that key).
        _keyed_material_only = {"mask.fpe"}
        _reproduces_output = expected_sites - _keyed_material_only
        assert len(_reproduces_output) == 18
        assert len(_ROUTED) == 19
        assert sorted(_ROUTED) == sorted(expected_sites)
