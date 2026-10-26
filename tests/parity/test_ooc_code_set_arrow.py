"""Out-of-core code_set as a named-Arrow dict-map (B6).

The out-of-core code_set mask remaps every non-null cell to a corpus code. The remap is a pure
function of the value in the admitted shape (mask mode, no chapter_preserve), so B6 selects it
ONCE per distinct non-null value and expands the result with an Arrow take, instead of one
`apply_code_set` call per row. Pinned here: the computation-count witness (one selection per
distinct non-null input per batch, which fails the pre-B6 per-cell loop), byte-parity with the
per-cell reference across repeated / unknown / source-excluded / null / empty / unicode inputs on
a shipped and a customer corpus, that a pinned corpus record is honoured and a version mismatch
fails closed, and that gen mode and chapter_preserve stay declined.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core import _mask_group_c
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms.code_set import CodeSetConfig, apply_code_set

_JOB_SEED = b"\x11" * 8
_NS = "cs"


def _customer_corpus(tmp_path: pathlib.Path, codes: list[str], name: str = "custom") -> str:
    tbl = pa.table(
        {
            "code": pa.array(codes, pa.string()),
            "chapter": pa.array(["C"] * len(codes), pa.string()),
            "description": pa.array([f"Code {c}" for c in codes], pa.string()),
        }
    )
    path = tmp_path / f"{name}.parquet"
    pq.write_table(tbl, str(path))
    return f"customer:{path}"


def _cfg(corpus_source: str, **overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {"code_set": "custom", "corpus_source": corpus_source}
    cfg.update(overrides)
    return cfg


def _run(values: list[Any], cfg: dict[str, Any]) -> list[Any]:
    out = _mask_group_c._code_set_array(
        pa.array(values, pa.string()), job_seed=_JOB_SEED, namespace=_NS, cfg=cfg
    )
    assert out.type == pa.string()
    return out.to_pylist()


def _reference(values: list[Any], cfg: dict[str, Any]) -> list[Any]:
    """The pre-B6 per-cell result: apply_code_set on every non-null value, nulls in place."""
    code_cfg = CodeSetConfig.from_dict(cfg)
    return [
        None
        if v is None
        else apply_code_set(str(v), code_cfg, mode="mask", job_seed=_JOB_SEED, namespace=_NS)
        for v in values
    ]


class _CountingSelect:
    """Wraps apply_code_set and records every value it was called with."""

    def __init__(self) -> None:
        self.real = apply_code_set
        self.calls: list[str] = []

    def __call__(self, value: str, *args: Any, **kwargs: Any) -> str:
        self.calls.append(value)
        return self.real(value, *args, **kwargs)


# --- the B6 computation-count witness ---------------------------------------------------------


def test_each_distinct_non_null_value_is_selected_exactly_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = _CountingSelect()
    monkeypatch.setattr(_mask_group_c, "apply_code_set", counter)
    cfg = _cfg(_customer_corpus(tmp_path, [f"C{i:03d}" for i in range(12)]))
    # 9 rows, 3 distinct non-null inputs plus two nulls: the per-cell loop would call 7 times.
    values = ["x", "y", "x", None, "z", "x", "y", None, "z"]
    got = _run(values, cfg)
    assert sorted(counter.calls) == ["x", "y", "z"]  # once per distinct non-null, never per row
    assert got == _reference(values, cfg)  # and the expansion is byte-identical to the per-cell loop


# --- byte-parity across input shapes, shipped and customer corpora ----------------------------


@pytest.mark.parametrize(
    "values",
    [
        ["x", "y", "x", "z", "y", "x"],  # repeats
        ["unknown-a", "unknown-b", "unknown-a"],  # inputs absent from the corpus
        ["C000", "C001", "C000"],  # inputs present in the corpus (source-exclusion path)
        [None, None, None],  # all null
        ["", "", "a", ""],  # empty strings are a real distinct value, not a null
        ["é", "\U0001f600", "é", None, "日本"],  # unicode + null
        [],  # zero rows
        ["only"],  # singleton
    ],
    ids=["repeats", "unknown", "source-excluded", "all-null", "empties", "unicode", "zero", "one"],
)
def test_output_is_byte_identical_to_the_per_cell_reference(
    values: list[Any], tmp_path: pathlib.Path
) -> None:
    cfg = _cfg(_customer_corpus(tmp_path, [f"C{i:03d}" for i in range(16)]))
    assert _run(values, cfg) == _reference(values, cfg)


def test_the_shipped_mcc_corpus_matches_the_per_cell_reference() -> None:
    cfg = {"code_set": "mcc"}
    values = ["alpha", "beta", "alpha", None, "gamma", "beta", "delta"]
    assert _run(values, cfg) == _reference(values, cfg)


def test_a_high_cardinality_batch_still_matches_and_selects_each_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = _CountingSelect()
    monkeypatch.setattr(_mask_group_c, "apply_code_set", counter)
    cfg = _cfg(_customer_corpus(tmp_path, [f"C{i:03d}" for i in range(64)]))
    values = [f"in{i % 40}" for i in range(400)]  # 40 distinct across 400 rows
    got = _run(values, cfg)
    assert len(set(counter.calls)) == len(counter.calls) == 40  # each distinct once, no repeats
    assert got == _reference(values, cfg)


# --- the pinned corpus record and its version pin ---------------------------------------------


def test_a_pinned_corpus_record_is_honoured_across_the_batch(tmp_path: pathlib.Path) -> None:
    from decoy_engine.transforms.code_set import resolve_corpus_record

    cfg = _cfg(_customer_corpus(tmp_path, [f"C{i:03d}" for i in range(8)]))
    code_cfg = CodeSetConfig.from_dict(cfg)
    record = resolve_corpus_record(code_cfg)
    values = ["a", "b", "a", None, "b"]
    pinned = _mask_group_c._code_set_array(
        pa.array(values, pa.string()),
        job_seed=_JOB_SEED,
        namespace=_NS,
        cfg=cfg,
        corpus_record=record,
    ).to_pylist()
    assert pinned == _reference(values, cfg)


def test_a_corpus_version_mismatch_fails_closed(tmp_path: pathlib.Path) -> None:
    from decoy_engine.transforms.code_set import resolve_corpus_record

    cfg = _cfg(_customer_corpus(tmp_path, [f"C{i:03d}" for i in range(8)]))
    record = resolve_corpus_record(CodeSetConfig.from_dict(cfg))
    # A config that pins a different source version than the record carries must not mask silently.
    pinned_cfg = _cfg(
        _customer_corpus(tmp_path, [f"C{i:03d}" for i in range(8)], name="other"),
        corpus_source_version="does-not-match",
    )
    with pytest.raises(PlanCompileError):
        _mask_group_c._code_set_array(
            pa.array(["a", "a"], pa.string()),
            job_seed=_JOB_SEED,
            namespace=_NS,
            cfg=pinned_cfg,
            corpus_record=record,
        )


# --- the declined shapes stay declined --------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [{"mode": "gen"}, {"chapter_preserve": True}],
    ids=["gen", "chapter_preserve"],
)
def test_gen_and_chapter_preserve_stay_declined(
    overrides: dict[str, Any], tmp_path: pathlib.Path
) -> None:
    cfg = _cfg(_customer_corpus(tmp_path, [f"C{i:03d}" for i in range(8)]), **overrides)
    with pytest.raises(ExecutionError, match="out_of_core_code_set_shape_unsupported"):
        _run(["a", "b"], cfg)
