"""Reason codes named in the QA checklist tables (`docs/qa/`) exist in `decoy_engine.reasons`.

The checklist states expected reason codes by hand, so it can drift from the registry (it
once named `derived_cyclic_ref` for `derived_cyclic_reference`). The convention in
`docs/qa/README.md`: a table names a reason as `namespace.code` in backticks. This sentry
checks three things in every check row:

- a backticked `namespace.code`, with `namespace` a registry namespace, is registered;
- a bare backticked token that is a registered code is an error (qualify it), so codes
  cannot slip in unchecked;
- the argument of a backticked `SomethingError(x)` is qualified, so a misspelled bare
  code in that position cannot pass as prose.
"""

from __future__ import annotations

import re
from pathlib import Path

from decoy_engine import reasons

ROOT = Path(__file__).resolve().parents[2]
QA_DIR = ROOT / "docs" / "qa"

ROW = re.compile(r"^\| ([A-Z]+-\d+) \|")
SPAN = re.compile(r"`([^`\n]+)`")
ERROR_CALL = re.compile(r"\b[A-Z][A-Za-z]*Error\(([a-z][a-z0-9_.]*)\)")

# Codes a check expects that an open engine branch registers. An entry must not be
# registered yet: once its branch merges, `test_pending_codes_are_not_yet_registered`
# fails and the entry comes out.
PENDING: dict[tuple[str, str], str] = {
    ("runtime", "fpe_unencryptable_value"): "fix/fpe-unregistered-reason",
}

NAMESPACES = frozenset(ns for ns, _ in reasons.REASONS)
BARE_CODES = frozenset(code for _, code in reasons.REASONS)


def problems_in(text: str, where: str) -> list[str]:
    """Every reason-code problem in the check rows of one checklist file."""
    found: list[str] = []
    for line in text.splitlines():
        row = ROW.match(line)
        if not row:
            continue
        check = row.group(1)
        for span in SPAN.findall(line):
            tokens = [span.strip()]
            for arg in ERROR_CALL.findall(span):
                tokens.append(arg)
                if "." not in arg:
                    found.append(f"{where} {check}: `{span}` names an unqualified code")
            for token in tokens:
                ns, dot, code = token.partition(".")
                if dot and ns in NAMESPACES:
                    if (ns, code) not in reasons.REASONS and (ns, code) not in PENDING:
                        found.append(f"{where} {check}: `{token}` is not a registered reason")
                elif token in BARE_CODES:
                    found.append(f"{where} {check}: `{token}` is a reason code; write ns.code")
    return found


def checklist_files() -> list[Path]:
    return sorted(QA_DIR.glob("[0-9][0-9]-*.md"))


def test_the_checklist_files_exist() -> None:
    assert len(checklist_files()) >= 18


def test_every_checklist_reason_code_is_registered() -> None:
    found = [
        p
        for path in checklist_files()
        for p in problems_in(path.read_text(encoding="utf-8"), path.name)
    ]
    assert not found, "\n".join(found)


def test_pending_codes_are_not_yet_registered() -> None:
    landed = sorted(f"{ns}.{code}" for ns, code in PENDING if (ns, code) in reasons.REASONS)
    assert not landed, f"registered now; remove from PENDING: {landed}"


def test_the_checks_catch_each_drift() -> None:
    def row(expect: str) -> str:
        return f"| X-01 | run it | {expect} |\n"

    assert problems_in(row("Refused with `plan.derived_cyclic_reference`."), "f") == []
    assert problems_in(row("`config.source_file.has_header_required`"), "f") == []
    assert problems_in(row("Uses `date_shift` on `first_name`."), "f") == []
    assert problems_in(row("Fails with `runtime.fpe_unencryptable_value`."), "f") == []
    # A misspelled qualified code.
    assert problems_in(row("`plan.derived_cyclic_ref`"), "f")
    # A registered code written bare.
    assert problems_in(row("Refused with `derived_cyclic_reference`."), "f")
    # A misspelled bare code inside an error call.
    assert problems_in(row("`PlanCompileError(derived_cyclic_ref)`"), "f")
    # A string argument is a message, not a code.
    assert problems_in(row('`ValueError("boom")`'), "f") == []
    # Prose outside a check row is not read.
    assert problems_in("Text naming `plan.not_a_code`.\n", "f") == []
