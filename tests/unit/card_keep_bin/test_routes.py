"""keep_bin on every route: the Python routes agree byte for byte, every native admission
declines it, the native config builders carry it, and a config the strategy cannot honour
fails closed before any value is encrypted."""

from __future__ import annotations

import random
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import checksums, run_pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import ExecutionError
from decoy_engine.execution._chunked import run_mask_pipeline_chunked
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._fpe import FpeStrategyHandler
from decoy_engine.execution.native._crypto_ext import FpeConfig, FpeConfigError
from decoy_engine.execution.native._crypto_reference import reference_fpe
from decoy_engine.execution.native._fpe_ext import _CompiledFpeKernel
from decoy_engine.execution.native._fpe_route import fpe_config_from_params
from decoy_engine.execution.native._operator_params import FpeParams, resolve_operator_params
from decoy_engine.execution.native._real_type_admission import _fpe_checksum_decline
from decoy_engine.execution.out_of_core._fpe_native import decline_reason
from decoy_engine.execution.out_of_core._mask_group_b import fpe_array
from decoy_engine.unmask import unmask_pipeline
from tests.parity._d2b_shapes import run_full, run_seq, strs
from tests.parity._d2c_fpe_data import FpeMarkingKernel, fpe_seed, install_fpe, payload_shape

from ._pans import digits_of, pan

_TABLE = "payments"
_SEED = 4242
_PC = {"charset": "digits", "checksum": "luhn", "keep_bin": 6}


def _values() -> list[str | None]:
    rng = random.Random(3)
    cards: list[str | None] = [pan(rng, n) for n in (13, 15, 16, 16, 19, 16)]
    cards += ["4539 5787 6362 1486", None, "", "4539 5787 6362 1486"]
    return cards


def _config(tmp_path: Any, pc: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = {
        "version": 2,
        "global_settings": {"seed": _SEED},
        "sources": {_TABLE: {"type": "file", "format": "csv", "path": str(tmp_path / "in.csv")}},
        "tables": [
            {
                "name": _TABLE,
                "columns": [
                    {"name": "card", "strategy": "fpe", "namespace": "card_ns",
                     "provider_config": dict(pc or _PC)},
                ],
            }
        ],
        "targets": {_TABLE: {"type": "file", "format": "csv", "path": str(tmp_path / "out.csv")}},
    }  # fmt: skip
    return PipelineConfig.model_validate(raw).model_dump()


def _full_frame(cfg: dict[str, Any], tmp_path: Any, values: list[Any]) -> pa.Table:
    df = pd.DataFrame({"card": values})
    df.to_csv(tmp_path / "in.csv", index=False)
    table = pa.Table.from_pandas(df, preserve_index=False)
    result = run_pipeline(cfg, sources={_TABLE: table}, engine_version="keep-bin-test")
    return result.outputs[_TABLE]


def test_full_frame_chunked_and_unmask_agree(tmp_path: Any) -> None:
    values = _values()
    cfg = _config(tmp_path)
    full = _full_frame(cfg, tmp_path, values).column("card").to_pylist()
    for source, out in zip(values, full, strict=True):
        if not source:
            assert out == source
            continue
        assert len(out) == len(source)
        assert digits_of(out)[:6] == digits_of(source)[:6]
        assert checksums.validate("luhn", digits_of(out))
    assert full[6] == full[9]  # deterministic within the column

    table = pa.table({"card": pa.array(values, pa.string())})
    chunks = [table.slice(i, 3) for i in range(0, len(values), 3)]
    chunked = pa.concat_tables(
        run_mask_pipeline_chunked(cfg, chunks, table=_TABLE, engine_version="keep-bin-test")
    )
    assert chunked.column("card").to_pylist() == full

    masked = pa.table({"card": pa.array(full, pa.string())})
    recovered = unmask_pipeline(cfg, {_TABLE: masked})
    assert recovered.outputs[_TABLE].column("card").to_pylist() == values


class _Ctx:
    mask_key = bytes(range(32))
    seq_native = None


def _handler(values: list[Any], pc: dict[str, Any]) -> list[Any]:
    df = pd.DataFrame({"card": values})
    out, _ = FpeStrategyHandler().run(df, "card", fpe_seed("card_ns", **pc), _Ctx())  # type: ignore[arg-type]
    return out["card"].tolist()


def test_out_of_core_and_reference_oracle_match_the_handler() -> None:
    values = _values()
    full = _handler(values, _PC)
    ooc = fpe_array(
        pa.array(values, pa.string()),
        job_seed=_Ctx.mask_key,
        namespace="card_ns",
        column="card",
        cfg=dict(_PC),
    )
    assert ooc.to_pylist() == full

    config = FpeConfig.from_mapping(_PC)
    assert config.keep_bin == 6
    ref = reference_fpe().encrypt_batch(  # type: ignore[attr-defined]
        pa.array(values, pa.string()),
        mask_key=_Ctx.mask_key,
        namespace="card_ns",
        tweak_column="card",
        config=config,
    )
    assert ref.values.to_pylist() == full and ref.errors == ()
    back = reference_fpe().decrypt_batch(  # type: ignore[attr-defined]
        ref.values, mask_key=_Ctx.mask_key, namespace="card_ns", tweak_column="card", config=config
    )
    assert back.values.to_pylist() == values


def test_sequential_route_declines_and_matches_full_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_fpe(monkeypatch, FpeMarkingKernel())
    cards = [pan(random.Random(i), 16) for i in range(7)]
    cols = {"card": (strs(cards), fpe_seed(**_PC))}
    seq = run_seq(payload_shape(cols))
    entry = seq.quality_metrics["sequential_fpe"][0]
    assert entry["reason"] == "fpe_checksum_not_native:card"
    assert entry["executed_backend"] == "python_scalar"
    full = run_full(payload_shape(cols)).outputs["P"].column("card").to_pylist()
    assert seq.outputs["P"].column("card").to_pylist() == full
    assert all(o[:6] == c[:6] and o != c for o, c in zip(full, cards, strict=True))
    assert kernel.calls == []


def test_every_native_admission_declines_keep_bin() -> None:
    config = {"tables": [{"name": "t", "columns": [
        {"name": "card", "strategy": "fpe", "namespace": "ns", "provider_config": dict(_PC)}]}]}  # fmt: skip
    assert _fpe_checksum_decline(config, "t", "card") == "fpe_checksum_not_native:card"
    seed = fpe_seed("ns", **_PC)
    schema = pa.schema([("card", pa.string())])
    assert decline_reason("card", schema, seed) == (None, "fpe_checksum_not_native:card")
    from decoy_engine.execution._sequential_native_ops import fpe_op

    assert (
        fpe_op(lambda _t: None).admit("card", pa.string(), seed) == "fpe_checksum_not_native:card"
    )


def test_both_native_config_builders_carry_keep_bin() -> None:
    params = resolve_operator_params("fpe", target="card", provider_config=_PC, namespace="ns")
    assert isinstance(params, FpeParams) and params.keep_bin == 6
    assert fpe_config_from_params(params) == FpeConfig.from_mapping(_PC)
    config_fields = set(FpeConfig.__dataclass_fields__)
    params_fields = set(FpeParams.__dataclass_fields__) - {"namespace", "tweak_column"}
    assert config_fields == params_fields


@pytest.mark.parametrize("keep_bin", [6, 7, False])
def test_the_kernel_refuses_keep_bin_before_calling_the_compiled_function(keep_bin: Any) -> None:
    calls: list[Any] = []
    kernel = _CompiledFpeKernel(lambda *a, **k: calls.append((a, k)))
    with pytest.raises(FpeConfigError):
        kernel.run(
            pa.array(["4539578763621486"], pa.string()),
            mask_key=bytes(32),
            namespace="ns",
            tweak_column="card",
            config=FpeConfig(keep_bin=keep_bin, checksum=None),
            forward=True,
        )
    assert calls == []


def test_reference_oracle_refuses_an_unvalidated_keep_bin() -> None:
    with pytest.raises(FpeConfigError):
        reference_fpe().encrypt_batch(  # type: ignore[attr-defined]
            pa.array(["4539578763621486"], pa.string()),
            mask_key=bytes(32),
            namespace="ns",
            tweak_column="card",
            config=FpeConfig(keep_bin=7, checksum="luhn"),
        )


@pytest.mark.parametrize(
    "pc",
    [{"keep_bin": 6}, {"keep_bin": 7, "checksum": "luhn"}, {"keep_bin": 6, "checksum": "npi"}],
    ids=["no_checksum", "seven", "npi"],
)
def test_the_handler_fails_closed_before_encrypting(pc: dict[str, Any]) -> None:
    seed = fpe_seed("ns", **pc)
    df = pd.DataFrame({"card": ["4539578763621486"]})
    with pytest.raises(StrategyError) as info:
        FpeStrategyHandler().run(df, "card", seed, _Ctx())  # type: ignore[arg-type]
    # The only handler-surface code a bad keep_bin produces.
    assert info.value.code == "fpe_checksum_unsupported"
    assert "4539" not in str(info.value)
    assert df["card"].tolist() == ["4539578763621486"]


def test_out_of_core_fails_closed_on_a_bad_keep_bin() -> None:
    with pytest.raises(StrategyError) as info:
        fpe_array(
            pa.array(["4539578763621486"], pa.string()),
            job_seed=bytes(32),
            namespace="ns",
            column="card",
            cfg={"keep_bin": 6},
        )
    assert info.value.code == "fpe_checksum_unsupported"


def test_a_pipeline_fails_closed_on_a_short_card(tmp_path: Any) -> None:
    cfg = _config(tmp_path, {**_PC, "keep_bin": 8})
    with pytest.raises(ExecutionError) as info:
        _full_frame(cfg, tmp_path, ["4539578763621486", "4222222222222"])
    assert info.value.code == "fpe_unencryptable_domain"
    assert "4222" not in info.value.message
