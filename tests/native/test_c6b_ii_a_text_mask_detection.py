"""C6b-ii slice a: `native_text_mask` takes its spans from the compiled detection kernel.

The oracle is the shipped per-cell `mask_cell` loop (the C6b-i path), so every comparison is
native-vs-oracle and no expected output is hand-written. The splice stays Python in this slice,
so each parity cell also proves the kernel was consulted and `mask_cell` was not. Every
fallback (kernel absent, catalog skew, ineligible cell) must be the oracle's own path.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.detection.detectors import Span
from decoy_engine.errors import FpeUnencryptableError
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution.native import (
    _kernels_scalar,
    _text_mask_native_splice,
    _text_redact_kernel,
)
from decoy_engine.execution.native._kernels_scalar import native_text_mask
from decoy_engine.execution.native._text_redact_kernel import SUPPORTED_IDS
from decoy_engine.transforms.text_mask import mask_cell
from tests.native._c6b_i_support import CORPUS, SUB_FLOOR_TEXTS
from tests.native._c6c_ii_support import (
    DETECTOR_SELECTIONS,
    ELIGIBLE_TEXTS,
    INELIGIBLE_TEXTS,
    NEEDS_TR_KERNEL,
)

pytestmark = NEEDS_TR_KERNEL

KEY = b"\x07" * 32
# The free-text corpora carry 5-digit zips and checksum values, which fail closed with no policy,
# so the sweeps run under "redact"; the fail-closed path has its own test below.
_SWEEP: dict[str, Any] = {"sub_floor_span_policy": "redact"}
_DEFAULTS: dict[str, Any] = {
    "per_detector_strategy": None,
    "unmatched_span_policy": "redact",
    "token": "[REDACTED]",
    "date_shift_bounds": (),
    "sub_floor_span_policy": None,
}


def _native(values: list[str | None], **over: Any) -> tuple[list[Any], dict[str, int]]:
    kw = {**_DEFAULTS, "detectors": None, **over}
    arr, notices = native_text_mask(pa.array(values, pa.string()), mask_key=KEY, column="s", **kw)
    return arr.to_pylist(), notices


def _oracle(values: list[str | None], **over: Any) -> tuple[list[Any], dict[str, int]]:
    kw = {**_DEFAULTS, "detectors": None, **over}
    notices: dict[str, int] = {}
    detectors = kw["detectors"]
    out = [
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
            cfg=dict(kw["date_shift_bounds"]) or None,
            sub_floor_span_policy=kw["sub_floor_span_policy"],
            sub_floor_notices=notices,
        )
        for v in values
    ]
    return out, notices


class _Spies:
    """What the native call consulted: kernel batches (cells, detector ids) and `mask_cell`."""

    def __init__(self) -> None:
        self.kernel_calls: list[tuple[int, tuple[str, ...]]] = []
        self.mask_cell_calls = 0
        self.iter_spans_calls = 0


@pytest.fixture
def spies(monkeypatch: pytest.MonkeyPatch) -> _Spies:
    spy = _Spies()
    cls = _text_redact_kernel._CompiledTextRedactKernel
    original = cls.text_redact_candidates

    def kernel(self: Any, values: Any, ids: Any, version: int) -> Any:
        spy.kernel_calls.append((len(values), tuple(ids)))
        return original(self, values, ids, version)

    real_mask_cell = _kernels_scalar.mask_cell
    real_iter_spans = _kernels_scalar.iter_spans

    def counting_mask_cell(*a: Any, **k: Any) -> Any:
        spy.mask_cell_calls += 1
        return real_mask_cell(*a, **k)

    def counting_iter_spans(*a: Any, **k: Any) -> Any:
        spy.iter_spans_calls += 1
        return real_iter_spans(*a, **k)

    real_load = _kernels_scalar.load_splice_kernel

    def load_splice() -> Any:
        # Slice b folds detection into the splice call for redact/passthrough configs, so the
        # detector set reaches the compiled kernel through `detect_splice` instead. Record it
        # the same way so these tests still see which detectors the kernel ran.
        loaded = real_load()
        if loaded is None:
            return None
        real_detect = loaded.detect_splice

        def detect(values: Any, ids: Any, *rest: Any) -> Any:
            spy.kernel_calls.append((len(values), tuple(ids)))
            return real_detect(values, ids, *rest)

        return _text_mask_native_splice.SpliceKernel(loaded.splice_spans, detect)

    monkeypatch.setattr(_kernels_scalar, "load_splice_kernel", load_splice)
    monkeypatch.setattr(cls, "text_redact_candidates", kernel)
    monkeypatch.setattr(_kernels_scalar, "mask_cell", counting_mask_cell)
    monkeypatch.setattr(_kernels_scalar, "iter_spans", counting_iter_spans)
    return spy


# T-a1: detection reuse is byte-identical across the four span strategies.

STRATEGY_CONFIGS: dict[str, dict[str, Any]] = {
    "default_fpe_redact": {},
    "all_redact": {
        "per_detector_strategy": {d: "redact" for d in (*SUPPORTED_IDS, "ssn", "us_zip")}
    },
    "all_passthrough": {
        "per_detector_strategy": {d: "passthrough" for d in (*SUPPORTED_IDS, "ssn", "us_zip")},
        "unmatched_span_policy": "passthrough",
    },
    "faker": {"per_detector_strategy": {"email": "faker", "ssn": "faker", "us_phone": "faker"}},
    "date_shift": {
        "per_detector_strategy": {"ssn": "date_shift"},
        "date_shift_bounds": (("min_days", -10), ("max_days", 10)),
    },
    "unmatched_replace": {"unmatched_span_policy": "replace_with_token", "token": "<X>"},
}


@pytest.mark.parametrize("name", sorted(STRATEGY_CONFIGS))
def test_a1_native_detection_matches_the_oracle(name: str, spies: _Spies) -> None:
    got = _native(CORPUS, **STRATEGY_CONFIGS[name])
    assert got == _oracle(CORPUS, **STRATEGY_CONFIGS[name])
    assert spies.kernel_calls, "the compiled detection kernel was not consulted"
    assert spies.mask_cell_calls == 0, "the oracle's mask_cell ran instead of the span splice"


def test_a1_negative_control_a_dropped_span_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """The comparison has power: a detection that loses one span must not equal the oracle."""
    real = _kernels_scalar.merge_text_redact_spans

    def lossy(*a: Any, **k: Any) -> list[Span]:
        return real(*a, **k)[1:]

    monkeypatch.setattr(_kernels_scalar, "merge_text_redact_spans", lossy)
    assert _native(CORPUS) != _oracle(CORPUS)


def test_a1_detection_order_matters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant: spans reversed after the merge. The oracle comparison must fail on a multi-span
    cell (the splice walks spans in start order), proving the order contract is graded."""
    real = _kernels_scalar.merge_text_redact_spans

    def reversed_spans(*a: Any, **k: Any) -> list[Span]:
        return list(reversed(real(*a, **k)))

    monkeypatch.setattr(_kernels_scalar, "merge_text_redact_spans", reversed_spans)
    cell = ["a@b.com and 10.0.0.1"]
    kw = {
        "per_detector_strategy": {"email": "passthrough", "ipv4": "passthrough"},
        "unmatched_span_policy": "passthrough",
    }
    assert _native(cell, **kw) != _oracle(cell, **kw)


# T-a2: every fallback is the oracle's own path.


def test_a2_kernel_absent_runs_mask_cell(monkeypatch: pytest.MonkeyPatch, spies: _Spies) -> None:
    monkeypatch.setattr(_kernels_scalar, "load_text_redact_kernel", lambda: None)
    assert _native(CORPUS) == _oracle(CORPUS)
    assert spies.kernel_calls == []
    assert spies.mask_cell_calls == sum(v is not None for v in CORPUS)


def test_a2_catalog_skew_falls_back_whole_column(
    monkeypatch: pytest.MonkeyPatch, spies: _Spies
) -> None:
    monkeypatch.setattr(_text_redact_kernel, "CATALOG_VERSION", 999)
    assert _native(CORPUS) == _oracle(CORPUS)
    assert spies.kernel_calls == []
    assert spies.mask_cell_calls > 0


@pytest.mark.parametrize(
    "cell",
    [*INELIGIBLE_TEXTS, "ascii \x1c control a@b.com", "x\x1fy 123-45-6789"],
    ids=lambda c: repr(c)[:24],
)
def test_a2_ineligible_cells_take_the_python_detection(cell: str, spies: _Spies) -> None:
    assert _native([cell]) == _oracle([cell])
    assert spies.iter_spans_calls == 1
    assert spies.mask_cell_calls == 0


def test_a2_lookaround_detector_cells_match(spies: _Spies) -> None:
    cells = ["ssn 123-45-6789 ok", "zip 12345-6789 here", "12 Main Street, Springfield", "x"]
    for selection in (["ssn"], ["us_zip"], ["street_address"], ["ssn", "email", "us_zip"]):
        assert _native(cells, detectors=tuple(selection)) == _oracle(
            cells, detectors=tuple(selection)
        )


def test_a2_leaf_extra_spans_survive_the_splice_extraction() -> None:
    """`mask_cell` still forwards NER-injected / directly supplied spans after the extraction."""
    text = "fax 5551234567 now"
    injected = [Span("fax_number", 4, 14, "5551234567")]
    out = mask_cell(
        text,
        KEY,
        detector_ids=[],
        extra_spans=injected,
        unmatched_span_policy="passthrough",
    )
    assert out.startswith("fax ") and out.endswith(" now")
    middle = out[4:-4]
    assert len(middle) == 10 and middle.isdigit() and middle != "5551234567"
    again = mask_cell(
        text, KEY, detector_ids=[], extra_spans=injected, unmatched_span_policy="passthrough"
    )
    assert again == out  # keyed, so deterministic
    # Without the injected span the same call finds nothing to mask, so the span was the cause.
    assert mask_cell(text, KEY, detector_ids=[], unmatched_span_policy="passthrough") == text


# T-a3: the detector set resolves exactly as the handler does.


def test_a3_none_all_explicit_full_and_single(spies: _Spies) -> None:
    cells = list(ELIGIBLE_TEXTS)
    every = _native(cells, detectors=None, **_SWEEP)
    assert every == _oracle(cells, detectors=None, **_SWEEP)
    assert spies.kernel_calls[-1] == (len(cells), tuple(SUPPORTED_IDS))
    # An explicit list of every detector is the same set, so the same output.
    full = tuple(DETECTOR_SELECTIONS_ALL)
    assert _native(cells, detectors=full, **_SWEEP) == _oracle(cells, detectors=full, **_SWEEP)
    assert _native(cells, detectors=full, **_SWEEP)[0] == every[0]
    assert _native(cells, detectors=("email",), **_SWEEP) == _oracle(
        cells, detectors=("email",), **_SWEEP
    )
    assert spies.kernel_calls[-1] == (len(cells), ("email",))


DETECTOR_SELECTIONS_ALL = (*SUPPORTED_IDS, "ssn", "us_zip", "street_address")


def test_a3_empty_tuple_means_zero_detectors_never_all() -> None:
    """The kernel reads `()` as ZERO detectors; the handler resolves an empty list to None
    BEFORE it reaches here. A mutant substituting `()` for `None` masks nothing and fails this."""
    cells = ["mail a@b.com now", "ssn 123-45-6789"]
    as_none = _native(cells, detectors=None)[0]
    as_empty = _native(cells, detectors=())[0]
    assert as_none == _oracle(cells, detectors=None)[0]
    assert as_empty == _oracle(cells, detectors=())[0]
    assert as_none != as_empty
    assert "a@b.com" not in as_none[0]


@pytest.mark.parametrize("name", sorted(DETECTOR_SELECTIONS))
def test_a3_every_selection_shape_matches_the_oracle(name: str) -> None:
    selection = DETECTOR_SELECTIONS[name]
    cells = [*ELIGIBLE_TEXTS, *INELIGIBLE_TEXTS]
    assert _native(cells, detectors=selection, **_SWEEP) == _oracle(
        cells, detectors=selection, **_SWEEP
    )


# Sub-floor counts come out of the one call, identical to the oracle's.


@pytest.mark.parametrize("policy", ["redact", "synthetic"])
def test_a_sub_floor_notices_match_the_oracle(policy: str) -> None:
    got = _native(SUB_FLOOR_TEXTS, sub_floor_span_policy=policy)
    assert got == _oracle(SUB_FLOOR_TEXTS, sub_floor_span_policy=policy)
    assert got[1], "the fixture must drive the sub-floor path"


def test_a_fail_closed_matches_the_oracle_error() -> None:
    """A sub-floor span with no policy raises the handler's coded error, not a raw exception."""
    with pytest.raises(StrategyError) as native:
        _native(SUB_FLOOR_TEXTS)
    assert native.value.code == "fpe_unencryptable_domain"
    assert native.value.strategy == "text_mask"
    with pytest.raises(FpeUnencryptableError):
        _oracle(SUB_FLOOR_TEXTS)


# T-common: realistic free text, every route shape the slice landed (unified/chunked share this
# entry point), overlapping / back-to-back spans, empty and all-gap / all-span cells.

REALISTIC = [
    *ELIGIBLE_TEXTS,
    *INELIGIBLE_TEXTS,
    "a@b.comc@d.org",
    "4111111111111111",
    "x" * 300 + " 10.0.0.1 " + "y" * 300,
    "10.0.0.1",
    "no match at all",
    "",
    "tail match a@b.com",
    "a@b.com head match",
    "Dr. Smith (NPI 1234567893) phoned 212-555-1234, email DR@CLINIC.ORG, ssn 987-65-4321.",
]


@pytest.mark.parametrize("policy", ["redact", "passthrough", "replace_with_token"])
@pytest.mark.parametrize("name", sorted(DETECTOR_SELECTIONS))
def test_common_realistic_sweep_matches_the_oracle(name: str, policy: str) -> None:
    kw = {"detectors": DETECTOR_SELECTIONS[name], "unmatched_span_policy": policy, **_SWEEP}
    assert _native(REALISTIC, **kw) == _oracle(REALISTIC, **kw)


@pytest.mark.parametrize("policy", ["redact", "passthrough", "replace_with_token"])
def test_an_empty_gap_is_never_tokenized(policy: str) -> None:
    """Two adjacent spans leave an empty gap that must not emit a token under any policy."""
    from decoy_engine.transforms._text_mask_splice import apply_unmatched, splice_spans

    assert apply_unmatched("", policy, "TOK") == ""
    spans = [Span("a", 0, 1, "x"), Span("b", 1, 2, "y")]
    out = splice_spans(
        "xy",
        spans,
        mask_span=lambda s: s.matched_text.upper(),
        unmatched_span_policy=policy,
        token="TOK",
    )
    assert out == "XY"
