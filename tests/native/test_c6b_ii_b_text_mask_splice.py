"""C6b-ii slice b (T-b1): the redact/passthrough splice runs in the compiled companion.

The oracle is the shipped per-cell `mask_cell` loop, so every comparison is native-vs-oracle and
no expected output is hand-written. Beyond equality, each case asserts WHICH path ran: the Rust
splice for an eligible config, slice a's Python splice for an ineligible one. The lookaround
counterexample (`ssn` is detected in Python, so a splice over raw Rust candidates would miss it)
is pinned explicitly.
"""

from __future__ import annotations

import logging
import sys
import types
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native import _kernels_scalar
from decoy_engine.execution.native._kernels_scalar import native_text_mask
from decoy_engine.execution.native._text_mask_native_splice import (
    SpliceKernel,
    load_splice_kernel,
    plan_native_splice,
)
from decoy_engine.execution.native._text_redact_kernel import SUPPORTED_IDS
from decoy_engine.transforms.text_mask import mask_cell
from tests.native._c6c_ii_support import ELIGIBLE_TEXTS, INELIGIBLE_TEXTS, NEEDS_TR_KERNEL

pytestmark = NEEDS_TR_KERNEL

KEY = b"\x07" * 32
ALL_IDS = (*SUPPORTED_IDS, "ssn", "us_zip", "street_address")
_DEFAULTS: dict[str, Any] = {
    "per_detector_strategy": None,
    "unmatched_span_policy": "redact",
    "token": "[REDACTED]",
    "date_shift_bounds": (),
    "sub_floor_span_policy": None,
}


def _native(values: list[str | None], **over: Any) -> list[Any]:
    kw = {**_DEFAULTS, "detectors": None, **over}
    arr, _ = native_text_mask(pa.array(values, pa.string()), mask_key=KEY, column="s", **kw)
    return arr.to_pylist()


def _oracle(values: list[str | None], **over: Any) -> list[Any]:
    kw = {**_DEFAULTS, "detectors": None, **over}
    detectors = kw["detectors"]
    return [
        None
        if v is None
        else mask_cell(
            v,
            KEY,
            detector_ids=list(detectors) if detectors is not None else None,
            extra_spans=None,
            strategy_map=dict(kw["per_detector_strategy"]) if kw["per_detector_strategy"] else None,
            unmatched_span_policy=kw["unmatched_span_policy"],
            token=kw["token"],
            sub_floor_span_policy=kw["sub_floor_span_policy"],
            sub_floor_notices={},
        )
        for v in values
    ]


class _Paths:
    """Which splice ran: the compiled calls (by entry point) and the Python `mask_cell_with_spans`."""

    def __init__(self) -> None:
        self.detect_splice_cells = 0
        self.splice_spans_cells = 0
        self.python_splice_cells = 0


@pytest.fixture
def paths(monkeypatch: pytest.MonkeyPatch) -> _Paths:
    seen = _Paths()
    real_load = _kernels_scalar.load_splice_kernel

    def load() -> SpliceKernel | None:
        loaded = real_load()
        if loaded is None:
            return None

        def detect(values: Any, *rest: Any) -> Any:
            seen.detect_splice_cells += len(values)
            return loaded.detect_splice(values, *rest)

        def spans(values: Any, *rest: Any) -> Any:
            seen.splice_spans_cells += len(values)
            return loaded.splice_spans(values, *rest)

        return SpliceKernel(spans, detect)

    real_python = _kernels_scalar.mask_cell_with_spans

    def counting(*a: Any, **k: Any) -> Any:
        seen.python_splice_cells += 1
        return real_python(*a, **k)

    monkeypatch.setattr(_kernels_scalar, "load_splice_kernel", load)
    monkeypatch.setattr(_kernels_scalar, "mask_cell_with_spans", counting)
    return seen


def _strategies(strategy: str, ids: tuple[str, ...] = ALL_IDS) -> dict[str, str]:
    return {d: strategy for d in ids}


CORPUS: list[str | None] = [
    *ELIGIBLE_TEXTS,
    "a@b.comc@d.org",  # back-to-back spans, empty gap between them
    "a@b.com",  # the whole cell is one span, no gaps at all
    "no match at all",  # zero spans: the whole cell is one gap
    "",
    None,
    "10.0.0.1 10.0.0.2",
    "10.0.0.1a@b.com",  # ipv4 and email share a start; the longer email must win
    "a@b.com10.0.0.1",  # adjacent spans from different detectors, no gap between them
    "x" * 200 + " 10.0.0.1 " + "y" * 200,
    "ssn 123-45-6789 ok",
    "Dr. Smith (NPI 1234567893) phoned 212-555-1234, email DR@CLINIC.ORG, ssn 987-65-4321.",
]


CONFIGS: dict[str, dict[str, Any]] = {
    "all_redact_gap_redact": {"per_detector_strategy": _strategies("redact")},
    "all_redact_gap_passthrough": {
        "per_detector_strategy": _strategies("redact"),
        "unmatched_span_policy": "passthrough",
    },
    "all_passthrough_gap_redact": {"per_detector_strategy": _strategies("passthrough")},
    "all_passthrough_gap_passthrough": {
        "per_detector_strategy": _strategies("passthrough"),
        "unmatched_span_policy": "passthrough",
    },
    "mixed_gap_redact": {
        "per_detector_strategy": {
            **_strategies("redact"),
            "email": "passthrough",
            "ipv4": "passthrough",
            "ssn": "passthrough",
        },
    },
    "mixed_gap_passthrough": {
        "per_detector_strategy": {**_strategies("redact"), "email": "passthrough"},
        "unmatched_span_policy": "passthrough",
    },
    "custom_token": {"per_detector_strategy": _strategies("redact"), "token": "<#>"},
    # No strategy map at all: these detectors default to redact, so the config is eligible.
    "default_redact_subset": {"detectors": ("email", "iban", "ipv4", "icd10", "url")},
    "rust_only_subset_gap_passthrough": {
        "detectors": ("email", "us_phone", "pan"),
        "per_detector_strategy": _strategies("redact", ("email", "us_phone", "pan")),
        "unmatched_span_policy": "passthrough",
    },
    "duplicate_ids": {
        "detectors": ("email", "ipv4", "email"),
        "per_detector_strategy": _strategies("redact"),
    },
    "requested_order_ipv4_first": {
        "detectors": ("ipv4", "email"),
        "per_detector_strategy": _strategies("redact"),
        "unmatched_span_policy": "passthrough",
    },
    "no_detectors": {"detectors": (), "per_detector_strategy": _strategies("redact")},
}


@pytest.mark.parametrize("name", sorted(CONFIGS))
def test_b1_eligible_configs_splice_in_rust_and_match_the_oracle(name: str, paths: _Paths) -> None:
    cfg = CONFIGS[name]
    assert _native(CORPUS, **cfg) == _oracle(CORPUS, **cfg)
    assert paths.detect_splice_cells + paths.splice_spans_cells > 0, "the Rust splice never ran"
    # Only non-ASCII cells (declined by the kernel) may touch the Python splice.
    assert paths.python_splice_cells == 0


def test_b1_all_rust_detectors_use_the_one_call_detect_splice(paths: _Paths) -> None:
    cfg = CONFIGS["rust_only_subset_gap_passthrough"]
    _native(list(ELIGIBLE_TEXTS), **cfg)
    assert paths.detect_splice_cells == len(ELIGIBLE_TEXTS)
    assert paths.splice_spans_cells == 0


def test_b1_lookaround_detector_is_spliced_from_merged_spans(paths: _Paths) -> None:
    """`ssn` is detected in Python. A splice over raw Rust candidates would leave it in place."""
    cfg = {
        "detectors": ("ssn",),
        "per_detector_strategy": {"ssn": "redact"},
        "unmatched_span_policy": "passthrough",
    }
    got = _native(["123-45-6789"], **cfg)
    assert got == _oracle(["123-45-6789"], **cfg) == ["[REDACTED]"]
    assert paths.splice_spans_cells == 1 and paths.detect_splice_cells == 0
    assert paths.python_splice_cells == 0
    # The same input with a hybrid selection (Rust + lookaround interleaved).
    mixed = {**cfg, "detectors": ("email", "ssn", "ipv4")}
    cells = ["mail a@b.com ssn 123-45-6789 at 10.0.0.1"]
    assert _native(cells, **mixed) == _oracle(cells, **mixed)


@pytest.mark.parametrize("name", sorted(CONFIGS))
def test_b1_non_ascii_and_control_cells_fall_back_and_match(name: str, paths: _Paths) -> None:
    cfg = CONFIGS[name]
    cells = [*INELIGIBLE_TEXTS, "plain a@b.com", "café a@b.com 10.0.0.1"]
    assert _native(cells, **cfg) == _oracle(cells, **cfg)
    assert paths.python_splice_cells > 0, "non-ASCII cells must take the Python splice"


@pytest.mark.parametrize(
    "cfg",
    [
        {},  # defaults put ssn/us_phone/pan on fpe
        {"per_detector_strategy": {**_strategies("redact"), "us_phone": "fpe"}},
        {"per_detector_strategy": {**_strategies("redact"), "email": "faker"}},
        {"per_detector_strategy": {**_strategies("redact"), "ssn": "date_shift"}},
        {
            "per_detector_strategy": _strategies("redact"),
            "unmatched_span_policy": "replace_with_token",
        },
        {"per_detector_strategy": _strategies("redact"), "unmatched_span_policy": "bogus"},
    ],
    ids=[
        "default-fpe",
        "one-fpe",
        "one-faker",
        "one-date-shift",
        "replace-with-token",
        "bogus-policy",
    ],
)
def test_b1_ineligible_configs_use_the_python_splice(cfg: dict[str, Any], paths: _Paths) -> None:
    cfg = {**cfg, "sub_floor_span_policy": "redact"}
    assert _native(CORPUS, **cfg) == _oracle(CORPUS, **cfg)
    assert paths.detect_splice_cells == 0 and paths.splice_spans_cells == 0
    assert paths.python_splice_cells > 0


def test_b1_plan_resolves_the_complete_detector_set_first() -> None:
    redact_all = _strategies("redact")
    # detectors=None is every detector: a single fpe default (ssn) among them blocks the plan.
    assert plan_native_splice(None, None, "redact", "[R]") is None
    assert plan_native_splice(None, redact_all, "redact", "[R]") is not None
    # An fpe default for a detector that is NOT requested does not block it.
    assert plan_native_splice(["email"], None, "redact", "[R]") is not None
    assert plan_native_splice(["ssn"], None, "redact", "[R]") is None
    # A non-string token cannot be joined by the splice; the oracle normalizes it.
    assert plan_native_splice(["email"], None, "redact", 5) is None
    plan = plan_native_splice(["email", "ssn"], {"ssn": "redact"}, "redact", "[R]")
    assert plan is not None and plan.rust_ids is None, "ssn is Python-detected: merged-span mode"


def test_b1_crash_class_configs_decline_to_the_oracle() -> None:
    # Codex final-gate MED-1/MED-2: configs where the oracle returns a value but the native path
    # would raise -- an unrepresentable token (a lone surrogate cannot encode to UTF-8 for Rust),
    # and a non-string strategy value (unhashable, crashes the set-membership test). Both must
    # decline at plan time so the Python splice reproduces the oracle; a crash is worse than a diff.
    surrogate = "\ud800"
    assert plan_native_splice(["email"], None, "redact", surrogate) is None
    assert plan_native_splice(["email"], {"email": []}, "redact", "[R]") is None
    assert plan_native_splice(["email"], {"email": {}}, "redact", "[R]") is None
    # MED-1 end-to-end: Codex's repro -- cells with NO matching span, so the surrogate token is
    # never emitted into the output (Arrow can hold the result). Pre-fix the native path eagerly
    # passed the token to Rust and crashed on the PyO3 conversion; the oracle returns the cells
    # unchanged. (A cell that DOES emit a surrogate token crashes Arrow output on every route,
    # including slice a and full-frame-on-Arrow; that is a pre-existing Arrow limit, out of scope.)
    plain = ["plain text", "nothing here"]
    assert _native(
        plain, detectors=["email"], token=surrogate, unmatched_span_policy="passthrough"
    ) == _oracle(plain, detectors=["email"], token=surrogate, unmatched_span_policy="passthrough")
    # MED-2 end-to-end: a non-string (unhashable) strategy value on a cell WITH a span.
    spanned = ["mail a@b.com now", "x"]
    assert _native(spanned, detectors=["email"], per_detector_strategy={"email": []}) == _oracle(
        spanned, detectors=["email"], per_detector_strategy={"email": []}
    )


def test_b1_older_companion_without_the_symbols_keeps_slice_a(
    monkeypatch: pytest.MonkeyPatch, paths: _Paths
) -> None:
    monkeypatch.setattr(_kernels_scalar, "load_splice_kernel", lambda: None)
    cfg = CONFIGS["all_redact_gap_redact"]
    assert _native(CORPUS, **cfg) == _oracle(CORPUS, **cfg)
    assert paths.python_splice_cells > 0


def test_b1_passthrough_policy_still_warns_per_cell(caplog: pytest.LogCaptureFixture) -> None:
    """The oracle warns once per non-empty cell under `passthrough`; the native cell must too."""
    cfg = CONFIGS["all_redact_gap_passthrough"]
    cells = ["a@b.com here", "", "plain", None]

    def warnings(fn: Any) -> int:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="decoy_engine.transforms.text_mask"):
            fn(cells, **cfg)
        return sum("unmatched_span_policy='passthrough'" in r.getMessage() for r in caplog.records)

    assert warnings(_native) == warnings(_oracle) == 2


def test_b1_negative_control_an_injected_diff_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the compiled splice returned a different string the parity comparison would catch it."""
    real_load = _kernels_scalar.load_splice_kernel

    def corrupt() -> SpliceKernel | None:
        loaded = real_load()
        assert loaded is not None

        def detect(*a: Any) -> Any:
            return [
                None if r is None else r.replace("[REDACTED]", "[REDACTE]")
                for r in loaded.detect_splice(*a)
            ]

        return SpliceKernel(loaded.splice_spans, detect)

    monkeypatch.setattr(_kernels_scalar, "load_splice_kernel", corrupt)
    cfg = CONFIGS["rust_only_subset_gap_passthrough"]
    cells = ["mail a@b.com now"]
    assert _native(cells, **cfg) != _oracle(cells, **cfg)


def test_b1_loader_returns_none_without_the_companion_or_its_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert load_splice_kernel() is not None
    monkeypatch.setitem(sys.modules, "decoy_engine_native", None)
    assert load_splice_kernel() is None
    older = types.ModuleType("decoy_engine_native")
    older._kernel = types.SimpleNamespace(text_mask_splice_spans=lambda *a: [])  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "decoy_engine_native", older)
    monkeypatch.setitem(sys.modules, "decoy_engine_native._kernel", older._kernel)  # type: ignore[attr-defined]
    assert load_splice_kernel() is None
