"""C6c-ii acceptance: the Rust span-detection kernel for text_redact (plan section 7).

Byte-identical to the C6c-i Python path is the bar. Every merged assertion compares the native
wrapper against `oracle_text_redact` (the literal `iter_spans` + `_splice` path); the raw-candidate
and validator tests compare the kernel against the Python detectors and validators directly. Tests
that need the new compiled symbol carry `@NEEDS_TR_KERNEL` and skip on an old/absent companion (the
companion-present CI job runs them); the merge-helper, routing-predicate-mirror and fallback tests
run on the Python path alone.
"""

from __future__ import annotations

import os
import random
import re
import statistics
import sys
import time
import types
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native import _kernels_scalar
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
    _COMPANION_PRESENT,
    CORPUS,
    DETECTOR_SELECTIONS,
    ELIGIBLE_TEXTS,
    INELIGIBLE_TEXTS,
    LOOKAROUND_DETECTORS,
    NEEDS_TR_KERNEL,
    RUST_DETECTORS,
    SHAPES,
    oracle_text_redact,
    python_raw_candidates,
)

# The eight original lookaround-free detectors (`regex` crate) vs the three lookaround ones
# (hand-written ASCII byte scanners; `fancy-regex` was tried and rejected on perf -- see the
# text_redact.rs module doc). The split is the index boundary in the Rust catalog.
LOOKAROUND_FREE_DETECTORS = tuple(d for d in RUST_DETECTORS if d not in LOOKAROUND_DETECTORS)


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


def _lookaround_adversarial_corpus(seed: int, n: int) -> list[str]:
    """ASCII-only cells stressing the three lookaround detectors' boundary semantics: SSA reserved
    blocks and optional hyphens (ssn), the non-word lookbehind/lookahead and decimal guard
    (us_zip), and the house-number + USPS-suffix + unit + trailing-punctuation anchor
    (street_address). Every cell is eligible, so the kernel must match Python span-for-span."""
    rng = random.Random(seed)
    ssn_ish = [
        "123-45-6789",
        "123456789",
        "000-12-3456",
        "666-12-3456",
        "912-34-5678",
        "123-00-4567",
        "123-45-0000",
        "078-05-1120",
        "1234567890",
        "12-345-6789",
    ]
    zip_ish = [
        "12345",
        "90210-1234",
        "12345mg",
        "12345.6",
        "x12345",
        "123456",
        "12345-678",
        "00000",
        "12345-67890",
        "a12345b",
        "12345,",
        " 12345 ",
        "12345-1234x",
    ]
    street_ish = [
        "12 Main Street",
        "742 Evergreen Terrace.",
        "1 A St",
        "100 oak AVE here",
        "350 Fifth Ave, Suite 200",
        "1600 Pennsylvania Ave NW",
        "42 2nd St Apt 3B",
        "no number Main Street",
        "12 Main",
        "5 Elm Rd; next",
        "9 Birch Ln.",
        "1234567 Long St",
        "10 Main Street#5",
        "7 A B C D E Blvd",
    ]
    punct = ["", " ", ",", ". ", "; ", ": ", "! ", "? ", ")", '"', "'", " - ", "/", "x", "0"]
    fillers = ["patient", "note", "seen", "on", "re", "visit", "and", "the", "id", "addr", "mrn"]
    pools = ssn_ish + zip_ish + street_ish + fillers
    out: list[str] = []
    for _ in range(n):
        parts: list[str] = []
        for _ in range(rng.randint(0, 7)):
            parts.append(rng.choice(pools))
            parts.append(rng.choice(punct))
        # Also emit raw random ASCII runs so the scanners meet shapes no fixture anticipated.
        if rng.random() < 0.3:
            alphabet = "0123456789- .,#/abcdefXYZ"
            parts.append("".join(rng.choice(alphabet) for _ in range(rng.randint(1, 25))))
        out.append("".join(parts))
    return out


@NEEDS_TR_KERNEL
def test_lookaround_raw_candidates_match_python_on_an_adversarial_corpus() -> None:
    """Byte-parity gate for the ported detectors: the kernel's raw per-detector candidate list for
    ssn/us_zip/street_address must equal Python `finditer`+validator span-for-span on a large
    randomized ASCII corpus, before any merge. A divergence here is the regression the port must
    not introduce."""
    kern = _kernel()
    index_of = {det_id: i for i, det_id in enumerate(SUPPORTED_IDS)}
    corpus = _lookaround_adversarial_corpus(seed=20261022, n=4000)
    # Every generated cell must be eligible (ASCII, no 0x1c-0x1f); otherwise the kernel returns
    # None and the comparison would be vacuous.
    for text in corpus:
        assert is_ascii_safe(text), repr(text)
    per_cell_all = kern.text_redact_candidates(corpus, list(SUPPORTED_IDS), CATALOG_VERSION)
    for text, per_cell in zip(corpus, per_cell_all, strict=True):
        assert per_cell is not None, repr(text)
        for det_id in LOOKAROUND_DETECTORS:
            rust = [(s, e) for (idx, s, e) in per_cell if idx == index_of[det_id]]
            assert rust == python_raw_candidates(text, det_id), (det_id, repr(text))


@NEEDS_TR_KERNEL
def test_lookaround_merged_output_matches_oracle_on_the_adversarial_corpus() -> None:
    """End-to-end: the native wrapper's spliced output equals the oracle on the same adversarial
    corpus, for the lookaround-only selection and for the all-detector selection, labeled and not."""
    corpus = _lookaround_adversarial_corpus(seed=7, n=3000)
    for detectors in (LOOKAROUND_DETECTORS, None):
        for label in (False, True):
            got = _native(corpus, detectors=detectors, token="<X>", label_token=label)
            assert got == oracle_text_redact(
                corpus, detectors=detectors, token="<X>", label_token=label
            )


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


def test_the_lookaround_free_patterns_use_only_d_s_and_ascii_case_fold() -> None:
    # The eight lookaround-free detectors use neither \w/\b nor any lookaround, so their
    # class-equivalence proof needs only \d, \s and ASCII case folding.
    for det_id in LOOKAROUND_FREE_DETECTORS:
        pattern = storm_detectors._SPAN_DETECTORS[det_id][0].pattern
        assert r"\w" not in pattern, det_id
        assert r"\b" not in pattern, det_id
        for lookaround in (r"(?=", r"(?!", r"(?<=", r"(?<!"):
            assert lookaround not in pattern, (det_id, lookaround)


def test_the_lookaround_patterns_use_only_classes_whose_ascii_membership_is_proven() -> None:
    # The three ported detectors add lookaround and the \w/\b constructs. Their byte parity rests
    # on (a) the ASCII membership of \d, \s and \w (proven equal to Python's by the class test
    # below), (b) \b being derived purely from \w, and (c) the eligibility predicate excluding the
    # one \s divergence (0x1c-0x1f). Assert they introduce no OTHER class escape that would need
    # its own proof (e.g. \D, \S, \W, a Unicode property \p{...}).
    # \d/\s/\w are the classes; \b is derived from \w; the rest are escaped literals (dot,
    # hyphen, quote, backslash) that carry no Unicode-vs-ASCII ambiguity.
    allowed_escapes = {r"\d", r"\s", r"\w", r"\b", r"\.", r"\-", r"\\", '\\"'}
    for det_id in LOOKAROUND_DETECTORS:
        pattern = storm_detectors._SPAN_DETECTORS[det_id][0].pattern
        escapes = {pattern[i : i + 2] for i in range(len(pattern) - 1) if pattern[i] == "\\"}
        unexpected = escapes - allowed_escapes
        assert not unexpected, (det_id, unexpected)


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
        _install_fake_kernel(mp, abi_version=lambda: "decoy-native-abi-3")
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
    # ssn/us_zip are supported now, so they survive the filter in requested order; only ids
    # outside the catalog (and duplicates) are dropped.
    assert requested_rust_ids(["ssn", "email", "us_zip", "ipv4"]) == [
        "ssn",
        "email",
        "us_zip",
        "ipv4",
    ]
    assert requested_rust_ids(["email", "email", "ssn"]) == ["email", "ssn"]
    assert requested_rust_ids(["person_name", "mrn", "email"]) == ["email"]  # unsupported dropped
    assert requested_rust_ids(["no_such"]) == []


@pytest.mark.perf
@NEEDS_TR_KERNEL
def test_representative_workload_uses_the_rust_path_not_a_silent_python_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loose CI guard (plan 7.7): on an ASCII-dominant representative workload the native path must
    be materially faster than a forced-Python run. A silent drop back to Python gives ratio ~1.0
    and fails this, well below the measured 2.1x so it is not flaky."""
    rng = random.Random(7)
    pii = ["a@b.com", "(555) 123-4567", "123-45-6789", "4111 1111 1111 1111", "10.0.0.1", "E11.9"]
    fillers = ["patient", "seen", "today", "note", "with", "on", "and", "stable", "visit"]
    cells = [
        " ".join(
            rng.choice(pii if rng.random() < 0.2 else fillers) for _ in range(rng.randint(3, 30))
        )
        for _ in range(15_000)
    ]
    arr = pa.array(cells, pa.string())

    def median(reps: int = 5) -> float:
        native_text_redact(arr, detectors=None, token="[R]", label_token=False)  # warmup
        samples = []
        for _ in range(reps):
            t = time.perf_counter()
            native_text_redact(arr, detectors=None, token="[R]", label_token=False)
            samples.append(time.perf_counter() - t)
        return statistics.median(samples)

    new = median()
    with monkeypatch.context() as mp:
        mp.setattr(_kernels_scalar, "load_text_redact_kernel", lambda: None)
        base = median()
    assert new <= 0.9 * base, (
        f"native text_redact path not materially faster ({new:.3f}s vs forced-Python {base:.3f}s); "
        "has it silently dropped back to the Python path on the default ASCII-dominant workload?"
    )


def test_corpus_round_trips_identically_on_whichever_path_is_active() -> None:
    # Whatever the substrate (companion present or not), the wrapper equals the oracle on the full
    # corpus -- the one invariant that holds in every environment.
    got = _native(list(CORPUS), detectors=None, token="<X>", label_token=True)
    assert got == oracle_text_redact(list(CORPUS), detectors=None, token="<X>", label_token=True)


# ---------------------------------------------------------------------------
# 7.7 Companion-present preflight (plan section 9): prove the correctness gate RAN, not skipped.
# ---------------------------------------------------------------------------

# Every @NEEDS_TR_KERNEL test above (raw differential, merged differential, golden clinical-notes,
# routing, catalog agreement, vocabulary agreement) skips when `load_text_redact_kernel()` returns
# None -- which happens not only when the companion is absent but also on a silent catalog skew or
# an old companion without the span symbol. So the whole byte-parity gate can SKIP while the
# ordinary fallback tests pass, giving false confidence. This preflight shares that exact load
# condition, so a loaded kernel here means none of those tests skipped.
_REQUIRE_TR_KERNEL = os.environ.get("DECOY_REQUIRE_TR_KERNEL") == "1"

# The plan's frozen catalog surface (CATALOG_VERSION 1->2; SUPPORTED_IDS 8->11, appended so the
# original eight keep their indices). Pinned as literals so the preflight catches a one-sided drift
# in the Python-side constants too, not only Python<->Rust disagreement.
_EXPECTED_CATALOG_VERSION = 2
_EXPECTED_SUPPORTED_IDS = (
    "email",
    "us_phone",
    "pan",
    "iban",
    "ipv4",
    "icd10",
    "npi",
    "url",
    "ssn",
    "us_zip",
    "street_address",
)


def test_preflight_companion_present_kernel_loads_with_expected_catalog() -> None:
    """Acceptance preflight: assert the span kernel actually loaded, so the NEEDS_TR_KERNEL gate
    above RAN and did not silently skip (plan section 9).

    With DECOY_REQUIRE_TR_KERNEL=1 (the acceptance run), an absent/old/skewed companion is a
    FAILURE, not a skip. Without it, the preflight skips only when the companion is genuinely
    absent (the companion-absent regression-gate run), but still FAILS if the companion package is
    importable yet the kernel will not load -- the catalog-skew trap, which would otherwise make
    every NEEDS_TR_KERNEL test skip while ordinary fallback tests stay green.
    """
    kernel = load_text_redact_kernel()
    if kernel is None:
        if _REQUIRE_TR_KERNEL:
            pytest.fail(
                "DECOY_REQUIRE_TR_KERNEL=1 but the text_redact span kernel did not load "
                "(companion absent, old symbol, or catalog skew): the byte-parity acceptance "
                "gate did NOT run -- build the companion (`maturin develop --release`) before "
                "the acceptance run."
            )
        if not _COMPANION_PRESENT:
            pytest.skip("companion absent; the byte-parity gate's fallback runs on the Python path")
        pytest.fail(
            "the decoy_engine_native package is importable but the text_redact span kernel did "
            "not load (old symbol or catalog skew): every @NEEDS_TR_KERNEL test would silently "
            "skip. Rebuild the companion and reconcile CATALOG_VERSION / SUPPORTED_IDS."
        )
    # Kernel loaded: pin both the Python-side catalog and the compiled catalog to the plan's values.
    from decoy_engine_native import _kernel as compiled

    assert SUPPORTED_IDS == _EXPECTED_SUPPORTED_IDS
    assert CATALOG_VERSION == _EXPECTED_CATALOG_VERSION
    assert tuple(compiled.text_redact_supported_ids()) == _EXPECTED_SUPPORTED_IDS
    assert compiled.text_redact_catalog_version() == _EXPECTED_CATALOG_VERSION


# ---------------------------------------------------------------------------
# 7.8 Vocabulary + pattern agreement (plan section 7): one-sided drift fails CI.
# ---------------------------------------------------------------------------

# The Rust kernel hand-copies the oracle's detection surface: the eight lookaround-free detectors as
# ASCII-restricted `regex`-crate translations of their Python bodies, and ssn/us_zip/street_address
# as byte scanners replaying their Python bodies (including the 47-entry USPS suffix vocabulary and
# the secondary-unit designators embedded in `_STREET_ADDRESS_RE`). No existing test fails when a
# pattern body, a flag, or a vocabulary entry is edited on only ONE side: the catalog-version /
# supported-set checks and the ASCII-class test do not cover the bodies or the vocabularies, and the
# random differential corpus draws mostly from fixed examples. So pin the oracle contract here.
#
# These frozen literals ARE the scanner contract. They were copied from the live `_SPAN_DETECTORS`
# at CATALOG_VERSION 2. A failure means an oracle pattern/flag drifted from what the Rust kernel was
# built against; the fix is NOT to re-snapshot in isolation but, in ONE change (plan section 6): (a)
# re-review the affected Rust scanner/translation in text_redact.rs, (b) extend the differential
# corpus to cover the change, (c) bump CATALOG_VERSION in BOTH text_redact.rs and
# _text_redact_kernel.py, and (d) update this snapshot.
_U = re.UNICODE
_UI = re.UNICODE | re.IGNORECASE
_FROZEN_ORACLE_PATTERNS: dict[str, tuple[str, int]] = {
    "email": ("[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}", _U),
    "ssn": ("(?!000|666|9\\d{2})\\d{3}-?(?!00)\\d{2}-?(?!0000)\\d{4}", _U),
    "us_phone": ("(?:\\+?1[\\s.-]?)?\\(?[2-9]\\d{2}\\)?[\\s.-]?[2-9]\\d{2}[\\s.-]?\\d{4}", _U),
    "us_zip": ("(?<!\\w)\\d{5}(?:-\\d{4})?(?!\\w)(?!\\.\\d)", _U),
    "pan": ("\\d{4}[\\s-]?\\d{4}[\\s-]?\\d{4}[\\s-]?\\d{1,7}", _U),
    "iban": ("[A-Z]{2}\\d{2}[\\sA-Z0-9]{11,34}", _U),
    "ipv4": ("(?:\\d{1,3}\\.){3}\\d{1,3}", _U),
    "icd10": ("[A-Z]\\d{2}(?:\\.?[A-Z0-9]{1,4})?", _UI),
    "npi": ("\\d{10}", _U),
    "url": ("https?://[A-Za-z0-9\\-._~:/?#\\[\\]@!$&'()*+,;=%]{4,}", _U),
    "street_address": (
        "\\b\\d{1,6}\\s+(?:[A-Za-z0-9][A-Za-z0-9'.\\-]*\\s+){1,4}"
        "(?:street|st|avenue|ave|boulevard|blvd|road|rd|drive|dr|lane|ln|court|ct|circle|cir|"
        "place|pl|terrace|ter|trail|trl|parkway|pkwy|highway|hwy|way|plaza|plz|square|sq|"
        "expressway|expy|freeway|fwy|crossing|xing|loop|alley|aly|bend|cove|cv|point|pt|ridge|rdg)"
        "\\.?(?:\\s+(?:apt|apartment|suite|ste|unit|#)\\.?\\s*[\\w\\-]+)?(?=[\\s,.;:!?)\\\"']|$)",
        _UI,
    ),
}


def test_the_oracle_patterns_and_flags_match_the_pinned_scanner_contract() -> None:
    """Pin every detector's oracle regex BODY + FLAGS (runs without the companion -- the Python
    oracle is the source of truth). A one-sided edit to a boundary, quantifier, lookahead, or the
    IGNORECASE flag leaves CATALOG_VERSION and the supported-set unchanged, so only this snapshot
    catches it."""
    assert set(_FROZEN_ORACLE_PATTERNS) == set(SUPPORTED_IDS), (
        "a detector was added/removed from _SPAN_DETECTORS without updating the pinned scanner "
        "contract (and the Rust catalog + CATALOG_VERSION)"
    )
    live = {
        det_id: (rx.pattern, int(rx.flags))
        for det_id, (rx, _validator) in storm_detectors._SPAN_DETECTORS.items()
    }
    expected = {
        det_id: (pat, int(flags)) for det_id, (pat, flags) in _FROZEN_ORACLE_PATTERNS.items()
    }
    assert live == expected, (
        "a text_redact oracle pattern/flag drifted from the pinned scanner contract. The Rust "
        "kernel hand-copies these bodies+flags; fix in ONE change (plan section 6): re-review the "
        "affected Rust scanner/translation, extend the differential corpus, bump CATALOG_VERSION "
        "in BOTH text_redact.rs and _text_redact_kernel.py, and update this snapshot."
    )


@NEEDS_TR_KERNEL
def test_the_rust_street_vocabularies_reconstruct_the_oracle_pattern() -> None:
    """Expose the Rust suffix + unit vocabularies and tie them to the Python oracle (plan section
    7). List-equality alone is insufficient -- the unit vocabulary is embedded directly in
    `_STREET_ADDRESS_RE`, so also assert the Rust lists reconstruct the exact alternation substrings
    the oracle pattern is built from."""
    from decoy_engine_native import _kernel as compiled

    rust_suffixes = tuple(compiled.text_redact_street_suffixes())
    # (a) Ordered list-equality against the Python oracle's suffix vocabulary.
    assert rust_suffixes == storm_detectors._USPS_STREET_SUFFIXES
    street_pattern = storm_detectors._SPAN_DETECTORS["street_address"][0].pattern
    # (b) The suffix alternation the oracle builds is exactly join(rust suffixes) + the optional dot.
    assert "(?:" + "|".join(rust_suffixes) + r")\.?" in street_pattern
    # (c) The unit designators are embedded in the oracle body, not a standalone list, so tie the
    #     Rust unit vocabulary back to that body directly.
    rust_units = list(compiled.text_redact_street_units())
    assert "(?:" + "|".join(rust_units) + ")" in street_pattern


@NEEDS_TR_KERNEL
def test_every_street_suffix_alternative_matches_like_the_oracle() -> None:
    """Targeted differential (plan section 7): every USPS suffix, plain AND with the optional
    trailing dot, drives a real match that equals the oracle span-for-span -- the random corpus
    draws mostly from fixed examples, so each alternative is exercised explicitly here."""
    suffixes = storm_detectors._USPS_STREET_SUFFIXES
    cells: list[str] = []
    for suf in suffixes:
        cells.append(f"12 Main {suf} here")  # plain suffix
        cells.append(f"12 Main {suf}. here")  # optional-dot fallback
    got = _native(cells, detectors=("street_address",), token="<X>", label_token=False)
    exp = oracle_text_redact(cells, detectors=("street_address",), token="<X>", label_token=False)
    assert got == exp
    # Each plain-suffix frame must actually redact, so the suffix alternation is genuinely exercised
    # (not a vacuous no-match agreement).
    for suf in suffixes:
        plain = f"12 Main {suf} here"
        assert oracle_text_redact([plain], detectors=("street_address",), token="<X>") != [plain], (
            suf
        )


@NEEDS_TR_KERNEL
def test_every_street_unit_alternative_matches_like_the_oracle() -> None:
    """Targeted differential (plan section 7): every secondary-unit designator, plain AND with the
    optional dot, equals the oracle span-for-span."""
    from decoy_engine_native import _kernel as compiled

    units = list(compiled.text_redact_street_units())
    cells: list[str] = []
    for un in units:
        cells.append(f"12 Main Street {un} 5 here")  # unit present
        cells.append(f"12 Main Street {un}. 5 here")  # optional unit-dot fallback
    got = _native(cells, detectors=("street_address",), token="<X>", label_token=False)
    exp = oracle_text_redact(cells, detectors=("street_address",), token="<X>", label_token=False)
    assert got == exp


@NEEDS_TR_KERNEL
def test_street_name_repetition_backtracks_like_the_oracle() -> None:
    """Targeted differential (plan section 7): the greedy `(?:WORD\\s+){1,4}` street-name repetition
    must backtrack over the iteration count (most-first), then the suffix/dot/unit alternatives,
    exactly in the oracle's order -- including the exhaustion boundary where >4 interior words admit
    no match."""
    cells = [
        "5 Main Street Road here",  # greedy 3 iters fail suffix, backtrack to let 'Road' anchor
        "10 Oak Elm Pine Maple Street here",  # exactly 4 interior words + suffix (max iterations)
        "10 Oak Elm Pine Maple Birch Street here",  # 5 interior words > {1,4}: no match
        "7 A B C D E Blvd here",  # 5 interior words before the suffix: exhaustion, no match
        "350 Fifth Ave, Suite 200",  # suffix + unit + boundary punctuation
        "1600 Pennsylvania Ave NW",  # suffix then a trailing non-unit token
        "42 2nd St Apt 3B",  # ordinal street-name word + abbreviated suffix + unit
        "9 Birch Ln.",  # optional trailing dot at end of string
        "1 A St",  # single one-letter street-name word
    ]
    got = _native(cells, detectors=("street_address",), token="<X>", label_token=True)
    exp = oracle_text_redact(cells, detectors=("street_address",), token="<X>", label_token=True)
    assert got == exp
    # The fixture must exercise BOTH branches of the backtracking: at least one cell redacts and at
    # least one does not, per the oracle.
    assert any(o != c for o, c in zip(exp, cells, strict=True)), "no cell redacted; match untested"
    assert any(o == c for o, c in zip(exp, cells, strict=True)), "all redacted; exhaustion untested"
