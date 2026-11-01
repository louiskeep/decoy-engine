"""F6 (2026-10-25): the out-of-core text_redact NER path windows its inference
and shares the config/version plumbing with the full-frame handler.

The byte-parity of the out-of-core NER path against the oracle is proven, with
the real model, by
`tests/parity/test_out_of_core_group_b_parity.test_text_redact_ner_path_parity`.
This module pins the two properties that do not need the model:

1. the apply-window bound (the kernel feeds `iter_ner_spans_batch` in
   `_NER_APPLY_WINDOW`-row windows, so Doc/span-list memory stays window-bounded
   rather than scaling with `batch_rows`), and that windowing is output-invariant;
2. the `ner_model_version_mismatch` guard and the `ner` config resolution run
   through the shared `_ner_config` module, so they cannot drift from the
   full-frame handler.

The spy returns no (or synthetic) spans, so none of this needs spaCy.
"""

from __future__ import annotations

import math

import pyarrow as pa
import pytest

from decoy_engine.detection.detectors import Span
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._ner_config import (
    enforce_ner_model_version,
    resolve_ner_config,
)
from decoy_engine.execution.out_of_core._mask_group_b import text_redact_array
from decoy_engine.plan._types import ColumnSeed


def _seed(provider_config: dict, *, ner_model_version: str | None = None) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="text_redact",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="bijective",
        deterministic=False,
        provider_config=tuple(sorted(provider_config.items())),
        ner_model_version=ner_model_version,
    )


class TestKernelWindowsTheNerBatch:
    """The out-of-core kernel must feed NER through `iter_ner_spans_batch` in
    `_NER_APPLY_WINDOW`-row windows, not the whole batch at once: `batch_rows`
    can be far above the window, so the pre-window single call let Doc +
    span-list memory scale with the batch. Spy on the call sizes; dropping the
    window loop (one call over every non-null cell) makes this fail. Needs no
    spaCy (the spy returns no spans)."""

    def test_kernel_windows_the_ner_batch(self, monkeypatch) -> None:
        from decoy_engine.detection.ner import _NER_APPLY_WINDOW

        window_sizes: list[int] = []

        def _spy(texts, *, model=None, entities=None):
            window_sizes.append(len(texts))
            return [[] for _ in texts]

        monkeypatch.setattr("decoy_engine.detection.ner.iter_ner_spans_batch", _spy)
        n_rows = _NER_APPLY_WINDOW * 2 + 5  # two full windows plus a partial
        values = pa.array([f"row {i} plain text" for i in range(n_rows)], type=pa.string())
        text_redact_array(values, plan=_seed({"ner": True}), column="notes")

        assert len(window_sizes) == math.ceil(n_rows / _NER_APPLY_WINDOW), (
            f"kernel must window the batch: expected "
            f"{math.ceil(n_rows / _NER_APPLY_WINDOW)} calls, got {len(window_sizes)}"
        )
        assert all(size <= _NER_APPLY_WINDOW for size in window_sizes), window_sizes
        assert sum(window_sizes) == n_rows  # every non-null cell fed exactly once

    def test_null_cells_are_not_fed_to_ner(self, monkeypatch) -> None:
        fed: list[int] = []

        def _spy(texts, *, model=None, entities=None):
            fed.append(len(texts))
            return [[] for _ in texts]

        monkeypatch.setattr("decoy_engine.detection.ner.iter_ner_spans_batch", _spy)
        values = pa.array(["a", None, "b", None, "c"], type=pa.string())
        out = text_redact_array(values, plan=_seed({"ner": True}), column="notes")
        assert sum(fed) == 3  # only the 3 non-null cells
        assert out.to_pylist() == ["a", None, "b", None, "c"]  # no spans -> unchanged

    def test_no_ner_config_never_calls_the_batch_helper(self, monkeypatch) -> None:
        calls: list[object] = []
        monkeypatch.setattr(
            "decoy_engine.detection.ner.iter_ner_spans_batch",
            lambda texts, *, model=None, entities=None: calls.append(texts) or [[] for _ in texts],
        )
        values = pa.array(["John Smith in Boston", "plain"], type=pa.string())
        text_redact_array(values, plan=_seed({}), column="notes")
        assert calls == []  # NER off: the batch helper is never reached

    def test_windowing_is_output_invariant(self, monkeypatch) -> None:
        """Windowing must not change output: `iter_ner_spans_batch` returns one
        span list per input regardless of how the texts are chunked, so a tiny
        window and a huge window produce byte-identical arrays. A deterministic
        span spy (redacts the literal 'SECRET') makes this checkable off spaCy."""

        def _spy(texts, *, model=None, entities=None):
            spans_per = []
            for t in texts:
                i = t.find("SECRET")
                spans_per.append([Span("person_name", i, i + 6, "SECRET")] if i >= 0 else [])
            return spans_per

        monkeypatch.setattr("decoy_engine.detection.ner.iter_ner_spans_batch", _spy)
        values = pa.array(
            [f"cell {i} has a SECRET here" if i % 3 else f"clean {i}" for i in range(20)] + [None],
            type=pa.string(),
        )
        seed = _seed({"ner": True})

        monkeypatch.setattr("decoy_engine.detection.ner._NER_APPLY_WINDOW", 1000)
        wide = text_redact_array(values, plan=seed, column="notes")
        monkeypatch.setattr("decoy_engine.detection.ner._NER_APPLY_WINDOW", 1)
        narrow = text_redact_array(values, plan=seed, column="notes")

        assert wide.to_pylist() == narrow.to_pylist()
        assert "SECRET" not in "".join(v for v in wide.to_pylist() if v)


class TestSharedVersionGuard:
    """The `ner_model_version_mismatch` guard runs through the shared
    `_ner_config` module, so the out-of-core route raises the same code the
    full-frame handler does, now with the masked column named in the message
    (the column is threaded from `mask_column`)."""

    def test_version_mismatch_raises_with_column(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "decoy_engine.detection.ner.installed_model_version", lambda model=None: "2.0.0"
        )
        values = pa.array(["John Smith"], type=pa.string())
        seed = _seed({"ner": {"model": "en_core_web_sm"}}, ner_model_version="1.0.0")
        with pytest.raises(StrategyError) as exc:
            text_redact_array(values, plan=seed, column="notes")
        assert exc.value.code == "ner_model_version_mismatch"
        assert "notes" in exc.value.message

    def test_version_match_does_not_fire(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "decoy_engine.detection.ner.installed_model_version", lambda model=None: "1.0.0"
        )
        monkeypatch.setattr(
            "decoy_engine.detection.ner.iter_ner_spans_batch",
            lambda texts, *, model=None, entities=None: [[] for _ in texts],
        )
        values = pa.array(["plain text"], type=pa.string())
        seed = _seed({"ner": {"model": "en_core_web_sm"}}, ner_model_version="1.0.0")
        out = text_redact_array(values, plan=seed, column="notes")
        assert out.to_pylist() == ["plain text"]


class TestNerConfigResolution:
    """`resolve_ner_config` is the one resolver both routes call."""

    def test_no_ner_key_is_off(self) -> None:
        assert resolve_ner_config({}) == (None, None)

    def test_falsy_ner_is_off(self) -> None:
        assert resolve_ner_config({"ner": False}) == (None, None)

    def test_ner_true_is_default_model_all_entities(self) -> None:
        from decoy_engine.detection.ner import DEFAULT_NER_MODEL

        assert resolve_ner_config({"ner": True}) == (DEFAULT_NER_MODEL, None)

    def test_ner_dict_model_and_entities(self) -> None:
        assert resolve_ner_config({"ner": {"model": "m", "entities": ["person_name"]}}) == (
            "m",
            ["person_name"],
        )

    def test_ner_dict_empty_entities_is_all(self) -> None:
        assert resolve_ner_config({"ner": {"model": "m", "entities": []}}) == ("m", None)

    def test_empty_ner_dict_is_off(self) -> None:
        # An empty `ner: {}` dict is falsy, so NER stays off -- preserving the
        # handlers' long-standing `if not ner_cfg` guard (an empty mapping is
        # not a request to run the default model).
        assert resolve_ner_config({"ner": {}}) == (None, None)

    def test_ner_dict_truthy_no_model_is_default(self) -> None:
        from decoy_engine.detection.ner import DEFAULT_NER_MODEL

        # A non-empty dict with no `model` key resolves to the default model.
        assert resolve_ner_config({"ner": {"entities": ["location"]}}) == (
            DEFAULT_NER_MODEL,
            ["location"],
        )

    def test_enforce_is_noop_when_off_or_unstamped(self) -> None:
        # No model, or no stamped version: nothing to compare, never raises.
        enforce_ner_model_version(
            ner_model=None, plan_ner_model_version="1.0.0", column="c", strategy="text_redact"
        )
        enforce_ner_model_version(
            ner_model="m", plan_ner_model_version=None, column="c", strategy="text_redact"
        )

    def test_enforce_strategy_label_is_passed_through(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "decoy_engine.detection.ner.installed_model_version", lambda model=None: "9.9.9"
        )
        with pytest.raises(StrategyError) as exc:
            enforce_ner_model_version(
                ner_model="m",
                plan_ner_model_version="1.0.0",
                column=None,
                strategy="text_mask",
            )
        assert exc.value.code == "ner_model_version_mismatch"
        assert exc.value.strategy == "text_mask"
