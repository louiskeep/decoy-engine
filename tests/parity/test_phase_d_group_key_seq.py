"""B7 acceptance: native group_key on the sequential (SEQ) route.

Plan: docs/plans/2026-10-26-substrate-route-coverage.md (B7). group_key had no SEQ
native admission before this slice, so the native-consumption assertions here fail
on the base commit (the Python handler ran, leaving unmarked tokens).

group_key derives one key per row from a SIBLING `group_by` column. The SEQ seam
reads that sibling from the adapter's `to_pandas_fk_safe` frame (the same frame the
oracle reads) and selects through the shared raw-hex kernel. Compared here: the SEQ
route vs the full-frame pandas oracle (`run_full`), for string / nullable int64 /
bool siblings, non-default length and prefix, repeated and null siblings, and a
value past 2**53 (the int-null widening trap); the compiled selection actually
consumed; the unsupported sibling shapes declined to Python with a caller-visible
reason; the companion-absent Python path; and an FK-key group_key staying Python.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import _sequential_native, _sequential_native_group_key
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._group_key_ext import load_compiled_raw_hex_kernel
from decoy_engine.plan._types import ColumnSeed
from tests.parity._d1a_hash_data import column_seed
from tests.parity._d2b_shapes import (
    Shape,
    edge,
    graph,
    hashed,
    make_plan,
    passthrough,
    run_full,
    run_seq,
    strs,
    table_seed,
)

_GK_MARK = "gknative:"


def _kernel_loads() -> bool:
    try:
        load_compiled_raw_hex_kernel()
        return True
    except CryptoExtensionUnavailableError:
        return False


pytestmark = pytest.mark.skipif(
    not _kernel_loads(),
    reason="decoy-engine-native raw-hex kernel not loadable; the companion-present job covers this",
)


def _gk_seed(group_by: str = "sib", *, length: int = 16, prefix: str = "") -> ColumnSeed:
    return column_seed(
        "group_key",
        namespace=None,
        provider_config=(("group_by", group_by), ("length", length), ("prefix", prefix)),
    )


def _shape(
    sib: pa.Array,
    *,
    group_by: str = "sib",
    length: int = 16,
    prefix: str = "",
    sib_strategy: ColumnSeed | None = None,
    target: str = "g",
) -> Shape:
    """A parent `p.pk` -> child `t.fk`, with group_key column `t.<target>` keyed on `t.sib`.

    `sib` is the sibling column's values and type (what the crux turns on). `sib_strategy`
    overrides the sibling's seed (default passthrough); pass a hashed seed to make it a
    masked sibling, which must decline."""
    n = len(sib)
    fk = strs([f"r{i}" for i in range(n)])
    t_columns: dict[str, ColumnSeed] = {
        "fk": passthrough(),
        "sib": sib_strategy or passthrough(),
        target: _gk_seed(group_by, length=length, prefix=prefix),
    }
    plan = make_plan(
        {
            "p": table_seed({"pk": hashed("ns_p")}),
            "t": table_seed(t_columns),
        }
    )
    g = graph(edge("p", "pk", "t", "fk", "ns_p"))
    sources = {
        "p": pa.table({"pk": strs([f"r{i}" for i in range(n)])}),
        "t": pa.table({"fk": fk, "sib": sib, target: strs([""] * n)}),
    }
    return Shape(plan, g, sources)


def _col(result: Any, table: str, column: str) -> list[Any]:
    return result.outputs[table].column(column).combine_chunks().to_pylist()


# --- parity: SEQ native == the full-frame oracle, across sibling types -------------------------

_STRING_SIB = strs(["grp-a", "grp-b", "grp-a", "", "héllo", None, "grp-b", "日本語"])
_INT_SIB = pa.array([1, 2, 1, 2**53 + 7, 5, None, 2, None], pa.int64())  # nulls -> Int64, not float64
_BOOL_SIB = pa.array([True, False, True, None, False, True, None, False], pa.bool_())


@pytest.mark.parametrize(
    "sib",
    [pytest.param(_STRING_SIB, id="string"), pytest.param(_INT_SIB, id="int64-with-nulls"),
     pytest.param(_BOOL_SIB, id="bool")],
)
def test_seq_native_group_key_is_byte_identical_to_the_oracle(sib: pa.Array) -> None:
    shape = _shape(sib)
    assert _col(run_seq(shape), "t", "g") == _col(run_full(shape), "t", "g")


@pytest.mark.parametrize("length", [8, 16, 32, 64])
@pytest.mark.parametrize("prefix", ["", "GK-", "member_"])
def test_non_default_length_and_prefix_match_the_oracle(length: int, prefix: str) -> None:
    shape = _shape(_STRING_SIB, length=length, prefix=prefix)
    got = _col(run_seq(shape), "t", "g")
    assert got == _col(run_full(shape), "t", "g")
    assert all(v.startswith(prefix) for v in got)
    # length is hex chars of the key, after the prefix.
    assert all(len(v) == len(prefix) + length for v in got)


def test_repeated_sibling_values_share_one_key() -> None:
    got = _col(run_seq(_shape(_STRING_SIB)), "t", "g")
    assert got[0] == got[2]  # both "grp-a"
    assert got[1] == got[6]  # both "grp-b"
    assert got[0] != got[1]  # group never emits a null; even "" and None get a key
    assert all(v is not None for v in got)


# --- compiled consumption witness --------------------------------------------------------------


class _MarkingRawHexKernel:
    """Wraps the real raw-hex kernel and prefixes every key, so a consumed native result is
    visible in the emitted column (SEQ round-trips through pandas, so buffer identity cannot
    be checked; the mark is the causal proof, as `_seq_witness` does for hash)."""

    def __init__(self, real: Any) -> None:
        self.real = real
        self.calls: list[int] = []

    def derive_hex_raw_batch(
        self, values: Any, *, mask_key: Any, namespace: str, hex_chars: int, native_threads: Any = None
    ) -> pa.Array:
        self.calls.append(len(values))
        base = self.real.derive_hex_raw_batch(
            values, mask_key=mask_key, namespace=namespace, hex_chars=hex_chars,
            native_threads=native_threads,
        )
        return pa.array([_GK_MARK + v for v in base.to_pylist()], pa.string())


def _install_marking(monkeypatch: pytest.MonkeyPatch) -> _MarkingRawHexKernel:
    kernel = _MarkingRawHexKernel(load_compiled_raw_hex_kernel())
    # `load_raw_hex` resolves `load_compiled_raw_hex_kernel` against the group_key sibling module's
    # globals, so the witness patches it there (not on `_sequential_native`, which no longer imports it).
    monkeypatch.setattr(_sequential_native_group_key, "load_compiled_raw_hex_kernel", lambda: kernel)
    return kernel


def test_the_kernel_result_is_consumed_not_recomputed(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = _install_marking(monkeypatch)
    got = _col(run_seq(_shape(_STRING_SIB)), "t", "g")
    assert kernel.calls  # the raw-hex kernel ran
    assert all(_GK_MARK in v for v in got)  # and its distinguishable output is what was emitted


def test_disposition_records_the_native_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_marking(monkeypatch)
    result = run_seq(_shape(_STRING_SIB))
    entries = {e["column"]: e for e in result.quality_metrics["sequential_group_key"]}
    assert entries["g"]["executed_backend"] == "rust_companion"
    assert "reason" not in entries["g"]


# --- declines: unsupported sibling shapes stay on the Python handler (and still match) ----------


def test_companion_absent_runs_python_and_matches_the_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> Any:
        raise CryptoExtensionUnavailableError("raw-hex companion hidden for a Python-only control")

    monkeypatch.setattr(_sequential_native_group_key, "load_compiled_raw_hex_kernel", unavailable)
    shape = _shape(_STRING_SIB)
    got = _col(run_seq(shape), "t", "g")
    assert got == _col(run_full(shape), "t", "g")  # the Python handler is byte-identical


@pytest.mark.parametrize(
    "shape_kwargs, reason_fragment",
    [
        pytest.param({"group_by": "g"}, "group_key_self_anchor_not_native", id="self-anchor"),
        pytest.param(
            {"sib_strategy": hashed("ns_sib")},
            "group_key_masked_sibling_not_native",
            id="masked-sibling",
        ),
    ],
)
def test_unsupported_sibling_shapes_decline_to_python(
    shape_kwargs: dict[str, Any], reason_fragment: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = _install_marking(monkeypatch)
    shape = _shape(_STRING_SIB, **shape_kwargs)
    result = run_seq(shape)
    got = _col(result, "t", "g")
    assert all(_GK_MARK not in v for v in got)  # the kernel result was not used
    entries = {e["column"]: e for e in result.quality_metrics["sequential_group_key"]}
    assert entries["g"]["executed_backend"] == "python_scalar"
    assert reason_fragment in entries["g"]["reason"]
    # The Python handler still produced the oracle's keys.
    assert got == _col(run_full(shape), "t", "g")
    del kernel


def test_float_sibling_declines_but_still_masks() -> None:
    float_sib = pa.array([1.0, 2.0, 1.0, 3.0, 5.0, None, 2.0, 4.0], pa.float64())
    shape = _shape(float_sib)
    result = run_seq(shape)
    entries = {e["column"]: e for e in result.quality_metrics["sequential_group_key"]}
    assert entries["g"]["executed_backend"] == "python_scalar"
    assert "group_key_sibling_type_not_native" in entries["g"]["reason"]
    assert _col(result, "t", "g") == _col(run_full(shape), "t", "g")


def test_group_key_is_never_added_to_the_seq_key_strategies() -> None:
    # group_key output is not injective, so it must never be accelerated as an FK key.
    assert "group_key" not in _sequential_native._KEY_STRATEGIES


def test_empty_table_runs_no_kernel_and_matches_the_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = _install_marking(monkeypatch)
    shape = _shape(strs([]))
    got = _col(run_seq(shape), "t", "g")
    assert got == _col(run_full(shape), "t", "g") == []
    assert kernel.calls == []  # no rows, no kernel call
