"""A6: rule M3 gives the same text on every supported interpreter (run under 3.10 and 3.12).

Plain NFC of ``a U+0316 U+1AC1`` differs between Unicode 13.0 (Python 3.10) and 14.0/15.0
(3.11, 3.12) because U+1AC1 is unassigned in 13.0. The frozen post-13.0 table closes that:
a canary holding a table code point is rejected and a cell holding one is not normalized.
"""

from __future__ import annotations

import ast
import hashlib
import json
import unicodedata
from pathlib import Path

from decoy_engine.verification.canary import (
    CanaryBindingSource,
    CanaryObservedReason,
    CanaryPolicyInputError,
    CanaryReason,
    CanaryStatus,
    CanaryVerdictPolicy,
)
from decoy_engine.verification.canary._match import (
    CellOutcome,
    build_patterns,
    classify_cell,
    screen_canaries,
)
from decoy_engine.verification.canary._normalize import (
    POST_13_0_NORMALIZATION_SENSITIVE,
    WHITE_SPACE,
    normalize_m3,
)

REPO = Path(__file__).parents[4]
GOLDEN = REPO / "tests" / "fixtures" / "f34" / "canary-span-normalization-v1-golden.json"
NORMALIZE_SRC = REPO / "src" / "decoy_engine" / "verification" / "canary" / "_normalize.py"

# Interpreter-divergent: U+1AC1 is unassigned (ccc 0) in Unicode 13.0 and ccc 230 from 14.0, so
# plain NFC reorders the marks on 3.11 and 3.12 and not on 3.10.
SEED = "a\u1ac1\u0316"
# Marks already in canonical order: equal plain NFC everywhere, still in the table.
SEED_CANONICAL_ORDER = "a\u0316\u1ac1"
# Output of the generation script in docs/records/2026-11-02-f34-e1-build.md section 2:
# sha256 of the comma-joined, sorted, 4-plus-digit uppercase hex code points.
TABLE_SIZE = 171
TABLE_SHA256 = "97026761caf0ee9200ab30987f6970957d7e43fa8a4856aa52c18e7d2b9e955a"
PLAIN_NFC_OF_SEED = {
    "13.0.0": "a\u1ac1\u0316",
    "14.0.0": "a\u0316\u1ac1",
    "15.0.0": "a\u0316\u1ac1",
}


def test_post_13_0_marks_reject_canary_uncompare_cell() -> None:
    # Non-vacuity: the seed is the interpreter-dependent case on this interpreter's UCD.
    assert unicodedata.normalize("NFC", SEED) == PLAIN_NFC_OF_SEED[unicodedata.unidata_version]
    assert 0x1AC1 in POST_13_0_NORMALIZATION_SENSITIVE
    assert unicodedata.normalize("NFC", SEED_CANONICAL_ORDER) == SEED_CANONICAL_ORDER
    assert normalize_m3(SEED) is None
    assert normalize_m3(SEED_CANONICAL_ORDER) is None

    assert screen_canaries(("zzzz" + SEED + "zzzz",)) is False
    assert screen_canaries(("zzzzzzzz",)) is True
    assert screen_canaries(("zzzz" + SEED_CANONICAL_ORDER + "zzzz",)) is False
    try:
        build_patterns(("zzzz" + SEED + "zzzz",), 8)
    except CanaryPolicyInputError:
        pass
    else:
        raise AssertionError("a canary holding a post-13.0 mark was accepted")

    patterns = build_patterns(("zzzzzzzz", "secretval"), 8)
    assert classify_cell(patterns, "zzzz" + SEED) is CellOutcome.UNCOMPARED
    assert classify_cell(patterns, "zzzz" + SEED_CANONICAL_ORDER) is CellOutcome.UNCOMPARED
    # An exact mode still applies to a cell holding a table code point.
    assert classify_cell(patterns, "secretval" + SEED) is CellOutcome.EXACT_SPAN
    assert classify_cell(patterns, "zzzzzzzz") is CellOutcome.EXACT_CELL

    observed = frozenset({CanaryObservedReason.CELL_UNCOMPARED})
    kw = {
        "validation_failed": False,
        "binding_source": CanaryBindingSource.PUBLICATION_RECORD,
        "observed": observed,
    }
    assert CanaryVerdictPolicy.status(**kw) is CanaryStatus.INCOMPLETE
    reasons = CanaryVerdictPolicy.reasons(**kw, has_findings=False, has_short_excluded=False)
    assert reasons == (CanaryReason.SCAN_INCOMPLETE, CanaryReason.CELL_UNCOMPARED)


def test_normalization_golden_on_this_interpreter() -> None:
    doc = json.loads(GOLDEN.read_text(encoding="utf-8"))["unicode"]
    assert doc["contract"] == "canary-span-normalization/v1"
    rows = doc["rows"]
    assert len(rows) == 48
    assert sum(r["expected"] is None for r in rows) == 4
    got = {r["id"]: normalize_m3(r["input"]) for r in rows}
    assert got == {r["id"]: r["expected"] for r in rows}


def test_white_space_is_the_pinned_uax44_set() -> None:
    expected = {0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0x85, 0xA0, 0x1680}
    expected |= set(range(0x2000, 0x200B)) | {0x2028, 0x2029, 0x202F, 0x205F, 0x3000}
    assert type(WHITE_SPACE) is frozenset
    assert frozenset(expected) == WHITE_SPACE
    assert len(WHITE_SPACE) == 25


def test_table_is_frozen_literal() -> None:
    table = POST_13_0_NORMALIZATION_SENSITIVE
    assert type(table) is frozenset
    assert len(table) == TABLE_SIZE
    text = ",".join(f"{cp:04X}" for cp in sorted(table))
    assert hashlib.sha256(text.encode("ascii")).hexdigest() == TABLE_SHA256
    # The source binds the name to frozenset({<int literals>}), never to a computed value.
    tree = ast.parse(NORMALIZE_SRC.read_text(encoding="utf-8"))
    values = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign | ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "POST_13_0_NORMALIZATION_SENSITIVE"
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    ]
    assert len(values) == 1
    call = values[0]
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Name)
    assert call.func.id == "frozenset"
    assert len(call.args) == 1
    assert isinstance(call.args[0], ast.Set)
    elts = call.args[0].elts
    assert all(isinstance(e, ast.Constant) and type(e.value) is int for e in elts)
    assert len(elts) == TABLE_SIZE
