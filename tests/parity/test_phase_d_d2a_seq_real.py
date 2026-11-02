"""Phase D / D2a stage 2: the REAL compiled companion on the SEQ hash route.

Needs the abi-3 companion; skipped (not weakened) on a box without it, where the stand-in
suite (`test_phase_d_d2a_seq_native.py`) covers the same routing. These tests run the real
kernel and check what only it can show: native SEQ output equals the stage-1 frozen literals
and the full-frame pandas oracle on every strict dimension, equals the SEQ Python path for
each shape, is never recomputed in Python, and keeps the one accepted Unicode-version
divergence.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import _sequential_native
from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    load_compiled_crypto_kernel,
)
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _strict_compare as sc
from tests.parity._d1a_hash_data import EXPECTED_TOKENS, chain_sources, column_seed
from tests.parity._d2a_seq_data import (
    payload_seeds,
    payload_table,
    run_full,
    run_seq,
    sources_with_orphans,
    table_digest,
    typed_columns,
    with_when,
)
from tests.parity._seq_witness import hide_companion, install, spy_python_hash
from tests.parity.test_phase_d_d2a_seq_baseline import _DIGESTS


def _companion_loads() -> bool:
    try:
        load_compiled_crypto_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _companion_loads(),
    reason="decoy-engine-native abi-3 companion not loadable; the companion-present job covers this",
)

_ALL_TRUE = dict.fromkeys(sc.DIMENSIONS, True)
# "a" + U+0897 + U+0316: the post-Unicode-14 mark reorders under Unicode >= 16 only.
_SENSITIVE = "a\u0897\u0316"
_ORDERED = "a\u0316\u0897"
# Re-pinned under F13 (job_seed keying removed): Python hash of the reordered / unreordered form
# under THROWAWAY_KEY_PROVIDER; the real companion is unavailable here so these skip-guarded
# tokens are unverified against the kernel.
_SENSITIVE_NATIVE_TOKEN = "8db248d3f22ce7e8d7faa6ccb6a4ac7d25345afb124f6a09a85c6e827ba1e7e9"
_SENSITIVE_PYTHON_14_TOKEN = "376687f6c8fada32210019cf0c8063b7021044377c37949395cbae20efc58bc5"


class RecordingRealKernel:
    """Delegates to the real compiled kernel and records each call and the array it returned."""

    def __init__(self) -> None:
        self._real = load_compiled_crypto_kernel()
        self.types: list[pa.DataType] = []
        self.results: list[pa.Array] = []

    def derive_batch(self, values: Any, **kwargs: Any) -> pa.Array:
        self.types.append(values.type)
        out = self._real.derive_batch(values, **kwargs)
        self.results.append(out)
        return out


def test_native_chain_matches_the_frozen_literals_and_is_never_recomputed_in_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, RecordingRealKernel())
    python_rows = spy_python_hash(monkeypatch)
    out = run_seq(payload_table(), payload_seeds()).outputs
    _, expected = typed_columns()
    for name, tokens in expected.items():
        if name in out["t"].column_names:
            assert out["t"].column(name).to_pylist() == tokens, name
    assert out["child"].column("ck").to_pylist() == EXPECTED_TOKENS["hk"]
    assert python_rows == []
    assert len(kernel.results) == 14
    # Each emitted payload column is the kernel's own result, token for token.
    columns = [*typed_columns()[0], "c_trunc", "c_other_ns", "hk"]
    returned = sorted(repr(r.to_pylist()) for r in kernel.results)
    emitted = sorted(repr(out["t"].column(name).to_pylist()) for name in columns)
    assert emitted == returned
    for name, digest in _DIGESTS.items():
        assert table_digest(out[name]) == digest, name


def test_each_admitted_input_type_reaches_the_kernel_at_its_pinned_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install(monkeypatch, RecordingRealKernel())
    run_seq(payload_table(), payload_seeds())
    pinned = [a.type for a in typed_columns()[0].values()]
    # Dispatch order is the work list's, not the plan's, so compare as a multiset: the typed
    # columns at their pinned type, then c_trunc, c_other_ns and hk as utf8.
    expected = [*pinned, pa.string(), pa.string(), pa.string()]
    assert sorted(map(str, kernel.types)) == sorted(map(str, expected))


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.REMAP, OrphanPolicy.WARN])
def test_native_seq_agrees_with_the_full_frame_oracle_on_every_dimension(
    policy: OrphanPolicy,
) -> None:
    srcs = sources_with_orphans(payload_table(), ["orph1", "orph2"])
    seq = run_seq(payload_table(), payload_seeds(), policy=policy, sources=srcs).outputs
    full = run_full(payload_table(), payload_seeds(), policy=policy, sources=srcs).outputs
    for name in full:
        assert sc.compare_strict(full[name], seq[name]) == _ALL_TRUE, name


def _int_sources(child: list[int | None], parent_type: pa.DataType) -> dict[str, pa.Table]:
    srcs = chain_sources(pa.table({"a": pa.array(list("abcd"))}))
    t = srcs["t"]
    srcs["t"] = t.set_column(
        t.schema.get_field_index("hk"), "hk", pa.array([0, 1, 2, 3], parent_type)
    )
    srcs["child"] = pa.table({"ck": pa.array(child, pa.int64())})
    return srcs


_SHAPES: dict[str, Callable[[], dict[str, Any]]] = {
    "chain": lambda: {"payload": payload_table(), "seeds": payload_seeds()},
    "orphans-preserve": lambda: {
        "payload": payload_table(),
        "seeds": payload_seeds(),
        "sources": sources_with_orphans(payload_table(), ["orph1", "orph2"]),
    },
    "orphans-remap": lambda: {
        "payload": payload_table(),
        "seeds": payload_seeds(),
        "sources": sources_with_orphans(payload_table(), ["orph1", "orph2"]),
        "policy": OrphanPolicy.REMAP,
    },
    "int-remap-wide-orphan": lambda: {
        "payload": None,
        "seeds": (("a", column_seed()),),
        "sources": _int_sources([0, None, 2, 7, 300], pa.int8()),
        "policy": OrphanPolicy.REMAP,
    },
    "nullable-child": lambda: {
        "payload": None,
        "seeds": (("a", column_seed()),),
        "sources": _int_sources([0, None, 2, 3], pa.int64()),
    },
    "when-noncontiguous": lambda: {
        "payload": pa.table({"a": pa.array(["w", "x", "y", "z"])}),
        "seeds": (("a", with_when("fk == 1 or fk == 3")),),
    },
    "when-large-string-nulls": lambda: {
        "payload": pa.table({"a": pa.array(["w", "x", None, "z", "v"], pa.large_string())}),
        "seeds": (("a", with_when("fk >= 1 and fk <= 3")),),
    },
    "when-zero-match": lambda: {
        "payload": pa.table({"a": pa.array(["w", "x"])}),
        "seeds": (("a", with_when("fk > 100")),),
    },
    "all-null": lambda: {
        "payload": pa.table(
            {"a": pa.array([None] * 3, pa.string()), "c": pa.array([None] * 3, pa.bool_())}
        ),
        "seeds": (("a", column_seed()), ("c", column_seed())),
    },
    "empty": lambda: {
        "payload": pa.table({"a": pa.array([], pa.string()), "i": pa.array([], pa.int64())}),
        "seeds": (("a", column_seed()), ("i", column_seed())),
    },
    "truncate-and-namespace": lambda: {
        "payload": pa.table(
            {"a": pa.array(["alice", "bob", None]), "b": pa.array(["x", "y", "z"])}
        ),
        "seeds": (
            ("a", column_seed(provider_config=(("truncate", 5),))),
            ("b", column_seed(namespace="ns_other")),
        ),
    },
}


@pytest.mark.parametrize("shape", list(_SHAPES))
def test_native_and_python_seq_are_byte_identical_on_every_strict_dimension(
    shape: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = run_seq(**_SHAPES[shape]())
    entries = native.quality_metrics.get(_sequential_native.SEQ_HASH_DISPOSITION_KEY, [])
    with monkeypatch.context() as m:
        hide_companion(m)
        python = run_seq(**_SHAPES[shape]())
    assert set(native.outputs) == set(python.outputs)
    for name in python.outputs:
        assert sc.compare_strict(python.outputs[name], native.outputs[name]) == _ALL_TRUE, (
            shape,
            name,
        )
        assert table_digest(python.outputs[name]) == table_digest(native.outputs[name])
    assert any(e["executed_backend"] == "rust_companion" for e in entries) or shape in {
        "empty",
        "when-zero-match",
    }


def test_without_the_companion_the_run_is_the_frozen_python_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hide_companion(monkeypatch)
    out = run_seq(payload_table(), payload_seeds()).outputs
    for name, digest in _DIGESTS.items():
        assert table_digest(out[name]) == digest, name


# The one accepted divergence: the same kernel as D1a, so the same Unicode-table behavior.


def test_post_python_version_sequence_is_hashed_with_the_native_normalization() -> None:
    table = pa.table({"s": pa.array([_SENSITIVE, _ORDERED], pa.string())})
    got = run_seq(table, (("s", column_seed()),)).outputs["t"].column("s").to_pylist()
    assert got == [_SENSITIVE_NATIVE_TOKEN, _SENSITIVE_NATIVE_TOKEN]


def test_the_python_seq_path_keeps_the_older_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert tuple(int(p) for p in unicodedata.unidata_version.split(".")) < (16, 0, 0)
    table = pa.table({"s": pa.array([_SENSITIVE], pa.string())})
    hide_companion(monkeypatch)
    python = run_seq(table, (("s", column_seed()),)).outputs["t"].column("s").to_pylist()
    assert python != [_SENSITIVE_NATIVE_TOKEN]
    assert python == [_SENSITIVE_PYTHON_14_TOKEN] or unicodedata.unidata_version != "14.0.0"
