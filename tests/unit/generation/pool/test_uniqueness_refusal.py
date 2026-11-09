"""GEN-UNIQUE E1: the shared, value-free `uniqueness_impossible` refusal builder (U7, AT-10)."""

from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.generation.pool import (
    CardinalityMode,
    GenerationError,
    PoolSampler,
    raise_uniqueness_impossible,
)

MARKER = "MARKER-SECRET-VALUE"


def refuse(**kw):
    with pytest.raises(GenerationError) as exc:
        raise_uniqueness_impossible(**kw)
    return exc.value


def test_code_is_uniqueness_impossible():
    err = refuse(needed=5, available=3)
    assert err.code == "uniqueness_impossible"


def test_message_names_table_column_kind_counts_and_row():
    err = refuse(
        table="users", column="first_name", kind="faker", needed=50_000, available=9_411, row=9_411
    )
    assert "users.first_name" in err.message
    assert "unique faker" in err.message
    assert "50,000" in err.message
    assert "9,411" in err.message
    assert "row 9,412" in err.message


def test_message_without_place_is_still_readable():
    err = refuse(needed=4, available=3)
    assert "4" in err.message
    assert "3" in err.message
    assert " at row " not in err.message


@pytest.mark.parametrize("bad", [MARKER, True, 1.5, None])
def test_non_int_counts_are_refused_without_echoing_them(bad):
    with pytest.raises(TypeError) as exc:
        raise_uniqueness_impossible(needed=bad, available=1)
    assert MARKER not in str(exc.value)
    with pytest.raises(TypeError):
        raise_uniqueness_impossible(needed=1, available=bad)
    if bad is not None:  # row=None is the "no row" default
        with pytest.raises(TypeError):
            raise_uniqueness_impossible(needed=1, available=1, row=bad)


def test_signature_accepts_no_value_argument():
    # A caller cannot hand the builder a generated or parent value to format.
    with pytest.raises(TypeError):
        raise_uniqueness_impossible(needed=1, available=1, value=MARKER)  # type: ignore[call-arg]


class TestMaskingSamplerUsesTheBuilder:
    def test_unique_sample_refusal_carries_the_shared_message_shape(self, monkeypatch):
        calls = []
        import decoy_engine.generation.pool._sampler as sampler_mod

        real = sampler_mod.raise_uniqueness_impossible

        def spy(**kw):
            calls.append(kw)
            real(**kw)

        monkeypatch.setattr(sampler_mod, "raise_uniqueness_impossible", spy)
        from tests.unit.generation.pool.test_builder_sampler import _SEED, _builder

        pool = _builder().build("person_email", size=10, job_seed=_SEED)
        with pytest.raises(GenerationError) as exc:
            PoolSampler().sample(pool, n=20, mode=CardinalityMode.UNIQUE, seed=_SEED)
        assert exc.value.code == "uniqueness_impossible"
        assert calls == [{"needed": 20, "available": 10, "kind": "pool sample"}]

    def test_refusal_message_has_no_pool_values(self):
        from tests.unit.generation.pool.test_builder_sampler import _SEED, _builder

        pool = _builder().build("person_email", size=3, job_seed=_SEED)
        source = pd.Series([MARKER, "b", "c", "d", None])
        with pytest.raises(GenerationError) as exc:
            PoolSampler().sample(pool, n=5, mode=CardinalityMode.UNIQUE, seed=_SEED, source=source)
        assert MARKER not in exc.value.message
        for v in pool.values:
            assert str(v) not in exc.value.message


def test_remedy_comes_from_a_fixed_table_by_kind():
    masking = refuse(needed=2, available=1, kind="pool sample").message
    assert "row_count" not in masking and "drop unique" not in masking
    assert "row_count" in refuse(needed=2, available=1).message
    assert "tuples" in refuse(needed=2, available=1, kind="bundle sample").message


class TestBundleSampleRefusal:
    def _pool(self, size):
        from tests.unit.generation.pool.test_sampler_native_index_parity import _bundle_pool

        return _bundle_pool([(f"a{i}", f"b{i}") for i in range(size)], ("x", "y"))

    def _sample(self, pool, n):
        return PoolSampler().sample_bundle(pool, n, mode=CardinalityMode.UNIQUE, seed=b"\x00" * 8)

    def test_n_equal_to_pool_size_succeeds_with_distinct_tuples(self):
        out = self._sample(self._pool(4), 4)
        assert len(set(zip(out["x"], out["y"], strict=True))) == 4

    def test_one_past_pool_size_refuses_through_the_builder(self, monkeypatch):
        import decoy_engine.generation.pool._sampler as sampler_mod

        calls = []
        real = sampler_mod.raise_uniqueness_impossible

        def spy(**kw):
            calls.append(kw)
            real(**kw)

        monkeypatch.setattr(sampler_mod, "raise_uniqueness_impossible", spy)
        with pytest.raises(GenerationError) as exc:
            self._sample(self._pool(4), 5)
        assert exc.value.code == "uniqueness_impossible"
        assert calls == [{"needed": 5, "available": 4, "kind": "bundle sample"}]
