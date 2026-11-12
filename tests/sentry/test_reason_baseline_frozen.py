"""The F29 reason baseline is frozen across Wave B moves (H-1 plan 8.2, AT-1).

Class 10 (frozen contract drift): ``reason-baseline.json`` is append-only and no Wave B
slice edits it, so a move that regenerates it (for example with
``_reason_baseline.py --write --i-am-at-the-baseline-commit`` on a moved tree) is red here.
The digest is the file's sha256 on engine main.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

BASELINE = Path(__file__).resolve().parents[1] / "fixtures" / "f29" / "reason-baseline.json"
FROZEN_SHA256 = "8b3345c2d8e5eb3541cde2f238b7048d712e5fc3903806f7ec79642520fed118"


def is_frozen(path: Path) -> bool:
    return hashlib.sha256(path.read_bytes()).hexdigest() == FROZEN_SHA256


def test_reason_baseline_unchanged_by_moves():
    assert is_frozen(BASELINE)


def test_frozen_check_rejects_an_edited_copy(tmp_path):
    text = BASELINE.read_text(encoding="utf-8")
    code = json.loads(text)["identities"][0]["code"]
    copy = tmp_path / BASELINE.name
    copy.write_bytes(BASELINE.read_bytes())
    assert is_frozen(copy)
    copy.write_text(text.replace(f'"code": "{code}"', f'"code": "{code}x"', 1), encoding="utf-8")
    assert copy.read_text(encoding="utf-8") != text
    assert not is_frozen(copy)
