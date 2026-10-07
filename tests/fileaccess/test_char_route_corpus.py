"""E1-0 characterization: the current-main route corpus matches its golden record.

The golden was captured on the E1 base. Regenerate only on purpose:
`FILEACCESS_WRITE_GOLDENS=1 pytest tests/fileaccess/test_char_route_corpus.py`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.fileaccess import route_corpus as rc

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.mark.parametrize("name", sorted(rc.JOBS))
def test_route_corpus_job_matches_golden(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = rc.JOBS[name](tmp_path, monkeypatch, rc.default_ctx())
    record = json.loads(json.dumps(record, sort_keys=True, default=repr))
    if os.environ.get("FILEACCESS_WRITE_GOLDENS") == "1":
        rc.write_golden(name, record)
        return
    assert rc.load_golden().get(name) == record
