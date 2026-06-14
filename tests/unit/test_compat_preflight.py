"""Unit tests for the compat pre-flight gate logic (scripts/check_compat_preflight.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_compat_preflight.py"
_spec = importlib.util.spec_from_file_location("check_compat_preflight", _SCRIPT)
assert _spec and _spec.loader
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)

# A fully-ticked section-9 checklist.
_TICKED_CHECKLIST = """
## Pre-flight checklist
- [x] I read this document.
- [x] My change is additive, OR follows the section 5 decision.
- [x] No name/vN artifact shape changed under its existing tag.
- [x] No determinism golden baseline changed.
- [x] Vault read/write is untouched.
- [x] No released disguise version was edited in place.
- [x] Any CLI/API removal goes through a deprecation shim with a CHANGELOG entry.
- [x] The cross-version compatibility corpus still passes.
"""


def test_touched_frozen_detects_frozen_paths() -> None:
    changed = ["README.md", "src/decoy_engine/vault.py", "tests/unit/test_x.py"]
    assert preflight.touched_frozen(changed) == ["src/decoy_engine/vault.py"]


def test_touched_frozen_ignores_non_frozen() -> None:
    changed = ["README.md", "tests/unit/test_x.py", "docs/guide.md"]
    assert preflight.touched_frozen(changed) == []


def test_determinism_dir_is_frozen() -> None:
    assert preflight.touched_frozen(["src/decoy_engine/determinism/_derive.py"])


def test_checklist_satisfied_when_all_ticked() -> None:
    assert preflight.checklist_problems(_TICKED_CHECKLIST) == []


def test_checklist_empty_body_reports_all_items() -> None:
    problems = preflight.checklist_problems("")
    assert len(problems) == len(preflight.CHECKLIST_ITEMS)


def test_checklist_flags_unticked_item() -> None:
    # Same checklist but the vault line is left unchecked.
    body = _TICKED_CHECKLIST.replace("- [x] Vault read/write", "- [ ] Vault read/write")
    problems = preflight.checklist_problems(body)
    assert problems == ["vault read/write"]
