"""ValuePool frozen-array + identity-tuple tests (S5 spec §2)."""

from __future__ import annotations

import numpy as np
import pytest

from decoy_engine.generation.pool._value_pool import (
    ValuePool,
    _freeze_array,
    estimate_pool_bytes,
)


def _make_pool(values: np.ndarray, **overrides) -> ValuePool:
    defaults = {
        "values": values,
        "provider": "person_email",
        "locale": "en_US",
        "config_hash": "abc",
        "seed": b"\x00" * 8,
        "size": len(values),
        "build_time_ms": 1.0,
        "backend_type": "faker",
        "backend_version": "25.4.0",
        "distinct_count": len(values),
    }
    defaults.update(overrides)
    return ValuePool(**defaults)


class TestFrozenness:
    def test_values_setflags_write_false_after_freeze(self) -> None:
        arr = np.array(["a", "b", "c"], dtype=object)
        _freeze_array(arr)
        with pytest.raises(ValueError):
            arr[0] = "x"

    def test_dataclass_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        pool = _make_pool(np.array(["a", "b"], dtype=object))
        with pytest.raises(FrozenInstanceError):
            pool.provider = "other"  # type: ignore[misc]


class TestIdentityTuple:
    def test_identity_is_five_tuple(self) -> None:
        pool = _make_pool(np.array(["a"], dtype=object))
        assert pool.identity == ("person_email", "en_US", "abc", b"\x00" * 8, 1)

    def test_identity_excludes_backend_version(self) -> None:
        """Per S5 spec §2: identity tuple is 5 fields; backend_version is
        NOT one of them. Faker patch shifts produce different pools
        (different values) but the cache key cannot depend on backend_version
        or every job would rebuild on every Faker patch."""
        pool_a = _make_pool(np.array(["a"], dtype=object), backend_version="25.4.0")
        pool_b = _make_pool(np.array(["a"], dtype=object), backend_version="26.0.0")
        # Same identity (5-tuple) despite different backend_version.
        assert pool_a.identity == pool_b.identity


class TestByteEstimate:
    def test_object_dtype_estimate_caps_per_string(self) -> None:
        # 3 short strings; estimate uses len*4 with cap.
        pool = _make_pool(np.array(["hi", "ok", "yo"], dtype=object))
        # 8 + 8 + 8 = 24
        assert estimate_pool_bytes(pool) == 24

    def test_numeric_dtype_uses_nbytes(self) -> None:
        arr = np.array([1, 2, 3, 4], dtype=np.int64)
        pool = _make_pool(arr)
        assert estimate_pool_bytes(pool) == arr.nbytes
