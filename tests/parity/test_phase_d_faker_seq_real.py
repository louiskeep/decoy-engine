"""Phase D / faker SEQ-native on the REAL compiled index kernel, against the Python handler.

Needs the abi-3 companion (skipped without it). Every masking cell runs the full-frame oracle
(the shipped Python faker handler), the SEQ route with the companion, and the SEQ route with
the index kernel hidden, and asserts all nine strict dimensions between each pair. The native
witness is the `sequential_faker` evidence entry (`executed_backend == rust_companion` and
`native_calls`), because the Python handler reaches the same index kernel and a kernel spy
alone cannot tell the two apart.

Acceptance F1 to F5, F7 (mixed job) and F8 of docs/plans/2026-10-13-phase-d-faker-seq-native.md.
The routing cases (F7, F7b) and the companion-independent declines are in
`test_phase_d_faker_seq.py`.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import SecretKeyProvider
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._sequential_native import SeqNativeRun
from decoy_engine.execution._sequential_native_ops import (
    SEQ_CATEGORICAL_DISPOSITION_KEY,
    SEQ_FAKER_DISPOSITION_KEY,
    SEQ_FPE_DISPOSITION_KEY,
    SEQ_HASH_DISPOSITION_KEY,
)
from decoy_engine.execution._strategies._faker import FakerStrategyHandler
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._index_ext import load_compiled_index_kernel
from decoy_engine.generation.pool import PoolCache, ValuePool
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from tests.parity._d1a_hash_data import column_seed
from tests.parity._d2b_shapes import run_full, run_seq, strs
from tests.parity._d2c_cat_data import cat_seed, hide_cat, install_cat, payload_shape
from tests.parity._faker_seq_data import (
    FIRST,
    LAST,
    NS,
    PASSTHROUGH,
    SRC,
    Trio,
    assert_native_witness,
    assert_python_ran,
    assert_trio_strict,
    cells,
    faker_entries,
    faker_entry,
    faker_seed,
    gated,
    positional_seed,
    run_trio,
    with_job_seed,
)


def _companion_loads() -> bool:
    try:
        load_compiled_index_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


pytestmark = pytest.mark.skipif(not _companion_loads(), reason="needs the abi-3 companion")

UNAVAILABLE = "crypto_extension_unavailable"


def _python_control(trio: Trio, column: str) -> None:
    """The hidden-kernel run is the Python handler and says so, with no native call."""
    assert_python_ran(trio.python, column, UNAVAILABLE)


# F1: deterministic REUSE, allowlisted provider, string source.


@pytest.mark.parametrize("provider", [FIRST, LAST])
@pytest.mark.parametrize("pool_size", [2, 50, 400])
@pytest.mark.parametrize("rows", [1, 7, 500, 3000])
def test_f1_deterministic_reuse_is_byte_identical_to_the_oracle(
    provider: str, pool_size: int, rows: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = payload_shape({"f": (cells(rows), faker_seed(provider=provider, pool_size=pool_size))})
    trio = run_trio(shape, monkeypatch)
    assert_trio_strict(trio)
    assert_native_witness(trio.native, "f")
    _python_control(trio, "f")


def test_f1_the_same_source_value_picks_the_same_name_wherever_it_sits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Deterministic keys on the source value, not the row: a reordered column permutes the output.
    forward = ["u1", "u2", "u3", "u4", "u5", "u6"]
    shape_a = payload_shape({"f": (strs(forward), faker_seed())})
    shape_b = payload_shape({"f": (strs(forward[::-1]), faker_seed())})
    a = run_seq(shape_a).outputs["P"].column("f").to_pylist()
    b = run_seq(shape_b).outputs["P"].column("f").to_pylist()
    assert a == b[::-1]
    assert_trio_strict(run_trio(shape_b, monkeypatch))


# F2: positional (non-deterministic REUSE), no `when:` gate.


@pytest.mark.parametrize("provider", [FIRST, LAST])
@pytest.mark.parametrize("rows", [1, 7, 500, 3000])
def test_f2_positional_reuse_is_byte_identical_to_the_oracle(
    provider: str, rows: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = payload_shape({"f": (cells(rows), positional_seed(provider=provider))})
    trio = run_trio(shape, monkeypatch)
    assert_trio_strict(trio)
    assert_native_witness(trio.native, "f")
    _python_control(trio, "f")


def test_f2_positional_without_an_explicit_pool_size_is_native_and_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The positional leg needs only REUSE; the pool size resolves through the shared identity.
    shape = payload_shape({"f": (cells(40), positional_seed(pool_size=None))})
    trio = run_trio(shape, monkeypatch)
    assert_trio_strict(trio)
    assert_native_witness(trio.native, "f")


def test_f2_positional_ignores_the_source_value(monkeypatch: pytest.MonkeyPatch) -> None:
    one = payload_shape({"f": (strs([f"a{i}" for i in range(9)]), positional_seed())})
    two = payload_shape({"f": (strs([f"zz{i}" for i in range(9)]), positional_seed())})
    assert (
        run_seq(one).outputs["P"].column("f").to_pylist()
        == run_seq(two).outputs["P"].column("f").to_pylist()
    )
    assert_trio_strict(run_trio(two, monkeypatch))


# F2b: positional under a `when:` gate keys on the FULL-TABLE position.

_FLAG = [0, 1, 0, 1, 0, 0, 0, 1, 0, 0, 1, 1]
_N = len(_FLAG)
_GATES = {
    "partial-non-contiguous": (_FLAG, "flag == 1"),  # positions 1, 3, 7, 10, 11
    "all-rows": ([1] * _N, "flag == 1"),
    "zero-match": ([0] * _N, "flag == 1"),
    "single-row": ([0] * 8 + [1] + [0] * 3, "flag == 1"),
}


@pytest.mark.parametrize("gate", list(_GATES))
@pytest.mark.parametrize("deterministic", [True, False])
def test_f2b_a_when_gate_keeps_the_full_table_keying(
    gate: str, deterministic: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    flag, predicate = _GATES[gate]
    source = strs([None if i % 5 == 4 else f"s{i}" for i in range(_N)])
    seed = faker_seed() if deterministic else positional_seed()
    shape = gated({"f": (source, seed)}, flag, predicate)
    trio = run_trio(shape, monkeypatch)
    assert_trio_strict(trio)
    selected = sum(flag)
    entry = faker_entry(trio.native, "f")
    if selected:
        assert_native_witness(trio.native, "f")
    else:
        # Nothing selected: the handler masks no rows, so no kernel ran.
        assert entry["native_calls"] == 0 and entry["executed_backend"] == "none"


def test_f2b_a_gated_positional_row_draws_what_the_ungated_run_drew_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The keying Codex flagged: rows 1, 3, 7 under a gate must take the values the SAME rows
    # take when nothing is gated, not the values rows 0, 1, 2 would take at offset 0.
    source = strs([f"s{i}" for i in range(_N)])
    ungated = run_seq(payload_shape({"f": (source, positional_seed())})).outputs["P"]
    shape = gated({"f": (source, positional_seed())}, _FLAG, "flag == 1")
    got = run_seq(shape).outputs["P"].column("f").to_pylist()
    want = ungated.column("f").to_pylist()
    for i, selected in enumerate(_FLAG):
        assert got[i] == (want[i] if selected else f"s{i}"), i
    assert_trio_strict(run_trio(shape, monkeypatch))


# F3: null handling.


@pytest.mark.parametrize("deterministic", [True, False])
@pytest.mark.parametrize(
    "values",
    [
        [None, "a", None, "b", None, None, "c"],
        [None] + [f"x{i}" for i in range(30)] + [None],
        ["", None, "", "k", None],
    ],
    ids=["leading-and-runs", "bookends", "empty-strings-are-not-null"],
)
def test_f3_nulls_stay_where_they_were(
    deterministic: bool, values: list[str | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = faker_seed() if deterministic else positional_seed()
    shape = payload_shape({"f": (strs(values), seed)})
    trio = run_trio(shape, monkeypatch)
    assert_trio_strict(trio)
    out = trio.native.outputs["P"].column("f").to_pylist()
    assert [v is None for v in out] == [v is None for v in values]
    assert_native_witness(trio.native, "f")


# F4: determinism and keying.


@pytest.mark.parametrize("deterministic", [True, False])
def test_f4_the_same_job_twice_gives_identical_output(deterministic: bool) -> None:
    seed = faker_seed() if deterministic else positional_seed()
    shape = payload_shape({"f": (cells(300), seed)})
    first = run_seq(shape).outputs["P"].column("f").to_pylist()
    again = run_seq(shape).outputs["P"].column("f").to_pylist()
    assert first == again


@pytest.mark.parametrize("deterministic", [True, False])
def test_f4_the_job_seed_is_part_of_the_key(
    deterministic: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = faker_seed() if deterministic else positional_seed()
    shape = payload_shape({"f": (cells(300), seed)})
    other = with_job_seed(shape, b"\x22" * 8)
    a = run_seq(shape).outputs["P"].column("f").to_pylist()
    b = run_seq(other).outputs["P"].column("f").to_pylist()
    assert a != b
    assert_trio_strict(run_trio(other, monkeypatch))


def test_f4_the_deterministic_namespace_is_part_of_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = cells(200)
    a = run_seq(payload_shape({"f": (source, faker_seed("ns_one"))}))
    b = run_seq(payload_shape({"f": (source, faker_seed("ns_two"))}))
    assert a.outputs["P"].column("f").to_pylist() != b.outputs["P"].column("f").to_pylist()
    assert_trio_strict(run_trio(payload_shape({"f": (source, faker_seed("ns_two"))}), monkeypatch))


def test_f4_default_and_explicit_selection_namespaces_key_differently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = cells(200)
    default = payload_shape({"f": (source, positional_seed(None))})
    explicit = payload_shape({"f": (source, positional_seed(NS))})
    d = run_seq(default).outputs["P"].column("f").to_pylist()
    e = run_seq(explicit).outputs["P"].column("f").to_pylist()
    assert d != e
    for shape in (default, explicit):
        trio = run_trio(shape, monkeypatch)
        assert_trio_strict(trio)
        assert_native_witness(trio.native, "f")


def test_f4_two_unnamespaced_columns_of_one_table_do_not_share_a_draw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The default namespace is per table and column, so f and g draw different positions.
    source = cells(200)
    shape = payload_shape(
        {"f": (source, positional_seed(None)), "g": (source, positional_seed(None))}
    )
    out = run_seq(shape).outputs["P"]
    assert out.column("f").to_pylist() != out.column("g").to_pylist()
    assert_trio_strict(run_trio(shape, monkeypatch))


SECRET = SecretKeyProvider(b"a-strong-32B+-managed-secret-value!!", key_version="v1")


def test_f4_deterministic_keys_on_the_secret_and_positional_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With a managed secret mask_key differs from job_seed. The deterministic draw re-keys onto
    # it (it re-identifies a source value); the positional draw stays on job_seed.
    det = payload_shape({"f": (cells(200), faker_seed())})
    pos = payload_shape({"f": (cells(200), positional_seed())})
    for shape, keyed in ((det, True), (pos, False)):
        plain = run_seq(shape).outputs["P"].column("f").to_pylist()
        trio = run_trio(shape, monkeypatch, key_provider=SECRET)
        assert_trio_strict(trio)
        assert_native_witness(trio.native, "f")
        secret = trio.native.outputs["P"].column("f").to_pylist()
        assert (secret != plain) is keyed


def _seam_run(shape: Any) -> tuple[SeqNativeRun, Any]:
    run = SeqNativeRun()
    run.prepare_table(shape.plan, "P", shape.sources["P"].schema, shape.graph)
    return run, shape.plan.seed_envelope.job_seed


def _seam_ctx(job_seed: bytes, run: SeqNativeRun | None, **kw: Any) -> StrategyContext:
    return StrategyContext(
        registry=get_default_registry(),
        pool_cache=PoolCache(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
        job_seed=job_seed,
        mask_key=job_seed,
        current_table="P",
        seq_native=run,
        **kw,
    )


@pytest.mark.parametrize(
    "gate",
    [None, np.array([1, 3, 7], dtype=np.int64), np.array([0], dtype=np.int64)],
    ids=["ungated", "gated-subset", "gated-first-row"],
)
@pytest.mark.parametrize("offset", [0, 11, 5000])
def test_f2b_the_seam_forwards_row_offset_and_gate_positions_to_the_kernel(
    offset: int, gate: Any
) -> None:
    # The SEQ route stamps row_offset 0, so the offset leg is proven at the seam: the same
    # handler call with and without the SEQ seam must draw the same positional values.
    n = 3 if gate is not None and len(gate) == 3 else 1 if gate is not None else 9
    source = [f"s{i}" for i in range(n)]
    shape = payload_shape({"f": (strs(source), positional_seed())})
    run, job_seed = _seam_run(shape)
    kwargs: dict[str, Any] = {"row_offset": offset, "gate_positions": gate}
    native = (
        FakerStrategyHandler()
        .run(
            pd.DataFrame({"f": source}), "f", positional_seed(), _seam_ctx(job_seed, run, **kwargs)
        )[0]["f"]
        .tolist()
    )
    python = (
        FakerStrategyHandler()
        .run(
            pd.DataFrame({"f": source}), "f", positional_seed(), _seam_ctx(job_seed, None, **kwargs)
        )[0]["f"]
        .tolist()
    )
    assert native == python
    assert run.metrics()[SEQ_FAKER_DISPOSITION_KEY][0]["native_calls"] == 1
    # And the keying is real: moving the offset changes the draw.
    shifted = (
        FakerStrategyHandler()
        .run(
            pd.DataFrame({"f": source}),
            "f",
            positional_seed(),
            _seam_ctx(job_seed, None, row_offset=offset + 1, gate_positions=gate),
        )[0]["f"]
        .tolist()
    )
    assert shifted != python


# F4b: thread-count invariance.


class _PinnedThreads:
    """Delegates to the real index kernel and forces the thread grant it is called with."""

    def __init__(self, threads: int) -> None:
        self._real = load_compiled_index_kernel()
        self._threads = threads
        self.grants: list[int | None] = []

    def derive_index_batch(self, values: Any, **kwargs: Any) -> Any:
        kwargs["native_threads"] = self._threads
        self.grants.append(self._threads)
        return self._real.derive_index_batch(values, **kwargs)


@pytest.mark.parametrize("deterministic", [True, False])
def test_f4b_native_output_is_identical_for_one_thread_and_many(
    deterministic: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = faker_seed() if deterministic else positional_seed()
    shape = payload_shape({"f": (cells(20_000), seed)})
    outputs = []
    for threads in (1, 4):
        pinned = install_cat(monkeypatch, _PinnedThreads(threads))
        result = run_seq(shape)
        assert pinned.grants == [threads]
        assert_native_witness(result, "f")
        outputs.append(result.outputs["P"].column("f").to_pylist())
    assert outputs[0] == outputs[1]
    assert outputs[0] == run_full(shape).outputs["P"].column("f").to_pylist()


# F5: declines stay Python, with a coded reason, and the output still matches the oracle.


def _declined(trio: Trio, column: str, reason: str) -> None:
    assert_trio_strict(trio)
    assert_python_ran(trio.native, column, reason)


@pytest.mark.parametrize("mode", ["unique", "match_source_cardinality", "scale_source_cardinality"])
def test_f5a_whole_column_cardinality_modes_stay_python(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = payload_shape({"f": (cells(30), positional_seed(mode=mode, pool_size=200))})
    trio = run_trio(shape, monkeypatch)
    _declined(trio, "f", f"faker_nondeterministic_mode_stays_python:f:{mode}")


def test_f5b_a_provider_outside_the_allowlist_stays_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for seed in (faker_seed(provider="person_email"), positional_seed(provider="person_email")):
        trio = run_trio(payload_shape({"f": (cells(40), seed)}), monkeypatch)
        _declined(trio, "f", "faker_provider_not_native:f:person_email")


@pytest.mark.parametrize(
    ("source", "type_name"),
    [
        (pa.array(list(range(20)), pa.int64()), "int64"),
        (pa.array([f"v{i}" for i in range(20)], pa.large_string()), "large_string"),
    ],
    ids=["int64", "large_string"],
)
@pytest.mark.parametrize("deterministic", [True, False])
def test_f5c_a_non_string_source_stays_python(
    source: pa.Array, type_name: str, deterministic: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = faker_seed() if deterministic else positional_seed()
    trio = run_trio(payload_shape({"f": (source, seed)}), monkeypatch)
    _declined(trio, "f", f"faker_source_type_not_string:f:{type_name}")


def test_f5d_no_companion_runs_the_python_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    hide_cat(monkeypatch)
    for seed in (faker_seed(), positional_seed()):
        shape = payload_shape({"f": (cells(50), seed)})
        result = run_seq(shape)
        assert_python_ran(result, "f", UNAVAILABLE)
        assert result.outputs["P"].equals(run_full(shape).outputs["P"])


class _IntPoolAdapter:
    """A poolable adapter bound over an allowlisted name whose values are integers."""

    backend_type = "test_int"
    backend_version = "1"

    def __init__(self, provider: str) -> None:
        self._provider = provider

    def generate(self, provider: str, *, spec: Any, source_value: bytes | None = None) -> Any:
        return 7

    def generate_batch(self, provider: str, *, spec: Any, count: int) -> list[int]:
        return list(range(100, 100 + count))

    def capability_matrix(self, provider: str) -> Any:
        return get_default_registry().get_capabilities(self._provider)


@pytest.mark.parametrize("deterministic", [True, False])
def test_f5e_an_incompatible_built_pool_declines_and_keeps_the_pool_cached(
    deterministic: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = get_default_registry()
    registry = default.override(FIRST, _IntPoolAdapter(FIRST), default.get_capabilities(FIRST))
    seed = faker_seed() if deterministic else positional_seed()
    shape = payload_shape({"f": (cells(40), seed)})
    trio = run_trio(shape, monkeypatch, registry)
    assert_trio_strict(trio)
    assert_python_ran(trio.native, "f", f"faker_provider_output_not_string:f:{FIRST}")
    # Admission was met, so the decline is the post-build guard and the output is integers.
    assert trio.native.outputs["P"].schema.field("f").type != pa.string()


def test_f5e_the_declined_pool_is_a_cache_hit_for_the_python_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.generation.pool import PoolBuilder

    default = get_default_registry()
    registry = default.override(FIRST, _IntPoolAdapter(FIRST), default.get_capabilities(FIRST))
    builds: list[int] = []
    real_build = PoolBuilder.build

    def counting(self: PoolBuilder, *args: Any, **kwargs: Any) -> ValuePool:
        builds.append(1)
        return real_build(self, *args, **kwargs)

    monkeypatch.setattr(PoolBuilder, "build", counting)
    from tests.parity._faker_seq_data import run_seq_with

    shape = payload_shape({"f": (cells(40), faker_seed())})
    run_seq_with(shape, registry)
    assert builds == [1]  # one build serves the declined seam call and the Python assembly


def test_f5f_a_deterministic_faker_without_a_pool_size_declines_with_oracle_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = payload_shape({"f": (cells(40), faker_seed(pool_size=None))})
    _declined(run_trio(shape, monkeypatch), "f", "faker_not_deterministic_reuse_variant:f")


def test_f5c_missing_namespace_surfaces_the_oracles_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not a fallback: the oracle itself rejects a deterministic faker with no namespace, so the
    # SEQ route must raise the same coded error on the native-capable and the hidden run.
    shape = payload_shape({"f": (cells(40), faker_seed(None))})
    from decoy_engine.generation.pool import GenerationError

    with pytest.raises(GenerationError) as oracle:
        run_full(shape)
    assert oracle.value.code == "deterministic_requires_source_and_namespace"
    with pytest.raises(GenerationError) as native:
        run_seq(shape)
    assert native.value.code == oracle.value.code
    hide_cat(monkeypatch)
    with pytest.raises(GenerationError) as python:
        run_seq(shape)
    assert python.value.code == oracle.value.code


# F7: a faker payload beside hash and categorical payloads in one job.


def _mixed(order: list[str]) -> Any:
    parts = {
        "h": (strs(["u0", "u1", None, "u3", "u4", "u5", "u6"]), column_seed(namespace="ns_h")),
        "c": (strs(SRC), cat_seed()),
        "f": (strs(SRC), faker_seed()),
        "g": (strs(SRC), positional_seed()),
    }
    return payload_shape({name: parts[name] for name in order})


@pytest.mark.parametrize(
    "order", [["h", "c", "f", "g"], ["g", "f", "c", "h"], ["c", "g", "h", "f"]]
)
def test_f7_faker_hash_and_categorical_each_report_under_their_own_key(
    order: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = _mixed(order)
    trio = run_trio(shape, monkeypatch)
    assert_trio_strict(trio)
    metrics = trio.native.quality_metrics
    assert set(metrics) == {
        SEQ_HASH_DISPOSITION_KEY,
        SEQ_CATEGORICAL_DISPOSITION_KEY,
        SEQ_FAKER_DISPOSITION_KEY,
    }
    assert sorted(e["column"] for e in faker_entries(trio.native)) == ["f", "g"]
    assert_native_witness(trio.native, "f")
    assert_native_witness(trio.native, "g")
    assert {e["column"] for e in metrics[SEQ_CATEGORICAL_DISPOSITION_KEY]} == {"c"}
    assert "f" not in {e["column"] for e in metrics[SEQ_HASH_DISPOSITION_KEY]}
    assert SEQ_FPE_DISPOSITION_KEY not in metrics


def test_f7_the_other_fragments_are_byte_identical_with_and_without_faker() -> None:
    with_faker = run_seq(_mixed(["h", "c", "f", "g"])).quality_metrics
    without = run_seq(_mixed(["h", "c"])).quality_metrics
    for key in (SEQ_HASH_DISPOSITION_KEY, SEQ_CATEGORICAL_DISPOSITION_KEY):
        assert repr(with_faker[key]) == repr(without[key]), key
    assert SEQ_FAKER_DISPOSITION_KEY not in without


def test_f7_a_job_without_faker_has_no_faker_key() -> None:
    shape = payload_shape({"p": (strs(SRC), PASSTHROUGH)})
    assert SEQ_FAKER_DISPOSITION_KEY not in run_seq(shape).quality_metrics


# F8: empty table versus all-null table, truthful evidence.


@pytest.mark.parametrize("deterministic", [True, False])
def test_f8_an_empty_table_runs_no_kernel_and_reports_zero_calls(
    deterministic: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = faker_seed() if deterministic else positional_seed()
    shape = payload_shape({"f": (pa.array([], pa.string()), seed)})
    trio = run_trio(shape, monkeypatch)
    assert_trio_strict(trio)
    entry = faker_entry(trio.native, "f")
    assert entry["native_calls"] == 0
    assert entry["executed_backend"] == "none" and entry["reason"] == "no_rows_hashed"


@pytest.mark.parametrize("deterministic", [True, False])
def test_f8_an_all_null_populated_column_runs_the_kernel_once(
    deterministic: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = faker_seed() if deterministic else positional_seed()
    shape = payload_shape({"f": (strs([None] * 9), seed)})
    trio = run_trio(shape, monkeypatch)
    assert_trio_strict(trio)
    assert_native_witness(trio.native, "f", calls=1)
    assert trio.native.outputs["P"].column("f").to_pylist() == [None] * 9
