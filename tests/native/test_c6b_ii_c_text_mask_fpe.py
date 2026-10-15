"""C6b-ii slice c (T-c1): plain-digit `fpe` text_mask spans run through the compiled FF1 kernel.

The oracle is the shipped per-cell `mask_cell` loop, so equality is native-vs-oracle with no
hand-written expected output, except the pinned known-answer values, which freeze the Python
oracle's FF1 output for a fixed key so a drift in either side is caught. Each case also asserts
WHICH path produced the span (the kernel versus `_mask_span`), so a silent fall-through to Python
cannot pass as native.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.errors import FpeUnencryptableError
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution.native import _text_mask_native_fpe as nfpe
from decoy_engine.execution.native._kernels_scalar import native_text_mask
from decoy_engine.transforms import text_mask as text_mask_mod
from decoy_engine.transforms.text_mask import _mask_fpe, mask_cell
from tests.native._c6c_ii_support import NEEDS_TR_KERNEL

pytestmark = NEEDS_TR_KERNEL

KEY = b"\x07" * 32
_DEFAULTS: dict[str, Any] = {
    "per_detector_strategy": None,
    "unmatched_span_policy": "redact",
    "token": "[REDACTED]",
    "date_shift_bounds": (),
    "sub_floor_span_policy": None,
}


def _kw(over: dict[str, Any]) -> dict[str, Any]:
    return {**_DEFAULTS, "detectors": None, **over}


_COUNTING = [False]  # spies count only inside the native call, not the oracle run


def _native(values: Sequence[str | None], **over: Any) -> tuple[list[Any], dict[str, int]]:
    _COUNTING[0] = True
    try:
        arr, notices = native_text_mask(
            pa.array(list(values), pa.string()), mask_key=KEY, column="s", **_kw(over)
        )
    finally:
        _COUNTING[0] = False
    return arr.to_pylist(), notices


def _oracle(values: Sequence[str | None], **over: Any) -> tuple[list[Any], dict[str, int]]:
    kw = _kw(over)
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
            sub_floor_span_policy=kw["sub_floor_span_policy"],
            sub_floor_notices=notices,
        )
        for v in values
    ]
    return out, notices


class _Paths:
    def __init__(self) -> None:
        self.kernel_rows = 0
        self.python_fpe_spans = 0


@pytest.fixture
def paths(monkeypatch: pytest.MonkeyPatch) -> _Paths:
    """Count rows handed to the compiled FF1 and fpe spans that fell to `_mask_span`."""
    seen = _Paths()
    nfpe.load_fpe_batch.cache_clear()
    real = nfpe.load_fpe_batch()
    assert real is not None, "the companion's fpe_transform_batch must load for these tests"

    def spy_kernel(values: Any, **kw: Any) -> Any:
        seen.kernel_rows += len(values) if _COUNTING[0] else 0
        return real(values, **kw)

    real_span = nfpe._mask_span

    def spy_span(span: Any, mask_key: Any, strategy: str, *a: Any, **k: Any) -> str:
        if strategy == "fpe" and _COUNTING[0]:
            seen.python_fpe_spans += 1
        return real_span(span, mask_key, strategy, *a, **k)

    monkeypatch.setattr(nfpe, "load_fpe_batch", lambda: spy_kernel)
    monkeypatch.setattr(nfpe, "_mask_span", spy_span)
    # Cells with no native span reach `_mask_span` through the oracle's own splice.
    monkeypatch.setattr(text_mask_mod, "_mask_span", spy_span)
    return seen


FPE_ONLY = {"per_detector_strategy": {"us_phone": "fpe", "us_zip": "fpe", "ssn": "fpe"}}

NATIVE_CORPUS = [
    "call (415) 555-0132 today",
    "415-555-0132",
    "zip 94105-1234 here",
    "94105-1234 and 10001-5678",
    "phones 212-555-1234 and (212) 555-1234 x",
    "SSN 123-45-6789 and phone 415-555-0132",
    "123-45-6789 123-45-6789",  # equal texts share one kernel result
    "no pii here at all",
    "",
    None,
]


def test_known_answers_pinned_to_the_python_oracle() -> None:
    # Frozen from `_mask_fpe` (Python FF1) at key 0x07*32. The compiled path must reproduce them
    # through the whole text_mask route, and the Python oracle must still produce them.
    pinned = {
        ("us_phone", "(415) 555-0132"): "(485) 317-3497",
        ("us_phone", "415-555-0132"): "485-317-3497",
        ("us_zip", "94105-1234"): "29286-8924",
        ("us_zip", "941051234"): "292868924",
        ("ssn", "123-45-6789"): "770-74-0746",
    }
    for (det, text), want in pinned.items():
        assert _mask_fpe(text, KEY, det) == want
    outputs = nfpe.encrypt_spans(
        [
            [
                _span(det, text)
                for (det, text) in pinned  # one synthetic span per pinned pair
            ]
        ],
        nfpe.NATIVE_FPE_DETECTORS,
        KEY,
    )
    assert outputs == {k: v for k, v in pinned.items()}
    # A different key changes the output (the key is really used).
    assert _mask_fpe("(415) 555-0132", b"\x01" * 8, "us_phone") == "(993) 094-6531"
    other = nfpe.encrypt_spans(
        [[_span("us_phone", "(415) 555-0132")]], nfpe.NATIVE_FPE_DETECTORS, b"\x01" * 8
    )
    assert other == {("us_phone", "(415) 555-0132"): "(993) 094-6531"}


def _span(det: str, text: str) -> Any:
    from decoy_engine.storm.detectors import Span

    return Span(det, 0, len(text), text)


def test_native_fpe_spans_match_the_oracle_and_run_in_the_kernel(paths: _Paths) -> None:
    got, got_notices = _native(NATIVE_CORPUS, **FPE_ONLY)
    want, want_notices = _oracle(NATIVE_CORPUS, **FPE_ONLY)
    assert got == want
    assert got_notices == want_notices == {}
    assert paths.kernel_rows > 0
    assert paths.python_fpe_spans == 0  # every fpe span was native


def test_default_config_uses_the_kernel_for_digit_spans(paths: _Paths) -> None:
    got, _ = _native(NATIVE_CORPUS)
    want, _ = _oracle(NATIVE_CORPUS)
    assert got == want
    assert paths.kernel_rows > 0


@pytest.mark.parametrize("sub_floor", ["redact", "synthetic"])
def test_zip5_declines_to_python_inside_the_same_cell(sub_floor: str, paths: _Paths) -> None:
    cells = ["zip 94105 and 94105-1234", "94105", "phone 415-555-0132 zip 94105"]
    got, got_notices = _native(cells, sub_floor_span_policy=sub_floor, **FPE_ONLY)
    want, want_notices = _oracle(cells, sub_floor_span_policy=sub_floor, **FPE_ONLY)
    assert got == want
    assert got_notices == want_notices
    assert got_notices == {"us_zip": 3}
    assert paths.python_fpe_spans == 3  # each ZIP5 handled by Python exactly once
    assert paths.kernel_rows > 0  # the ZIP+4 and phone spans still ran natively


@pytest.mark.parametrize(
    "det,cell", [("pan", "card 4111 1111 1111 1111 x"), ("npi", "npi 1234567893 x")]
)
def test_checksum_detectors_never_reach_the_kernel(det: str, cell: str, paths: _Paths) -> None:
    got, got_notices = _native([cell], sub_floor_span_policy="redact", detectors=[det])
    want, want_notices = _oracle([cell], sub_floor_span_policy="redact", detectors=[det])
    assert got == want
    assert got_notices == want_notices
    assert paths.kernel_rows == 0
    assert paths.python_fpe_spans == 1
    assert det not in nfpe.NATIVE_FPE_DETECTORS


def test_checksum_decline_is_enforced_even_when_requested_by_strategy(paths: _Paths) -> None:
    # pan/npi forced to fpe explicitly: still the Python checksum path.
    cell = "card 4111 1111 1111 1111 and npi 1234567893 and 415-555-0132"
    over = {"per_detector_strategy": {"pan": "fpe", "npi": "fpe", "us_phone": "fpe"}}
    got, n1 = _native([cell], sub_floor_span_policy="redact", **over)
    want, n2 = _oracle([cell], sub_floor_span_policy="redact", **over)
    assert (got, n1) == (want, n2)
    assert paths.python_fpe_spans == 2  # pan and npi
    assert paths.kernel_rows == 1  # only the phone


def test_mixed_cell_one_native_span_one_declined_span(paths: _Paths) -> None:
    cell = "phone 415-555-0132 card 4111 1111 1111 1111 zip 94105"
    over = {"per_detector_strategy": {"pan": "fpe", "us_phone": "fpe", "us_zip": "fpe"}}
    got, n1 = _native([cell], sub_floor_span_policy="synthetic", **over)
    want, n2 = _oracle([cell], sub_floor_span_policy="synthetic", **over)
    assert (got, n1) == (want, n2)
    assert paths.kernel_rows == 2  # the phone (accepted) and the ZIP5 (offered, rejected)
    assert paths.python_fpe_spans == 2  # pan (checksum) and the rejected ZIP5


def test_preserve_separators_exact() -> None:
    cells = ["(415) 555-0132", "415-555-0132", "+1 415-555-0132", "x 415-555-0132 y"]
    got, _ = _native(cells, unmatched_span_policy="passthrough", **FPE_ONLY)
    want, _ = _oracle(cells, unmatched_span_policy="passthrough", **FPE_ONLY)
    assert got == want
    for g, w in zip(got, cells, strict=True):
        assert g != w  # something was encrypted
        assert [i for i, c in enumerate(g) if not c.isdigit()] == [
            i for i, c in enumerate(w) if not c.isdigit()
        ]


def test_unset_sub_floor_policy_raises_like_the_oracle() -> None:
    cells = ["phone 415-555-0132", "zip 94105 here", "zip 94105 again"]
    with pytest.raises(StrategyError) as native_exc:
        _native(cells, **FPE_ONLY)
    with pytest.raises(FpeUnencryptableError):
        _oracle(cells, **FPE_ONLY)
    assert native_exc.value.code == "fpe_unencryptable_domain"


def test_exception_order_and_notices_before_the_failure(paths: _Paths) -> None:
    # An unknown sub_floor policy raises on the FIRST declined span; notices counted before it
    # must match the oracle's, which only sees spans in order.
    cells = ["phone 415-555-0132", "zip 94105"]
    with pytest.raises(StrategyError):
        _native(cells, sub_floor_span_policy="bogus", **FPE_ONLY)
    assert paths.python_fpe_spans == 1


def test_non_ascii_cell_stays_python(paths: _Paths) -> None:
    cells = ["café 415-555-0132", "ok 415-555-0132"]
    got, _ = _native(cells, **FPE_ONLY)
    want, _ = _oracle(cells, **FPE_ONLY)
    assert got == want
    assert paths.kernel_rows == 1  # only the ASCII cell's span
    assert paths.python_fpe_spans == 1


def test_older_companion_without_the_kernel_declines_everything(
    monkeypatch: pytest.MonkeyPatch, paths: _Paths
) -> None:
    monkeypatch.setattr(nfpe, "load_fpe_batch", lambda: None)
    got, _ = _native(NATIVE_CORPUS, **FPE_ONLY)
    want, _ = _oracle(NATIVE_CORPUS, **FPE_ONLY)
    assert got == want
    assert paths.kernel_rows == 0


def test_whole_batch_kernel_error_declines_to_the_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise ValueError("seed_wrong_length: x")

    monkeypatch.setattr(nfpe, "load_fpe_batch", lambda: boom)
    got, _ = _native(NATIVE_CORPUS, **FPE_ONLY)
    want, _ = _oracle(NATIVE_CORPUS, **FPE_ONLY)
    assert got == want


def test_randomised_digit_spans_match_the_python_oracle(paths: _Paths) -> None:
    rng = random.Random(0xC6B)

    def digits(n: int) -> str:
        return "".join(rng.choice("0123456789") for _ in range(n))

    cells: list[str | None] = []
    for _ in range(300):
        parts = [
            f"({digits(3)}) {digits(3)}-{digits(4)}",
            f"{digits(3)}-{digits(3)}-{digits(4)}",
            f"{digits(5)}-{digits(4)}",
            f"{digits(3)}-{digits(2)}-{digits(4)}",
            f"{digits(5)}",
            "word",
        ]
        rng.shuffle(parts)
        cells.append(" / ".join(parts[: rng.randint(1, 4)]))
    got, n1 = _native(cells, sub_floor_span_policy="synthetic", **FPE_ONLY)
    want, n2 = _oracle(cells, sub_floor_span_policy="synthetic", **FPE_ONLY)
    assert got == want
    assert n1 == n2
    assert paths.kernel_rows > 0


def test_negative_control_an_injected_difference_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the kernel result were wrong the comparison must notice (guards a vacuous equality)."""
    real = nfpe.load_fpe_batch()
    assert real is not None

    def wrong(values: Any, **kw: Any) -> Any:
        out, errors = real(values, **kw)
        return pa.array(["0" * len(v) if v else v for v in out.to_pylist()]), errors

    monkeypatch.setattr(nfpe, "load_fpe_batch", lambda: wrong)
    got, _ = _native(["call 415-555-0132"], **FPE_ONLY)
    want, _ = _oracle(["call 415-555-0132"], **FPE_ONLY)
    assert got != want


def test_wrong_tweak_or_key_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A kernel call using the wrong detector tweak must not equal the oracle (mutant guard)."""
    real = nfpe.load_fpe_batch()
    assert real is not None

    def wrong_tweak(values: Any, **kw: Any) -> Any:
        kw["tweak"] = kw["tweak"][:-1] + b"X"
        return real(values, **kw)

    monkeypatch.setattr(nfpe, "load_fpe_batch", lambda: wrong_tweak)
    got, _ = _native(["call 415-555-0132"], **FPE_ONLY)
    want, _ = _oracle(["call 415-555-0132"], **FPE_ONLY)
    assert got != want

    def wrong_ns(values: Any, **kw: Any) -> Any:
        kw["namespace"] = "text.ssn"
        return real(values, **kw)

    monkeypatch.setattr(nfpe, "load_fpe_batch", lambda: wrong_ns)
    got2, _ = _native(["call 415-555-0132"], **FPE_ONLY)
    assert got2 != want


def test_a_companion_that_fails_its_fpe_self_test_loads_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.execution.native import _fpe_ext

    def failing() -> Any:
        raise _fpe_ext.CryptoExtensionUnavailableError("stale binary")

    monkeypatch.setattr(_fpe_ext, "load_compiled_fpe_kernel", failing)
    nfpe.load_fpe_batch.cache_clear()
    try:
        assert nfpe.load_fpe_batch() is None
        got, _ = _native(NATIVE_CORPUS, **FPE_ONLY)
        want, _ = _oracle(NATIVE_CORPUS, **FPE_ONLY)
        assert got == want
    finally:
        nfpe.load_fpe_batch.cache_clear()
