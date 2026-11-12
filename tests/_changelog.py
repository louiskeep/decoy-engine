"""Read the unreleased CHANGELOG text, wherever it lives.

New text goes into one `changelog.d/<slice-slug>.md` fragment per change and is folded into
CHANGELOG.md at release prep, so a test that checks unreleased wording reads it through here and
sees the `[Unreleased]` section and every fragment.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_README = "README.md"


def unreleased_text(root: Path = ROOT) -> str:
    """The `[Unreleased]` section of CHANGELOG.md plus every fragment, fragments in name order."""
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    start = text.index("## [Unreleased]")
    nxt = re.search(r"^## \[", text[start + 5 :], re.M)
    parts = [text[start : start + 5 + nxt.start()] if nxt else text[start:]]
    folder = root / "changelog.d"
    if folder.is_dir():
        parts += [
            p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.md")) if p.name != _README
        ]
    return "\n".join(parts)
