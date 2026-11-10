"""A default export of an evidence record carries no free text; keep_paths copies it as is."""

from __future__ import annotations

import copy
from typing import Any

from decoy_engine import records
from tests.unit.records._helpers import PRODUCER, T0, corpus, make, raw_of

EMAIL = "jane.doe@example.com"
CARD = "4111111111111111"


def _evidence(**edit: Any) -> dict[str, Any]:
    payload = copy.deepcopy(corpus("evidence-cli-local-1.json"))
    payload.update(edit)
    return raw_of(make("evidence", payload, labels={}))


def _export(rec: dict[str, Any], **kw: Any) -> tuple[Any, list[Any], str]:
    lines = list(records.export_lines([rec], producer=PRODUCER, exported_at=T0, **kw))
    items = list(records.read_export(lines))
    return items[0], items[1:], b"".join(lines).decode()


def test_warning_text_does_not_leave_in_a_default_export() -> None:
    warnings = [
        f"column holds {EMAIL}",
        {"code": "low_distinct_ratio", "provider": None, "column": "email",
         "detail": {"example": EMAIL, "card": CARD}},
    ]  # fmt: skip
    rec = _evidence(warnings=warnings)
    header, items, text = _export(rec)
    assert EMAIL not in text and CARD not in text
    kept = items[0].record.raw["payload"]["warnings"]
    assert kept == [
        {"code": "low_distinct_ratio", "provider": None, "column": "email", "detail": {}}
    ]
    assert header.withheld_records == () and header.record_count == 1


def test_keep_paths_copies_the_warnings_unchanged() -> None:
    rec = _evidence(warnings=[f"column holds {EMAIL}"])
    _, items, text = _export(rec, keep_paths=True)
    assert EMAIL in text and records.canonical_bytes(
        items[0].record.raw
    ) == records.canonical_bytes(rec)


def test_a_layout_that_is_not_structural_withholds_the_record() -> None:
    payload = copy.deepcopy(corpus("evidence-cli-local-1.json"))
    payload["output_fingerprints"]["ledger"]["layout"]["note"] = CARD
    rec = raw_of(make("evidence", payload, labels={}))
    header, items, text = _export(rec)
    assert items == [] and header.withheld_records == (rec["id"],)
    assert CARD not in text


def test_a_layout_column_name_must_be_a_safe_name() -> None:
    payload = copy.deepcopy(corpus("evidence-cli-local-1.json"))
    payload["output_fingerprints"]["ledger"]["layout"]["columns"][0]["name"] = f"id {EMAIL} \\x00"
    rec = raw_of(make("evidence", payload, labels={}))
    header, items, _ = _export(rec)
    assert items == [] and header.withheld_records == (rec["id"],)


def test_the_corpus_evidence_layout_still_exports() -> None:
    header, items, _ = _export(_evidence())
    assert header.withheld_records == () and len(items) == 1
    layout = items[0].record.raw["payload"]["output_fingerprints"]["ledger"]["layout"]
    assert layout["columns"][0]["name"] == "id"
