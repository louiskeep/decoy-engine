"""text_redact SEQ-native: admission, handler-level config errors, routing and the FK-key
boundary. These cells need no compiled companion (the declines hold with or without it).
Byte-parity against the oracle on the real span kernel is in
`test_phase_d_text_redact_seq_real.py`.

Acceptance T4, T4b, T6 (routing) and T_key (decline half) of
docs/plans/2026-10-14-c-text-redact-seq-native.md.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import _sequential_native
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution._pipeline_routing import decide_execution_route
from decoy_engine.execution._pipeline_routing_signals import out_of_core_admission
from decoy_engine.execution._sequential_native_decisions import ColumnDecision
from decoy_engine.execution._sequential_native_ops import (
    SEQ_FAKER_DISPOSITION_KEY,
    SEQ_TEXT_REDACT_DISPOSITION_KEY,
    TEXT_REDACT,
    text_redact_op,
)
from decoy_engine.execution.native import _text_redact_kernel
from decoy_engine.relationships._graph import OrphanPolicy
from decoy_engine.storm import detectors as detectors_mod
from decoy_engine.storm import ner as ner_mod
from tests.parity import _d2b_shapes as s
from tests.parity._d2b_shapes import make_plan, run_full, run_seq, table_seed
from tests.parity._d2c_cat_data import payload_shape
from tests.parity._faker_seq_data import faker_seed
from tests.parity._fpe_key_data import assert_keys_declined
from tests.parity._text_redact_seq_data import (
    UNAVAILABLE,
    assert_python_ran,
    hide_text_redact,
    kernel_loads,
    strs,
    tr_entries,
    tr_entry,
    tr_seed,
)


def _declined_as(reason: str) -> str:
    """The reason a declined column reports. Without the span kernel the unavailable check comes
    first (the admit predicate is never consulted), so the config reason shows only with it."""
    return reason if kernel_loads() else UNAVAILABLE


PII = ["mail a@b.com now", "ssn 123-45-6789 ok", "call 212-555-1234", "clean", None]
_SEQUENTIAL = ("sequential", "pure_mask_fk")
_SEQ_BACKEND = "rust_companion" if kernel_loads() else "python_scalar"


def _shape(values: list[str | None] | pa.Array = PII, **config: Any) -> s.Shape:
    arr = values if isinstance(values, pa.Array) else strs(values)
    return payload_shape({"t": (arr, tr_seed(**config))})


def _equals_oracle(shape: s.Shape) -> Any:
    result = run_seq(shape)
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])
    return result


# The descriptor: registered, payload-only, config-aware, value-free.


def test_text_redact_is_registered_and_is_not_a_key_strategy() -> None:
    assert TEXT_REDACT in _sequential_native._OPS
    op = _sequential_native._OPS[TEXT_REDACT]
    assert op.disposition_key == SEQ_TEXT_REDACT_DISPOSITION_KEY == "sequential_text_redact"
    assert TEXT_REDACT not in _sequential_native._KEY_STRATEGIES
    assert op.remap_input_reason is None


def test_the_admit_reasons_are_coded_and_value_free() -> None:
    admit = text_redact_op(lambda _t: None).admit
    string = pa.string()
    assert admit("c", string, tr_seed()) is None
    assert admit("c", string, tr_seed(detectors=["email"], token="x", label_token=True)) is None
    assert admit("c", string, tr_seed(detectors=None)) is None
    assert admit("c", string, tr_seed(ner=True)) == "text_redact_ner_not_native:c"
    assert admit("c", string, tr_seed(ner={"model": "m"})) == "text_redact_ner_not_native:c"
    assert admit("c", string, tr_seed(token=5)) == "text_redact_token_not_string:c"
    assert admit("c", string, tr_seed(detectors="email")) == "text_redact_detectors_malformed:c"
    assert admit("c", pa.int64(), tr_seed()) == "text_redact_source_type_not_string:c:int64"
    assert admit("c", pa.large_string(), tr_seed()) == (
        "text_redact_source_type_not_string:c:large_string"
    )


def test_the_loader_adapter_takes_the_seam_thread_budget_and_keeps_none() -> None:
    from decoy_engine.execution._sequential_native_ops import _load_text_redact

    loaded = _load_text_redact(4)  # the seam passes a thread budget; the loader takes none
    assert (loaded is not None) == kernel_loads()


# T4: declines that reach the seam and fall to the Python handler, byte-identical.


def _install_ner_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """spaCy is not needed to tell the paths apart: the stub marks the first three characters of
    every cell as a person_name span, for the oracle and the Python fallback alike."""

    def fake(texts: list[str], *, model: str, entities: Any) -> list[list[Any]]:
        return [[detectors_mod.Span("person_name", 0, min(3, len(t)), t[:3])] for t in texts]

    monkeypatch.setattr(ner_mod, "iter_ner_spans_batch", fake)


@pytest.mark.parametrize("ner", [True, {"model": "en_core_web_sm"}], ids=["bool", "mapping"])
def test_t4_ner_declines_through_the_seam_and_runs_python(
    ner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ner_stub(monkeypatch)
    result = _equals_oracle(_shape(ner=ner))
    assert_python_ran(result, "t", _declined_as("text_redact_ner_not_native:t"))
    assert result.outputs["P"].column("t").to_pylist()[3] == "[REDACTED]an"  # NER really ran


@pytest.mark.parametrize(
    ("typ", "reason"),
    [
        (pa.large_string(), "text_redact_source_type_not_string:t:large_string"),
        (pa.int64(), "text_redact_source_type_not_string:t:int64"),
    ],
    ids=["large_string", "int64"],
)
def test_t4_a_non_string_source_declines_through_the_seam(typ: pa.DataType, reason: str) -> None:
    values = [f"v{i} a@b.com" for i in range(6)] if typ == pa.large_string() else list(range(6))
    result = _equals_oracle(_shape(pa.array(values, typ)))
    assert_python_ran(result, "t", _declined_as(reason))


def test_t4c_no_companion_declines_with_the_unavailable_reason_and_equals_the_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hide_text_redact(monkeypatch)
    result = _equals_oracle(_shape())
    assert_python_ran(result, "t", UNAVAILABLE)


def test_t4c_a_missing_companion_package_is_the_same_decline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The loader's own absent-package path (`sys.modules[...] = None`), not a patched loader.
    monkeypatch.setitem(__import__("sys").modules, "decoy_engine_native", None)
    assert _text_redact_kernel_missing()
    result = _equals_oracle(_shape())
    assert_python_ran(result, "t", UNAVAILABLE)


def _text_redact_kernel_missing() -> bool:
    return _text_redact_kernel.load_text_redact_kernel() is None


# T4b (handler level): config errors early-return in the handler and NEVER reach the seam.


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        ({"token": 5}, "text_redact_token_not_string:t"),
        ({"detectors": "email"}, "text_redact_detectors_malformed:t"),
        ({"detectors": 7}, "text_redact_detectors_malformed:t"),
    ],
    ids=["non-string-token", "string-detectors", "int-detectors"],
)
def test_t4b_handler_config_errors_are_unchanged_and_never_reach_the_seam(
    config: dict[str, Any], reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def seam_reached(*_a: Any, **_k: Any) -> None:
        raise AssertionError("a handler early-return must not consult the SEQ seam")

    monkeypatch.setattr(_sequential_native.SeqNativeRun, "text_redact_column", seam_reached)
    shape = _shape(**config)
    result = _equals_oracle(shape)
    # The handler leaves the column untouched, exactly as the oracle does.
    assert result.outputs["P"].column("t").to_pylist() == PII
    # Not a SEQ decline: the seam booked no rows and made no call.
    entry = tr_entry(result, "t")
    assert entry["executed_backend"] == "none"
    assert entry["native_calls"] == 0
    assert entry["reason"] == _declined_as(reason)


# T_key (decline half): text_redact configured on an FK key stays Python.


def _key_shape(policy: OrphanPolicy) -> s.Shape:
    plan = make_plan(
        {
            "A": table_seed({"k": tr_seed()}),
            "B": table_seed({"k": tr_seed()}),
            "C": table_seed({"k": tr_seed()}),
        }
    )
    return s.Shape(
        plan,
        s.graph(
            s.edge("A", "k", "B", "k", "ns_ab", policy),
            s.edge("B", "k", "C", "k", "ns_bc", policy),
        ),
        {
            "A": pa.table({"k": strs(["alpha a@b.com", "beta"])}),
            "B": pa.table({"k": strs(["alpha a@b.com", "beta", "gamma"])}),
            "C": pa.table({"k": strs(["alpha a@b.com", "beta", "gamma", "delta"])}),
        },
    )


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.REMAP])
def test_t_key_a_text_redact_key_stays_python_and_reports_the_decline(
    policy: OrphanPolicy,
) -> None:
    shape = _key_shape(policy)
    result = run_seq(shape)
    oracle = run_full(shape)
    for table in ("A", "B", "C"):
        assert result.outputs[table].equals(oracle.outputs[table]), table
    assert_keys_declined(result, shape, "text_redact", SEQ_TEXT_REDACT_DISPOSITION_KEY)


# The evidence entry renders its numeric call count.


def test_the_text_redact_entry_renders_its_numeric_call_count() -> None:
    entry = ColumnDecision("t", "c", "rust_companion", None, pa.string(), 5, TEXT_REDACT, 1).entry()
    assert entry["native_calls"] == 1
    declined = ColumnDecision("t", "c", "python_scalar", "r", pa.string(), 5, TEXT_REDACT).entry()
    assert declined["native_calls"] == 0
    other = ColumnDecision("t", "c", "rust_companion", None, pa.string(), 5, "categorical").entry()
    assert "native_calls" not in other


# T6 routing: where a text_redact-bearing FK job runs, through production routing.


def _route(shape: s.Shape, **kw: Any) -> tuple[str, str]:
    compatible, code = out_of_core_admission(shape.plan, registry=s.REGISTRY, graph=shape.graph)
    return decide_execution_route(
        SimpleNamespace(relationships=(object(),), tables=()),
        has_generate_table=False,
        has_mask_table=True,
        validators=[],
        fidelity_report=False,
        vault_writer=None,
        execution_mode=kw.pop("execution_mode", "auto"),
        graph=shape.graph,
        out_of_core_compatible=compatible,
        out_of_core_reject_code=code,
        largest_table_rows=kw.pop("largest_table_rows", 10),
        **kw,
    )


# text_redact IS out-of-core compatible (group b), so unlike faker a text_redact-only FK job
# that does not fit, is unpriceable, or reaches the row threshold goes OUT-OF-CORE, not to SEQ.
# SEQ is where it lands below the threshold, on an explicit override, or beside a strategy the
# out-of-core route cannot take (faker here), which is the case this slice accelerates.
_OOC_BOUNDED = ("out_of_core", "byte_estimate_bounded_out_of_core")
_OOC_LARGE_FK = ("out_of_core", "out_of_core_large_fk")


def _mixed_shape() -> s.Shape:
    return payload_shape(
        {"t": (strs(PII), tr_seed()), "f": (strs(PII), faker_seed())},
    )


@pytest.mark.parametrize(
    ("estimate", "expected"),
    [
        (True, ("full_frame", "byte_estimate_full_frame_fits")),
        (False, _OOC_BOUNDED),
        (None, _OOC_BOUNDED),
    ],
    ids=["fits", "does-not-fit", "unpriceable"],
)
def test_t6_byte_estimate_routing_for_a_text_redact_only_job(
    estimate: bool | None, expected: Any
) -> None:
    got = _route(
        _shape(),
        use_byte_estimate_routing=True,
        use_probe_routing=False,
        full_frame_fits_estimate=estimate,
    )
    assert got == expected


@pytest.mark.parametrize(
    ("estimate", "expected"),
    [
        (True, ("full_frame", "byte_estimate_full_frame_fits")),
        (False, _SEQUENTIAL),
        (None, _SEQUENTIAL),
    ],
    ids=["fits", "does-not-fit", "unpriceable"],
)
def test_t6_byte_estimate_routing_for_text_redact_beside_faker_reaches_sequential(
    estimate: bool | None, expected: Any
) -> None:
    got = _route(
        _mixed_shape(),
        use_byte_estimate_routing=True,
        use_probe_routing=False,
        full_frame_fits_estimate=estimate,
    )
    assert got == expected


@pytest.mark.parametrize(
    ("rows", "expected"),
    [(4_999_999, _SEQUENTIAL), (5_000_000, _OOC_LARGE_FK), (50_000_000, _OOC_LARGE_FK)],
    ids=["below", "at", "above"],
)
def test_t6_flag_off_row_threshold_routing(rows: int, expected: Any) -> None:
    kw: dict[str, Any] = {
        "use_byte_estimate_routing": False,
        "full_frame_fits_estimate": True,
        "largest_table_rows": rows,
        "out_of_core_threshold_rows": 5_000_000,
    }
    assert _route(_shape(), **kw) == expected
    # Beside faker the out-of-core route is unavailable, so both sides of the threshold are SEQ.
    assert _route(_mixed_shape(), **kw) == _SEQUENTIAL


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("sequential", _SEQUENTIAL),
        ("full_frame", ("full_frame", "override_full_frame")),
        ("out_of_core", ("out_of_core", "override_out_of_core")),
    ],
)
def test_t6_explicit_execution_mode_is_honoured(mode: str, expected: Any) -> None:
    assert _route(_shape(), execution_mode=mode, full_frame_fits_estimate=True) == expected


# End to end through run_pipeline on a real text_redact config.


def _config(tmp_path: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    parent = pa.table({"t": PII, "k": ["k1", "k2", "k3", "k4", "k5"]})
    child = pa.table({"k": ["k1", "k2", "k9"]})
    for name, table in (("P", parent), ("C", child)):
        pq.write_table(table, tmp_path / f"{name}.parquet")
    config: dict[str, Any] = {
        "version": 1,
        "global_settings": {"job_name": "text-redact-route", "seed": 7},
        "sources": {
            t: {"type": "file", "path": str(tmp_path / f"{t}.parquet"), "format": "parquet"}
            for t in ("P", "C")
        },
        "targets": {
            t: {"type": "file", "path": str(tmp_path / f"{t}.out.parquet"), "format": "parquet"}
            for t in ("P", "C")
        },
        "tables": [
            {
                "name": "P",
                "columns": [
                    {"name": "t", "strategy": "text_redact"},
                    {"name": "k", "strategy": "hash", "namespace": "ns_k"},
                ],
            },
            {"name": "C", "columns": [{"name": "k", "strategy": "hash", "namespace": "ns_k"}]},
        ],
        "relationships": [
            {
                "parent": {"table": "P", "columns": ["k"]},
                "children": [{"table": "C", "columns": ["k"]}],
                "orphan_policy": "preserve",
                "namespace": "ns_k",
            }
        ],
    }
    return config, {"P": parent, "C": child}


def _run(tmp_path: Path, **kw: Any) -> Any:
    config, sources = _config(tmp_path)
    return run_pipeline(
        config, sources, engine_version="0.1.0", out_of_core_budget_bytes=1 << 30, **kw
    )


def _visible(result: Any) -> tuple[str, str]:
    execution = result.quality_metrics["execution"]
    return execution["execution_mode"], execution["route_reason"]


def test_default_routing_runs_a_small_text_redact_job_full_frame(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert _visible(result) == ("full_frame", "byte_estimate_full_frame_fits")
    assert SEQ_TEXT_REDACT_DISPOSITION_KEY not in result.quality_metrics


@pytest.mark.parametrize(
    "kw",
    [{"use_byte_estimate_routing": False}, {"execution_mode": "sequential"}],
    ids=["flag-off", "explicit-sequential"],
)
def test_t6_sequential_route_reports_its_text_redact_backend(
    tmp_path: Path, kw: dict[str, Any]
) -> None:
    result = _run(tmp_path, **kw)
    assert _visible(result) == _SEQUENTIAL
    [entry] = tr_entries(result)
    assert (entry["column"], entry["path"], entry["executed_backend"]) == (
        "t",
        "scalar",
        _SEQ_BACKEND,
    )
    assert entry["native_calls"] == (1 if _SEQ_BACKEND == "rust_companion" else 0)
    assert SEQ_FAKER_DISPOSITION_KEY not in result.quality_metrics


def test_t6_full_frame_and_sequential_routes_return_strictly_equal_tables(
    tmp_path: Path,
) -> None:
    from tests.parity import _strict_compare as sc

    full = _run(tmp_path, execution_mode="full_frame")
    sequential = _run(tmp_path, execution_mode="sequential")
    for table in ("P", "C"):
        assert sc.compare_strict(full.outputs[table], sequential.outputs[table]) == dict.fromkeys(
            sc.DIMENSIONS, True
        )
    assert sequential.outputs["P"].column("t").to_pylist()[0] == "mail [REDACTED] now"
