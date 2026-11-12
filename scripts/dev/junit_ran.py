"""Exit 0 only when the JUnit file(s) show a suite that really ran: passed > 0 and nothing skipped,
errored or failed. A skip is a failure here because a lane that skips itself proves nothing
(dp-certified-gate, determinism). Counting lives in `_gates_report.junit_totals`; this file holds
only the verdict. Stdlib only. Exit 1: not clean, missing file or bad XML. Exit 2: usage.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # the sibling module, also run by path
from _gates_report import junit_totals


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: junit_ran.py JUNIT_XML [JUNIT_XML ...]", file=sys.stderr)
        return 2
    try:
        t = junit_totals([Path(a) for a in argv])
    except (OSError, ET.ParseError):
        print("junit_ran: a JUnit file is missing or unreadable", file=sys.stderr)
        return 1
    counts = f"passed={t.passed} failures={t.failures} errors={t.errors} skipped={t.skipped}"
    if t.passed > 0 and t.skipped == 0 and t.errors == 0 and t.failures == 0:
        print(counts)
        return 0
    print(
        f"junit_ran: the suite did not run clean ({counts}); need passed>0 and the rest 0",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
