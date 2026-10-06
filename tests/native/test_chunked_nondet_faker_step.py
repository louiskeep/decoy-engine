"""C5b-ii acceptance: the positional sampler and the shared kernel step (plan tests 2, 3, 12).

Companion-independent: every case runs on the pure-Python `reference_index_derivation()` kernel,
which is byte-identical to the compiled one, so this always runs. The expected values come from
the scalar `derive_index`, never from the sampler under test.
"""

from __future__ import annotations

import inspect
from typing import Any

import numpy as np
import pyarrow as pa
import pytest

from decoy_engine import kernel
from decoy_engine.determinism import derive_index
from decoy_engine.execution.native._index_ext import reference_index_derivation
from decoy_engine.execution.native._operator_params import FakerParams
from decoy_engine.execution.native._operator_step import (
    run_kernel_step,
    sample_faker_array,
)
from decoy_engine.generation.pool import GenerationError, ValuePool

JOB = (0x0123456789).to_bytes(8, "big")
MASK = bytes(range(32))
SELECTION = "faker-nd/1:t/1:f"


def _pool(n: int = 16) -> ValuePool:
    values = [f"v{i}" for i in range(n)]
    return ValuePool(
        values=np.array(values, dtype=object),
        provider="person_first_name",
        locale="default",
        config_hash="test-hash",
        seed=b"test-seed",
        size=n,
        build_time_ms=0.0,
        backend_type="faker",
        backend_version="0",
        distinct_count=n,
    )


def _expected(
    pool: ValuePool, ordinals: range, *, key: bytes = JOB, ns: str = SELECTION
) -> list[str]:
    return [
        pool.values[derive_index(key, ns, kernel.encode_int(g), pool_size=pool.size)]
        for g in ordinals
    ]


def _sampler() -> Any:
    # Imported lazily so each case reports on its own before the sampler exists.
    from decoy_engine.execution.native import _operator_step

    return _operator_step.sample_faker_array_positional


def _positional(source: pa.Array, **kw: Any) -> pa.Array:
    args: dict[str, Any] = {
        "pool": _pool(),
        "row_offset": 0,
        "job_seed": JOB,
        "namespace": SELECTION,
        "index_kernel": reference_index_derivation(),
        "native_threads": None,
    }
    args.update(kw)
    return _sampler()(source, **args)


# ---------------------------------------------------------------------------
# The sampler.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("row_offset", [0, 5, 1000, 2**63 - 1, 2**63, 2**64 - 8])
def test_the_draw_is_the_scalar_formula_at_the_global_ordinals(row_offset: int) -> None:
    source = pa.array(["x"] * 8, pa.string())
    out = _positional(source, row_offset=row_offset)
    assert out.type == pa.string()
    assert out.to_pylist() == _expected(_pool(), range(row_offset, row_offset + 8))


def test_the_source_values_never_reach_the_draw() -> None:
    a = _positional(pa.array(["a"] * 6, pa.string()))
    b = _positional(pa.array(["zzz", "y", "q", "r", "s", "t"], pa.string()))
    assert a.to_pylist() == b.to_pylist()


def test_nulls_restore_from_the_source_and_still_consume_their_ordinal() -> None:
    source = pa.array(["a", None, "b", None, "c"], pa.string())
    out = _positional(source)
    want = _expected(_pool(), range(5))
    assert out.to_pylist() == [
        w if v is not None else None for v, w in zip(source.to_pylist(), want, strict=True)
    ]


def test_a_chunked_array_source_is_combined_positionally() -> None:
    source = pa.chunked_array([pa.array(["a", None, "b"]), pa.array(["c", "d", None])])
    out = _positional(source, row_offset=3)
    want = _expected(_pool(), range(3, 9))
    assert out.to_pylist() == [
        None if v is None else w for v, w in zip(source.to_pylist(), want, strict=True)
    ]


def test_the_sampler_has_no_mask_key_parameter() -> None:
    assert "mask_key" not in inspect.signature(_sampler()).parameters


def test_the_key_is_the_job_seed() -> None:
    out = _positional(pa.array(["x"] * 20, pa.string()))
    assert out.to_pylist() == _expected(_pool(), range(20), key=JOB)
    assert out.to_pylist() != _expected(_pool(), range(20), key=MASK)


def test_a_job_seed_of_none_is_an_assertion() -> None:
    with pytest.raises(AssertionError):
        _positional(pa.array(["x"], pa.string()), job_seed=None)


def test_a_range_past_the_uint64_domain_raises_the_faker_code() -> None:
    with pytest.raises(GenerationError) as info:
        _positional(pa.array(["a", "b"], pa.string()), row_offset=2**64 - 1)
    assert info.value.code == "faker_position_out_of_domain"
    from decoy_engine.execution._strategies._faker_positional import positional_pool_indices

    with pytest.raises(GenerationError) as oracle:
        positional_pool_indices(2, row_offset=2**64 - 1, job_seed=JOB, namespace="n", pool_size=4)
    assert oracle.value.code == info.value.code


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (pa.array([0, 1, 2], pa.int64()), "index_batch_type_mismatch"),
        ([0, 1, 2], "index_batch_type_mismatch"),
        (pa.array([0, 1], pa.uint64()), "index_batch_length_mismatch"),
        (pa.array([0, 1, 99], pa.uint64()), "index_batch_out_of_bounds"),
        (pa.array([0, None, 1], pa.uint64()), "index_batch_null_mask_mismatch"),
    ],
    ids=["wrong_dtype", "not_an_array", "wrong_length", "out_of_bounds", "null_index"],
)
def test_a_malformed_kernel_result_fails_closed(result: Any, code: str) -> None:
    class _Bad:
        def derive_index_batch(self, *args: Any, **kwargs: Any) -> Any:
            return result

    with pytest.raises(GenerationError) as info:
        _positional(pa.array(["a", "b", "c"], pa.string()), index_kernel=_Bad())
    assert info.value.code == code


# ---------------------------------------------------------------------------
# 12. The step contract.
# ---------------------------------------------------------------------------


def _params() -> FakerParams:
    return FakerParams("ns_f", positional=True, selection_namespace=SELECTION)


def _step(source: pa.Array, **kw: Any) -> Any:
    args: dict[str, Any] = {
        "mask_key": MASK,
        "native_threads": None,
        "index_kernel": reference_index_derivation(),
        "pool": _pool(),
        "row_offset": 4,
        "job_seed": JOB,
    }
    args.update(kw)
    return run_kernel_step(_params(), source, **args)


def test_a_non_empty_source_runs_and_reports_ran() -> None:
    result = _step(pa.array(["a", None, "b"], pa.string()))
    assert result.ran is True
    want = _expected(_pool(), range(4, 7))
    assert result.out.to_pylist() == [want[0], None, want[2]]


def test_an_all_null_non_empty_source_still_runs_the_kernel() -> None:
    result = _step(pa.array([None, None], pa.string()))
    assert result.ran is True
    assert result.out.to_pylist() == [None, None]


def test_a_zero_row_source_returns_a_typed_empty_array_without_calling_the_kernel() -> None:
    class _Spy:
        calls = 0

        def derive_index_batch(self, *args: Any, **kwargs: Any) -> Any:
            type(self).calls += 1
            raise AssertionError("the kernel must not run for a zero-row chunk")

    result = _step(pa.array([], pa.string()), index_kernel=_Spy())
    assert result.ran is False
    assert result.out.type == pa.string() and len(result.out) == 0
    assert _Spy.calls == 0


def test_positional_faker_without_a_job_seed_asserts() -> None:
    with pytest.raises(AssertionError):
        _step(pa.array(["a"], pa.string()), job_seed=None)


def test_a_zero_row_source_with_no_job_seed_still_asserts() -> None:
    with pytest.raises(AssertionError):
        _step(pa.array([], pa.string()), job_seed=None)


def test_the_step_never_reads_the_mask_key_for_the_positional_draw() -> None:
    a = _step(pa.array(["a"] * 10, pa.string()), mask_key=MASK)
    b = _step(pa.array(["a"] * 10, pa.string()), mask_key=b"\x01" * 32)
    assert a.out.to_pylist() == b.out.to_pylist()


def test_the_deterministic_step_path_is_unchanged() -> None:
    pool = _pool()
    source = pa.array(["a", "b", None, "a"], pa.string())
    ref = reference_index_derivation()
    result = run_kernel_step(
        FakerParams("ns_f"),
        source,
        mask_key=MASK,
        native_threads=None,
        index_kernel=ref,
        pool=pool,
    )
    direct = sample_faker_array(
        source, pool=pool, namespace="ns_f", mask_key=MASK, index_kernel=ref, native_threads=None
    )
    assert result.ran is True
    assert result.out.equals(direct)


def test_the_params_defaults_are_the_deterministic_shape() -> None:
    params = FakerParams("ns_f")
    assert params.positional is False and params.selection_namespace is None
