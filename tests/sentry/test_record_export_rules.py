"""Every writable record kind declares how its default export is proven value-free."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from decoy_engine import records
from decoy_engine.records import _kinds, _registry  # noqa: F401
from decoy_engine.records._registry import KindSpec

RECORDS = Path(records.__file__).parent


def test_every_writable_kind_declares_an_export_rule() -> None:
    writable = [s for s in records.registered_kinds() if not s.reserved]
    assert writable
    assert [s.kind for s in writable if s.export_rule is None] == []


def test_a_writable_kind_without_a_rule_cannot_be_registered() -> None:
    base = records.kind_spec("sample-run")
    assert base is not None
    with pytest.raises(ValueError):
        KindSpec(
            kind="x",
            payload_schema="x/v1",
            write_model=base.write_model,
            read_model=base.read_model,
            diff_rule="x",
            canary_fixture="x",
        )


def test_a_reserved_kind_declares_no_rule() -> None:
    with pytest.raises(ValueError):
        KindSpec(kind="x", payload_schema="x/v1", export_rule=lambda p: (p, False))


@pytest.mark.parametrize("grammar", [r"sha256:\[0-9a-f\]\{64\}", r"\[a-z\]\[a-z0-9-\]\{0,63\}"])
def test_each_wire_grammar_is_spelled_once(grammar: str) -> None:
    holders = sorted(
        p.name for p in RECORDS.glob("*.py") if re.search(grammar, p.read_text(encoding="utf-8"))
    )
    assert holders == ["_grammar.py"]
