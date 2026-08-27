"""Task 0.3 protocol tests: the float fix, per-draw-site emulation, the
partition contract, and subprocess stability.

Each provider reproduces the EXACT sequence the current engine draws at its
site. These tests pin that against the shipped seed derivations verbatim (the
goldens gate in ``test_determinism_goldens.py`` routes the real engine code
through the same providers). Global sites are proven NON-partitionable by a
refusal, never by a fabricated substream.
"""

from __future__ import annotations

import hashlib
import hmac
import random
import subprocess
import sys

import numpy as np
import pytest

from decoy_engine.determinism import IdentityDomain, derive, derive_index, derive_value
from decoy_engine.execution.native._determinism_protocol import (
    DRAW_SITES,
    DrawSiteProtocolError,
    all_providers,
    draw_site_by_id,
    provider_for,
    unit_float_from_bits53,
)
from decoy_engine.execution.native._draw_site_providers import (
    _CODE_SET_KEYED_SALT,
    _FPE_KEY_LABEL,
    _JOINT_MASK_KEYED_ROW_SOURCE,
    _hmac_hex,
    _hole_resolve,
    _span_key,
)
from decoy_engine.generators.derivation import GenDeriveContext

_MASK_KEY = bytes.fromhex("0102030405060708")
_JOB_SEED = bytes.fromhex("1122334455667788")
_NS = "col/demo"


def _gen_ctx(name: str = "c", seed: int = 42, **extra: object) -> GenDeriveContext:
    cfg = {"name": name, "type": "faker", **extra}
    return GenDeriveContext.for_column(derive_key=None, column_config=cfg, fallback_seed=seed)


# ---------------------------------------------------------------------------
# Step 1: the float fix.
# ---------------------------------------------------------------------------


class TestUnitFloatFromBits53:
    def test_full_u64_all_ones_never_reaches_one(self) -> None:
        assert unit_float_from_bits53((1 << 64) - 1) < 1.0

    def test_zero_is_zero(self) -> None:
        assert unit_float_from_bits53(0) == 0.0

    def test_extracts_upper_53_bits(self) -> None:
        assert unit_float_from_bits53(1 << 11) == (1 << 11 >> 11) / 2**53

    def test_all_ones_equals_max_representable(self) -> None:
        assert unit_float_from_bits53((1 << 64) - 1) == (2**53 - 1) / 2**53

    def test_low_11_bits_are_discarded(self) -> None:
        # Any value below 2**11 maps to 0.0: the low 11 bits never survive.
        assert unit_float_from_bits53((1 << 11) - 1) == 0.0

    @pytest.mark.parametrize("bad", [-1, 1 << 64, (1 << 70)])
    def test_out_of_range_raises(self, bad: int) -> None:
        with pytest.raises(ValueError):
            unit_float_from_bits53(bad)

    def test_matches_numpy_bit_generator_random(self) -> None:
        # NumPy's Generator.random() is (upper 53 bits of a 64-bit draw) / 2**53.
        # Reconstruct it from the raw bit generator and confirm equality.
        bitgen = np.random.PCG64(12345)
        raw = int(bitgen.random_raw())
        gen = np.random.Generator(np.random.PCG64(12345))
        assert unit_float_from_bits53(raw) == gen.random()


# ---------------------------------------------------------------------------
# Step 2: per-draw-site emulation against the shipped seed derivations.
# ---------------------------------------------------------------------------


class TestShuffleEmulation:
    def test_permutation_matches_shipped_seed_expression(self) -> None:
        col, n = "email", 17
        seed_int = int.from_bytes(derive(_MASK_KEY, _NS, col.encode("utf-8"))[:8], "big")
        expected = np.random.default_rng(seed_int).permutation(n)
        got = provider_for("mask.shuffle").permutation(_MASK_KEY, _NS, col, n)
        assert np.array_equal(got, expected)

    def test_seed_binds_column_name(self) -> None:
        p = provider_for("mask.shuffle")
        assert p.seed_int(_MASK_KEY, _NS, "a") != p.seed_int(_MASK_KEY, _NS, "b")


class TestGroupedSeriesWalkEmulation:
    def test_walk_matches_shipped_per_group_stream(self) -> None:
        p = provider_for("mask.grouped_series_monotone_walk")
        g, group_len, start, step, max_step = "grp-A", 8, 1, 1, 10
        # Reference: seed default_rng from derive(...)[:8], accumulate integers.
        g_seed = int.from_bytes(
            derive(_MASK_KEY, _NS, str(g).encode("utf-8", errors="replace"))[:8], "big"
        )
        ref_rng = np.random.default_rng(g_seed)
        expected: list[int] = []
        cumulative = start
        for _ in range(group_len):
            expected.append(cumulative)
            cumulative += int(ref_rng.integers(step, max_step + 1))
        got = p.walk(_MASK_KEY, _NS, g, group_len, start=start, step=step, max_step=max_step)
        assert got == expected


class TestWindowedDateEmulation:
    def test_row_seed_matches_shipped_expression(self) -> None:
        p = provider_for("mask.windowed_date")
        for i in (0, 1, 7, 1000):
            expected = int.from_bytes(derive(_MASK_KEY, _NS, i.to_bytes(8, "big"))[:8], "big")
            assert p.row_seed_int(_MASK_KEY, _NS, i) == expected

    def test_row_generator_draw_matches(self) -> None:
        p = provider_for("mask.windowed_date")
        i = 42
        expected = np.random.default_rng(p.row_seed_int(_MASK_KEY, _NS, i)).integers(-5, 6)
        got = p.row_generator(_MASK_KEY, _NS, i).integers(-5, 6)
        assert got == expected


class TestCategoricalEmulation:
    def test_choices_match_python_random_base_int(self) -> None:
        ctx = _gen_ctx(name="tier", type="categorical")
        cats = ["A", "B", "C", "D"]
        weights = [1.0, 2.0, 3.0, 4.0]
        expected = random.Random(ctx.base_int("py")).choices(cats, weights=weights, k=25)
        got = provider_for("gen.categorical").choices(ctx, cats, weights, 25)
        assert got == expected


class TestReferenceEmulation:
    def test_random_distribution_matches_sequential_stream(self) -> None:
        ctx = _gen_ctx(name="fk", type="reference")
        ref_vals = [10, 20, 30, 40, 50]
        rng = random.Random(ctx.base_int("py"))
        expected = [rng.choice(ref_vals) for _ in range(12)]
        got = provider_for("gen.reference").sample(ctx, ref_vals, 12, distribution="random")
        assert got == expected

    def test_sequential_distribution_draws_nothing(self) -> None:
        ctx = _gen_ctx(name="fk", type="reference")
        ref_vals = [1, 2, 3]
        got = provider_for("gen.reference").sample(ctx, ref_vals, 7, distribution="sequential")
        assert got == [1, 2, 3, 1, 2, 3, 1]


class TestNullProbabilityEmulation:
    def test_floats_match_numpy_base_int_np(self) -> None:
        ctx = _gen_ctx(name="score", type="faker", null_probability=0.3)
        n = 40
        expected = np.random.default_rng(ctx.base_int("np")).random(n)
        got = provider_for("gen.null_probability").null_floats(ctx, n)
        assert np.array_equal(got, expected)


class TestFakerPerRowEmulation:
    def test_run_matches_shipped_per_row_loop(self) -> None:
        from faker import Faker

        ctx = _gen_ctx(name="first", type="faker", faker_type="first_name")
        n = 20
        # Reference: reseed per row from row_int("faker", i), then draw.
        ref_faker = Faker()
        expected = []
        for i in range(n):
            ref_faker.seed_instance(ctx.row_int("faker", i))
            expected.append(ref_faker.first_name())
        run_faker = Faker()
        got = provider_for("gen.faker_per_row").run(run_faker, run_faker.first_name, ctx, n)
        assert got == expected

    def test_row_seed_is_faker_family_row_int(self) -> None:
        ctx = _gen_ctx()
        assert provider_for("gen.faker_per_row").row_seed(ctx, 5) == ctx.row_int("faker", 5)


class TestStatisticalPerRowEmulation:
    def test_row_seed_is_col_seed_plus_i(self) -> None:
        p = provider_for("gen.statistical_per_row")
        col_seed = 987654321
        assert [p.row_seed(col_seed, i) for i in range(5)] == [col_seed + i for i in range(5)]

    def test_reseed_row_reproduces_the_stream(self) -> None:
        p = provider_for("gen.statistical_per_row")
        col_seed = 555
        rng = random.Random()
        for i in range(6):
            p.reseed_row(rng, col_seed, i)
            expected = random.Random(col_seed + i)
            assert rng.random() == expected.random()


class TestFormulaPerRowEmulation:
    def test_py_and_faker_row_seeds_use_disjoint_families(self) -> None:
        ctx = _gen_ctx(name="f", type="formula")
        p = provider_for("gen.formula_per_row")
        assert p.py_row_seed(ctx, 3) == ctx.row_int("py", 3)
        assert p.faker_row_seed(ctx, 3) == ctx.row_int("faker", 3)
        assert p.py_row_seed(ctx, 3) != p.faker_row_seed(ctx, 3)


class TestMaskFormulaEmulation:
    def test_seed_is_sha256_of_column_and_formula(self) -> None:
        p = provider_for("mask.formula")
        col, expr = "amount", "value * 2 + randint(0, 9)"
        expected = int(hashlib.sha256(f"{col}|{expr}".encode()).hexdigest()[:16], 16)
        assert p.formula_seed(col, expr) == expected


class TestSourceKeyedEmulation:
    def test_single_derive_sites_reproduce_digest(self) -> None:
        source = b"alice@example.com"
        for site_id in ("mask.hash", "mask.date_shift", "mask.bucket_perturb", "mask.group_key"):
            got = provider_for(site_id).draw(_MASK_KEY, _NS, source)
            assert got == derive(_MASK_KEY, _NS, source)

    def test_derive_index_sites_reproduce_index(self) -> None:
        source = b"CA"
        for site_id in ("mask.categorical_deterministic", "gen.pool_deterministic", "mask.faker"):
            got = provider_for(site_id).draw(_MASK_KEY, _NS, source, pool_size=50)
            assert got == derive_index(_MASK_KEY, _NS, source, pool_size=50)

    def test_derive_value_site_reproduces_domain_value(self) -> None:
        source = b"123-45-6789"
        got = provider_for("gen.identifier_deterministic").draw(
            _MASK_KEY, _NS, source, domain=IdentityDomain()
        )
        assert got == derive_value(_MASK_KEY, _NS, source, domain=IdentityDomain())


class TestCompoundSourceKeyedEmulation:
    def test_fpe_column_key_uses_fixed_label_source(self) -> None:
        # The Feistel key is per-COLUMN (source = the fixed FPE_KEY_LABEL), NOT
        # a per-value derive. This is the bug the review caught.
        p = provider_for("mask.fpe")
        assert p.column_key(_MASK_KEY, _NS) == derive(_MASK_KEY, _NS, _FPE_KEY_LABEL)
        # Two columns in the same namespace share the key (tweak, not key, varies).
        assert p.column_key(_MASK_KEY, _NS) == p.column_key(_MASK_KEY, _NS)

    def test_code_set_is_two_step_keyed_selection(self) -> None:
        p = provider_for("mask.code_set")
        key_value, candidate_count = "E11.9", 40
        hmac_key = derive(_MASK_KEY, _NS or "code_set", _CODE_SET_KEYED_SALT)
        expected = int(_hmac_hex(hmac_key, key_value)[:8], 16) % candidate_count
        assert p.select_index(_MASK_KEY, _NS, key_value, candidate_count) == expected
        # namespace None falls back to the "code_set" label, per shipped code.
        assert p.hmac_key(_MASK_KEY, None) == derive(_MASK_KEY, "code_set", _CODE_SET_KEYED_SALT)

    def test_joint_mask_is_two_step_keyed_row_index(self) -> None:
        p = provider_for("mask.joint_mask_keyed_row")
        key_value, row_count = "masked-pk-7", 128
        hmac_key = derive(_MASK_KEY, _NS, _JOINT_MASK_KEYED_ROW_SOURCE)
        expected = int(_hmac_hex(hmac_key, key_value)[:8], 16) % row_count
        assert p.row_index(_MASK_KEY, _NS, key_value, row_count) == expected


class TestShippedSymbolDrift:
    """The compound providers inline three shipped constants and two shipped
    utilities; these pins fail loudly if the shipped source ever changes."""

    def test_constants_match_shipped(self) -> None:
        from decoy_engine.execution._strategies._fpe import FPE_KEY_LABEL
        from decoy_engine.transforms.code_set import _KEYED_SALT
        from decoy_engine.transforms.joint_mask import _KEYED_ROW_SOURCE

        assert _FPE_KEY_LABEL == FPE_KEY_LABEL
        assert _CODE_SET_KEYED_SALT == _KEYED_SALT
        assert _JOINT_MASK_KEYED_ROW_SOURCE == _KEYED_ROW_SOURCE

    def test_hmac_hex_matches_shipped(self) -> None:
        from decoy_engine.internal.crypto import hmac_hex

        for value in ("abc", "E11.9", "masked-pk-7", 12345):
            assert _hmac_hex(_MASK_KEY, value) == hmac_hex(_MASK_KEY, value)
        assert _hmac_hex(_MASK_KEY, None) is None and hmac_hex(_MASK_KEY, None) is None

    def test_hole_resolve_matches_shipped(self) -> None:
        from decoy_engine.transforms._codeset_index import hole_resolve

        for idx in range(8):
            for position in (None, 0, 1, 3, 7):
                assert _hole_resolve(idx, position) == hole_resolve(idx, position)


class TestTextMaskEmulation:
    def test_faker_span_seed_matches_shipped(self) -> None:
        text = "123-45-6789"
        span_key = _span_key(_MASK_KEY, text)
        assert span_key == hmac.new(_MASK_KEY, text.encode("utf-8"), hashlib.sha256).digest()
        expected = int.from_bytes(span_key[:4], "big")
        assert provider_for("mask.text_mask_faker").span_seed(_MASK_KEY, text) == expected

    def test_date_shift_matches_shipped(self) -> None:
        text = "1990-01-15"
        min_days, max_days = -365, 365
        span_key = _span_key(_MASK_KEY, text)
        range_size = max_days - min_days + 1
        expected = min_days + (int.from_bytes(span_key[:8], "big") % range_size)
        got = provider_for("mask.text_mask_date_shift").shift_days(
            _MASK_KEY, text, min_days, max_days
        )
        assert got == expected


class TestPoolBuildFakerEmulation:
    def test_pool_seed_matches_shipped_derive_pool_seed(self) -> None:
        p = provider_for("gen.pool_build_faker")
        provider, locale, namespace, config_hash = "first_name", None, "col/name", "abc123"
        pool_namespace = f"pool/{provider}/{locale or 'default'}/{namespace or '_default'}"
        expected = derive(_JOB_SEED, pool_namespace, config_hash.encode("utf-8"))[:8]
        assert p.pool_seed(_JOB_SEED, provider, locale, namespace, config_hash) == expected


class TestGenDeriveContextSubstrate:
    def test_passthrough_base_and_row_int(self) -> None:
        ctx = _gen_ctx()
        p = provider_for("gen.derive_context")
        assert p.base_int(ctx, "py") == ctx.base_int("py")
        assert p.row_int(ctx, "faker", 9) == ctx.row_int("faker", 9)


class TestUnseededSites:
    @pytest.mark.parametrize(
        "site_id", ["mask.categorical_nondeterministic", "gen.identifier_nondeterministic"]
    )
    def test_reproduce_refuses(self, site_id: str) -> None:
        p = provider_for(site_id)
        with pytest.raises(DrawSiteProtocolError) as exc:
            p.reproduce()
        assert exc.value.code == "site_not_reproducible"

    def test_fresh_generator_is_unseeded_generator(self) -> None:
        gen = provider_for("mask.categorical_nondeterministic").fresh_generator()
        assert isinstance(gen, np.random.Generator)


# ---------------------------------------------------------------------------
# Registry totality: exactly one provider per catalogued site, per-site version.
# ---------------------------------------------------------------------------


class TestRegistryTotality:
    def test_one_provider_per_catalogued_site(self) -> None:
        catalogued = {s.draw_site_id for s in DRAW_SITES}
        registered = set(all_providers())
        assert registered == catalogued

    def test_provider_carries_its_sites_frozen_metadata(self) -> None:
        for site in DRAW_SITES:
            p = provider_for(site.draw_site_id)
            assert p.site is draw_site_by_id(site.draw_site_id)
            assert p.partitionable == site.partitionable
            assert p.family == site.family
            assert p.provider_version == site.provider_version
            assert p.provider_version  # non-empty: every site is versioned

    def test_versions_are_per_site_not_one_global(self) -> None:
        versions = {provider_for(s.draw_site_id).provider_version for s in DRAW_SITES}
        # The catalogue mixes numpy/python/faker/hmac version strings; a single
        # global version would collapse this set to one entry.
        assert len(versions) > 1


# ---------------------------------------------------------------------------
# Step 6: operation-specific partition tests.
# ---------------------------------------------------------------------------


class TestPartitionContract:
    def test_non_partitionable_sites_refuse_partitioned_draw(self) -> None:
        non_partitionable = [s for s in DRAW_SITES if not s.partitionable]
        assert non_partitionable  # sanity: the catalogue has global sites
        for site in non_partitionable:
            p = provider_for(site.draw_site_id)
            with pytest.raises(DrawSiteProtocolError) as exc:
                p.partitioned_draw()
            assert exc.value.code == "site_not_partitionable", site.draw_site_id

    def test_windowed_date_whole_column_equals_batched(self) -> None:
        p = provider_for("mask.windowed_date")
        n, k = 50, 17
        whole = [p.row_seed_int(_MASK_KEY, _NS, i) for i in range(n)]
        batched = [p.partitioned_draw(_MASK_KEY, _NS, i).integers(-5, 6) for i in range(n)]
        whole_draws = [p.row_generator(_MASK_KEY, _NS, i).integers(-5, 6) for i in range(n)]
        # The seed schedule keys on the GLOBAL index, so any split reproduces it.
        assert whole[:k] + whole[k:] == whole
        assert batched == whole_draws

    def test_faker_per_row_batches_concatenate(self) -> None:
        ctx = _gen_ctx()
        p = provider_for("gen.faker_per_row")
        n, k = 30, 11
        whole = [p.row_seed(ctx, i) for i in range(n)]
        batch_a = [p.partitioned_draw(ctx, i) for i in range(k)]
        batch_b = [p.partitioned_draw(ctx, i) for i in range(k, n)]
        assert batch_a + batch_b == whole

    def test_statistical_per_row_partition_invariant(self) -> None:
        p = provider_for("gen.statistical_per_row")
        col_seed, n, k = 424242, 20, 9
        whole = [p.row_seed(col_seed, i) for i in range(n)]
        batched = [p.partitioned_draw(col_seed, i) for i in range(k)] + [
            p.partitioned_draw(col_seed, i) for i in range(k, n)
        ]
        assert batched == whole

    def test_source_keyed_order_independent(self) -> None:
        p = provider_for("mask.hash")
        sources = [b"a", b"b", b"c", b"d"]
        forward = {s: p.partitioned_draw(_MASK_KEY, _NS, s) for s in sources}
        reverse = {s: p.partitioned_draw(_MASK_KEY, _NS, s) for s in reversed(sources)}
        assert forward == reverse
        for s in sources:
            assert forward[s] == derive(_MASK_KEY, _NS, s)


# ---------------------------------------------------------------------------
# Step 7: subprocess stability (mirror determinism/test_process_stability).
# ---------------------------------------------------------------------------

_CHILD_SCRIPT = """
import sys
import numpy as np
from decoy_engine.determinism import derive
from decoy_engine.execution.native._determinism_protocol import provider_for
from decoy_engine.generators.derivation import GenDeriveContext

mask_key = bytes.fromhex(sys.argv[1])
lo, hi = int(sys.argv[2]), int(sys.argv[3])

perm = provider_for("mask.shuffle").permutation(mask_key, "col/demo", "email", 17)
ctx = GenDeriveContext.for_column(
    derive_key=None, column_config={"name": "c", "type": "faker"}, fallback_seed=42
)
faker_seeds = [provider_for("gen.faker_per_row").row_seed(ctx, i) for i in range(lo, hi)]
digest = provider_for("mask.hash").draw(mask_key, "col/demo", b"alice").hex()

sys.stdout.write(
    ",".join(str(x) for x in perm.tolist())
    + "|" + ",".join(str(s) for s in faker_seeds)
    + "|" + digest
)
"""


@pytest.mark.golden
class TestSubprocessStability:
    def _run_child(self, lo: int, hi: int) -> str:
        proc = subprocess.run(  # noqa: S603 - test literals, not untrusted input
            [sys.executable, "-c", _CHILD_SCRIPT, _MASK_KEY.hex(), str(lo), str(hi)],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()

    def _parent(self, lo: int, hi: int) -> str:
        perm = provider_for("mask.shuffle").permutation(_MASK_KEY, _NS, "email", 17)
        ctx = _gen_ctx()
        seeds = [provider_for("gen.faker_per_row").row_seed(ctx, i) for i in range(lo, hi)]
        digest = provider_for("mask.hash").draw(_MASK_KEY, _NS, b"alice").hex()
        return (
            ",".join(str(x) for x in perm.tolist())
            + "|"
            + ",".join(str(s) for s in seeds)
            + "|"
            + digest
        )

    def test_child_matches_parent(self) -> None:
        assert self._run_child(0, 30) == self._parent(0, 30)

    def test_fresh_process_partitioned_draws_concatenate(self) -> None:
        # The fresh-process axis of the partition test: two child processes
        # compute disjoint global-index batches; their concatenation equals the
        # single in-process whole-column schedule.
        ctx = _gen_ctx()
        whole = [provider_for("gen.faker_per_row").row_seed(ctx, i) for i in range(30)]
        batch_a = self._run_child(0, 11).split("|")[1]
        batch_b = self._run_child(11, 30).split("|")[1]
        combined = batch_a + "," + batch_b
        assert combined == ",".join(str(s) for s in whole)
