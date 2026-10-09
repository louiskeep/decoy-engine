"""C6c-ii acceptance: the Rust span-detection kernel for text_redact (plan section 7).

Byte-identical to the C6c-i Python path is the bar. Every merged assertion compares the native
wrapper against `oracle_text_redact` (the literal `iter_spans` + `_splice` path); the raw-candidate
and validator tests compare the kernel against the Python detectors and validators directly. Tests
that need the new compiled symbol carry `@NEEDS_TR_KERNEL` and skip on an old/absent companion (the
companion-present CI job runs them); the merge-helper, routing-predicate-mirror and fallback tests
run on the Python path alone.
"""

from __future__ import annotations

import random
import re
import sys
import types
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native._kernels_scalar import native_text_redact
from decoy_engine.execution.native._text_redact_kernel import (
    CATALOG_LABELS,
    CATALOG_VERSION,
    SUPPORTED_IDS,
    is_ascii_safe,
    load_text_redact_kernel,
    merge_text_redact_spans,
    requested_rust_ids,
)
from decoy_engine.storm import detectors as storm_detectors
from decoy_engine.storm._validators import (
    _iban_valid,
    _icd10_valid,
    _ipv4_valid,
    _luhn_valid,
    _npi_valid,
)
from decoy_engine.storm.detectors import Span, iter_spans
from tests.native._c6c_ii_support import (
    CORPUS,
    DETECTOR_SELECTIONS,
    ELIGIBLE_TEXTS,
    INELIGIBLE_TEXTS,
    NEEDS_TR_KERNEL,
    RUST_DETECTORS,
    SHAPES,
    oracle_text_redact,
    python_raw_candidates,
)


def _kernel() -> Any:
    k = load_text_redact_kernel()
    assert k is not None, "the span kernel must load under the companion-present job"
    return k


def _native(values: list[Any], *, typ: pa.DataType | None = None, **kw: Any) -> list[Any]:
    return native_text_redact(pa.array(values, type=typ or pa.string()), **kw).to_pylist()


# ---------------------------------------------------------------------------
# 7.1 Routing-predicate boundary + golden.
# ---------------------------------------------------------------------------


def test_the_ascii_safe_predicate_marks_the_boundary() -> None:
    assert is_ascii_safe("plain ascii 123-45-6789 a@b.com")
    assert is_ascii_safe("")
    # One non-ASCII code point, or one 0x1c-0x1f separator, makes a cell ineligible.
    assert not is_ascii_safe("café")
    assert not is_ascii_safe("日本語")
    for sep in ("\x1c", "\x1d", "\x1e", "\x1f"):
        assert not is_ascii_safe(f"a{sep}b")
    # The chars just outside 0x1c-0x1f stay eligible.
    assert is_ascii_safe("a\x1bb")
    assert is_ascii_safe("a\x20b")


@NEEDS_TR_KERNEL
def test_the_kernel_eligibility_matches_the_python_predicate() -> None:
    kern = _kernel()
    from decoy_engine_native import _kernel as compiled

    for text in ELIGIBLE_TEXTS:
        assert compiled.text_redact_is_eligible(text) is True
        # An eligible cell returns a candidate list, never None.
        assert (
            kern.text_redact_candidates([text], list(SUPPORTED_IDS), CATALOG_VERSION)[0] is not None
        )
    for text in INELIGIBLE_TEXTS:
        assert compiled.text_redact_is_eligible(text) is False
        assert kern.text_redact_candidates([text], list(SUPPORTED_IDS), CATALOG_VERSION)[0] is None


@NEEDS_TR_KERNEL
def test_a_lone_surrogate_and_a_non_string_route_to_python() -> None:
    kern = _kernel()
    # A lone surrogate cannot be decoded to a Rust str; a non-string is not a cell the kernel
    # scans. Both come back None (Python must handle), distinct from [] (eligible, no match).
    results = kern.text_redact_candidates(
        ["\ud800abc", 12345, b"bytes", "a@b.com"], list(SUPPORTED_IDS), CATALOG_VERSION
    )
    assert results[0] is None
    assert results[1] is None
    assert results[2] is None
    assert results[3] == [(0, 0, 7)]  # email (index 0) spans all of "a@b.com"


@NEEDS_TR_KERNEL
def test_the_native_path_equals_the_oracle_on_the_golden_clinical_notes() -> None:
    from pathlib import Path

    golden = Path(__file__).resolve().parents[1] / "snapshots" / "golden" / "mask_text_redact"
    text = (golden / "clinical_notes_input.txt").read_text(encoding="utf-8")
    cells = text.splitlines(keepends=True)
    got = _native(cells, detectors=None, token="[REDACTED]", label_token=False)
    assert got == oracle_text_redact(cells, detectors=None, token="[REDACTED]", label_token=False)


# ---------------------------------------------------------------------------
# 7.2 Differential harness: raw candidates then merged, eligible AND ineligible cells.
# ---------------------------------------------------------------------------


@NEEDS_TR_KERNEL
@pytest.mark.parametrize("text", ELIGIBLE_TEXTS, ids=range(len(ELIGIBLE_TEXTS)))
def test_raw_candidates_match_python_per_detector_on_eligible_cells(text: str) -> None:
    kern = _kernel()
    index_of = {det_id: i for i, det_id in enumerate(SUPPORTED_IDS)}
    per_cell = kern.text_redact_candidates([text], list(SUPPORTED_IDS), CATALOG_VERSION)[0]
    assert per_cell is not None, "eligible cell must not route to Python"
    for det_id in RUST_DETECTORS:
        rust = [(s, e) for (idx, s, e) in per_cell if idx == index_of[det_id]]
        assert rust == python_raw_candidates(text, det_id), det_id


@NEEDS_TR_KERNEL
@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("selection", sorted(DETECTOR_SELECTIONS))
@pytest.mark.parametrize("label_token", [False, True], ids=["token", "label"])
def test_merged_output_equals_the_oracle(shape: str, selection: str, label_token: bool) -> None:
    values = SHAPES[shape]
    detectors = DETECTOR_SELECTIONS[selection]
    got = _native(values, detectors=detectors, token="<X>", label_token=label_token)
    assert got == oracle_text_redact(
        values, detectors=detectors, token="<X>", label_token=label_token
    )


@NEEDS_TR_KERNEL
def test_ineligible_cells_route_to_python_and_still_equal_the_oracle() -> None:
    kern = _kernel()
    for text in INELIGIBLE_TEXTS:
        assert kern.text_redact_candidates([text], list(SUPPORTED_IDS), CATALOG_VERSION)[0] is None
    got = _native(INELIGIBLE_TEXTS, detectors=None, token="T", label_token=True)
    assert got == oracle_text_redact(INELIGIBLE_TEXTS, detectors=None, token="T", label_token=True)


@NEEDS_TR_KERNEL
def test_the_two_named_tie_counterexamples_resolve_like_the_oracle() -> None:
    # "2234567891": npi and us_phone both span (0,10); detector order decides the winner, and
    # label_token makes the winner observable.
    for order in [("npi", "us_phone"), ("us_phone", "npi")]:
        got = _native(["2234567891"], detectors=order, token="T", label_token=True)
        assert got == oracle_text_redact(
            ["2234567891"], detectors=order, token="T", label_token=True
        )
    # "12345": us_zip (Python) vs an injected location span at (0,5) -- the merge-helper owns this
    # one (NER is not routed native); see test_merge_helper_* below.


@NEEDS_TR_KERNEL
def test_a_generated_clinical_corpus_matches_the_oracle() -> None:
    rng = random.Random(20261009)
    pii = [
        "a@b.com",
        "x.y+z@sub.example.co",
        "(555) 123-4567",
        "212-555-1234",
        "123-45-6789",
        "4111 1111 1111 1111",
        "1234567893",
        "E11.9",
        "z23",
        "10.0.0.1",
        "GB82WEST12345698765432",
        "https://ex.org/p?q=1",
        "12345",
        "90210-1234",
        "12 Main Street",
        "no pii",
        "",
        "weight 12345.6 mg",
    ]
    fillers = ["patient", "note", "seen", "on", "with", "re", "and", "today", "MRN", "visit"]
    cells: list[str | None] = []
    for _ in range(600):
        parts = [
            rng.choice(fillers if rng.random() < 0.5 else pii) for _ in range(rng.randint(0, 6))
        ]
        cell = " ".join(parts).strip()
        if rng.random() < 0.08:
            cells.append(None)
        elif rng.random() < 0.08:
            cells.append(cell + " café")  # push a fraction to the Python fallback
        else:
            cells.append(cell)
    for detectors in (None, ("email", "us_phone", "pan", "npi", "icd10", "ipv4")):
        for label in (False, True):
            got = _native(cells, detectors=detectors, token="<X>", label_token=label)
            assert got == oracle_text_redact(
                cells, detectors=detectors, token="<X>", label_token=label
            )


@NEEDS_TR_KERNEL
def test_non_string_arrays_are_stringified_and_match_the_oracle() -> None:
    for values, typ in (
        ([1234567893, 7, None], pa.int64()),
        ([2.5, None], pa.float64()),
        ([True, False, None], pa.bool_()),
    ):
        got = _native(values, typ=typ, detectors=None, token="T", label_token=False)
        assert got == oracle_text_redact(values, detectors=None, token="T", label_token=False)


# ---------------------------------------------------------------------------
# 7.3 Validator KATs (shared Python <-> Rust vectors on ASCII input).
# ---------------------------------------------------------------------------

_LUHN_KAT = {
    "0000000000000": True,
    "000000000000": False,
    "4111111111111111": True,
    "4111 1111 1111 1111": True,
    "4111-1111-1111-1111": True,
    "4111111111111112": False,
    "0000000000000000000": True,  # no upper-length cap
}
_NPI_KAT = {
    "1234567893": True,
    "1679576722": True,
    "1000000004": True,
    "1234567890": False,
    "123-456-7893": True,
}
_IPV4_KAT = {
    "001.002.003.004": True,
    "255.255.255.255": True,
    "256.1.1.1": False,
    "1.2.3": False,
    "1.2.3.4.5": False,
    "10.0.0.1": True,
}
_IBAN_KAT = {
    "GB82WEST12345698765432": True,
    "GB82 WEST 1234 5698 7654 32": True,
    "DE89370400440532013000": True,
    "ZZ00INVALID1234567890": False,
    "GB00WEST12345698765432": False,
    "GB82": False,
}
_ICD10_KAT = {
    "F00": False,
    "F01": True,
    "D89": True,
    "D90": False,
    "A00": True,
    "A00!!!!": True,
    "E11.9": True,
    "a01": True,
    "U07.1": True,
}

_KATS = [
    ("luhn", _luhn_valid, _LUHN_KAT),
    ("npi", _npi_valid, _NPI_KAT),
    ("ipv4", _ipv4_valid, _IPV4_KAT),
    ("iban", _iban_valid, _IBAN_KAT),
    ("icd10", _icd10_valid, _ICD10_KAT),
]


@pytest.mark.parametrize("name,py_fn,vectors", _KATS, ids=[k[0] for k in _KATS])
def test_python_validators_match_the_pinned_kat_vectors(
    name: str, py_fn: Any, vectors: dict[str, bool]
) -> None:
    """Pins the reference behavior the Rust port must match (runs without the companion)."""
    for value, expected in vectors.items():
        assert py_fn(value) is expected, (name, value)


@NEEDS_TR_KERNEL
@pytest.mark.parametrize("name,py_fn,vectors", _KATS, ids=[k[0] for k in _KATS])
def test_rust_validators_reproduce_the_python_validators(
    name: str, py_fn: Any, vectors: dict[str, bool]
) -> None:
    from decoy_engine_native import _kernel as compiled

    for value, expected in vectors.items():
        rust = compiled.text_redact_validate(name, value)
        assert rust == py_fn(value) == expected, (name, value)


# ---------------------------------------------------------------------------
# 7.4 Class-level ASCII equivalence (the constructive backing for 2.2).
# ---------------------------------------------------------------------------


def _python_class_members(pattern: str, flags: int) -> list[int]:
    rx = re.compile(pattern, flags)
    return [cp for cp in range(128) if rx.fullmatch(chr(cp))]


@NEEDS_TR_KERNEL
def test_rust_and_python_ascii_classes_agree_and_0x1c_0x1f_is_the_only_gap() -> None:
    from decoy_engine_native import _kernel as compiled

    # \d, \w, icd case-fold: Rust (?-u)/(?i-u) == Python re.ASCII == Python default over 0-127.
    for name, pattern in (("d", r"\d"), ("w", r"\w")):
        rust = list(compiled.text_redact_class_members(name))
        assert rust == _python_class_members(pattern, re.ASCII)
        assert rust == _python_class_members(pattern, 0)  # no ASCII-range divergence for \d,\w
    # icd10's (?i-u)[A-Z] folds to exactly [A-Za-z] on ASCII, same as Python re.I.
    assert list(compiled.text_redact_class_members("icd_ci")) == _python_class_members(
        r"[A-Z]", re.IGNORECASE
    )
    # \s is the ONLY construct where Python's default (Unicode) class exceeds the ASCII class, and
    # the excess over 0-127 is EXACTLY {0x1c,0x1d,0x1e,0x1f} -- the sole reason the predicate
    # excludes them (plan 2.1).
    rust_s = set(compiled.text_redact_class_members("s"))
    assert rust_s == set(_python_class_members(r"\s", re.ASCII))
    python_unicode_s = set(_python_class_members(r"\s", 0))
    assert python_unicode_s - rust_s == {0x1C, 0x1D, 0x1E, 0x1F}


@NEEDS_TR_KERNEL
def test_the_eight_patterns_use_only_d_s_and_ascii_case_fold() -> None:
    # None of the eight use \w or \b (those live in the Python-only lookaround detectors), so the
    # class-equivalence proof needs only \d, \s and ASCII case folding.
    for det_id in RUST_DETECTORS:
        pattern = storm_detectors._SPAN_DETECTORS[det_id][0].pattern
        assert r"\w" not in pattern, det_id
        assert r"\b" not in pattern, det_id


# ---------------------------------------------------------------------------
# 7.5 NER + custom interplay (merge helper, no companion needed).
# ---------------------------------------------------------------------------


def _simulated_rust(text: str, detector_ids: list[str] | None) -> list[tuple[int, int, int]]:
    """A correct kernel's candidate list for an eligible cell, built from the Python detectors."""
    index_of = {det_id: i for i, det_id in enumerate(SUPPORTED_IDS)}
    out: list[tuple[int, int, int]] = []
    for det_id in requested_rust_ids(detector_ids):
        for start, end in python_raw_candidates(text, det_id):
            out.append((index_of[det_id], start, end))
    return out


@pytest.mark.parametrize("detectors", [None, ("email", "ssn"), ("ipv4",), ()])
@pytest.mark.parametrize(
    "text",
    ["a@b.com and 10.0.0.1 ssn 123-45-6789", "", "12345", "no pii at all"],
)
def test_merge_helper_equals_iter_spans_with_extras_and_custom(
    detectors: tuple[str, ...] | None, text: str
) -> None:
    detector_ids = list(detectors) if detectors is not None else None
    extra = [Span("location", 0, 5, text[0:5])] if len(text) >= 5 else []
    custom = [{"detector_id": "mycode", "pattern": re.compile(r"\bpii\b"), "validator": None}]
    merged = merge_text_redact_spans(
        text,
        detector_ids,
        rust_candidates=_simulated_rust(text, detector_ids),
        custom=custom,
        extra_spans=list(extra),
    )
    expected = iter_spans(text, detector_ids, custom=custom, extra_spans=list(extra))
    assert merged == expected


def test_merge_helper_tie_counterexample_location_beats_us_zip() -> None:
    # "12345": an injected location span at (0,5) and us_zip at (0,5) tie; extras are inserted
    # before the built-ins, so location wins leftmost-then-longest, exactly as iter_spans resolves.
    text = "12345"
    extra = [Span("location", 0, 5, text)]
    merged = merge_text_redact_spans(
        text, ["us_zip"], rust_candidates=_simulated_rust(text, ["us_zip"]), extra_spans=extra
    )
    expected = iter_spans(text, ["us_zip"], extra_spans=extra)
    assert merged == expected
    assert merged == [Span("location", 0, 5, "12345")]


def test_merge_helper_none_rust_candidates_runs_all_python() -> None:
    text = "a@b.com and 10.0.0.1 ssn 123-45-6789"
    merged = merge_text_redact_spans(text, None, rust_candidates=None)
    assert merged == iter_spans(text, None)


def test_merge_helper_empty_text_returns_before_touching_extras() -> None:
    # The oracle's early return: empty text yields no spans even with extras/custom supplied.
    assert (
        merge_text_redact_spans("", None, rust_candidates=[], extra_spans=[Span("x", 0, 0, "")])
        == []
    )


# ---------------------------------------------------------------------------
# 7.6 Integration + fallback (3 modes) + catalog skew + catalog agreement.
# ---------------------------------------------------------------------------


@NEEDS_TR_KERNEL
def test_the_wrapper_actually_calls_rust_on_eligible_cells() -> None:
    """Poison a supported detector's Python regex: an eligible cell still redacts it (Rust supplied
    the span), while the pure-Python oracle under the same poison would not. An ineligible cell goes
    the other way (Python, so it sees the poison), proving the per-cell routing split."""
    dead = re.compile(r"(?!x)x")  # matches nothing
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(storm_detectors._SPAN_DETECTORS, "email", (dead, None))
        eligible = ["mail a@b.com now"]
        assert _native(eligible, detectors=("email",), token="T", label_token=False) == [
            "mail T now"
        ]
        # The oracle sees the poisoned regex, so it would NOT redact -- that is the contrast.
        assert oracle_text_redact(eligible, detectors=("email",)) == ["mail a@b.com now"]
        # An ineligible cell runs the (poisoned) Python path, so it matches the poisoned oracle.
        ineligible = ["café a@b.com now"]
        assert _native(ineligible, detectors=("email",), token="T", label_token=False) == [
            "café a@b.com now"
        ]


def _install_fake_kernel(mp: pytest.MonkeyPatch, **attrs: Any) -> None:
    fake_pkg = types.ModuleType("decoy_engine_native")
    fake_kernel = types.ModuleType("decoy_engine_native._kernel")
    for name, value in attrs.items():
        setattr(fake_kernel, name, value)
    fake_pkg._kernel = fake_kernel  # type: ignore[attr-defined]
    mp.setitem(sys.modules, "decoy_engine_native", fake_pkg)
    mp.setitem(sys.modules, "decoy_engine_native._kernel", fake_kernel)


def test_fallback_companion_absent_runs_full_python() -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "decoy_engine_native", None)
        assert load_text_redact_kernel() is None
        got = _native(list(ELIGIBLE_TEXTS), detectors=None, token="T", label_token=True)
    assert got == oracle_text_redact(
        list(ELIGIBLE_TEXTS), detectors=None, token="T", label_token=True
    )


def test_fallback_symbol_absent_runs_full_python() -> None:
    # An older companion with the ABI but no span symbol: the loader must return None.
    with pytest.MonkeyPatch.context() as mp:
        _install_fake_kernel(mp, abi_version=lambda: "decoy-native-abi-2")
        assert load_text_redact_kernel() is None
        got = _native(list(ELIGIBLE_TEXTS), detectors=None, token="T", label_token=False)
    assert got == oracle_text_redact(list(ELIGIBLE_TEXTS), detectors=None, token="T")


def test_fallback_catalog_version_skew_runs_full_python() -> None:
    with pytest.MonkeyPatch.context() as mp:
        _install_fake_kernel(
            mp,
            text_redact_candidates=lambda *a, **k: pytest.fail("skewed catalog must not be called"),
            text_redact_catalog_version=lambda: CATALOG_VERSION + 1,
            text_redact_supported_ids=lambda: list(SUPPORTED_IDS),
        )
        assert load_text_redact_kernel() is None
        got = _native(list(ELIGIBLE_TEXTS), detectors=None, token="T", label_token=False)
    assert got == oracle_text_redact(list(ELIGIBLE_TEXTS), detectors=None, token="T")


def test_fallback_supported_set_skew_runs_full_python() -> None:
    with pytest.MonkeyPatch.context() as mp:
        _install_fake_kernel(
            mp,
            text_redact_candidates=lambda *a, **k: pytest.fail("skewed catalog must not be called"),
            text_redact_catalog_version=lambda: CATALOG_VERSION,
            text_redact_supported_ids=lambda: ["email"],  # wrong set
        )
        assert load_text_redact_kernel() is None


@NEEDS_TR_KERNEL
def test_the_real_kernel_raises_on_a_catalog_version_mismatch() -> None:
    from decoy_engine_native import _kernel as compiled

    with pytest.raises(ValueError, match="catalog_version_mismatch"):
        compiled.text_redact_candidates(["a@b.com"], list(SUPPORTED_IDS), CATALOG_VERSION + 1)


@NEEDS_TR_KERNEL
def test_the_python_and_rust_catalogs_agree() -> None:
    from decoy_engine_native import _kernel as compiled

    assert tuple(compiled.text_redact_supported_ids()) == SUPPORTED_IDS
    assert compiled.text_redact_catalog_version() == CATALOG_VERSION
    # The label column equals the id column (the oracle's `_splice` reads `Span.detector_id`).
    assert {det_id: det_id for det_id in SUPPORTED_IDS} == CATALOG_LABELS


def test_empty_selection_redacts_nothing_but_all_selection_redacts() -> None:
    # The resolver maps empty -> None (all) before the kernel; an empty tuple reaching the wrapper
    # directly runs zero detectors, as iter_spans([]) does. (Runs on either path.)
    arr = ["mail a@b.com now"]
    assert _native(arr, detectors=(), token="T", label_token=False) == arr
    assert _native(arr, detectors=None, token="T", label_token=False) == ["mail T now"]


def test_requested_rust_ids_filters_dedupes_and_expands_none() -> None:
    assert requested_rust_ids(None) == list(SUPPORTED_IDS)
    assert requested_rust_ids(["ssn", "email", "us_zip", "ipv4"]) == ["email", "ipv4"]
    assert requested_rust_ids(["email", "email", "ssn"]) == ["email"]
    assert requested_rust_ids(["no_such"]) == []


def test_corpus_round_trips_identically_on_whichever_path_is_active() -> None:
    # Whatever the substrate (companion present or not), the wrapper equals the oracle on the full
    # corpus -- the one invariant that holds in every environment.
    got = _native(list(CORPUS), detectors=None, token="<X>", label_token=True)
    assert got == oracle_text_redact(list(CORPUS), detectors=None, token="<X>", label_token=True)
