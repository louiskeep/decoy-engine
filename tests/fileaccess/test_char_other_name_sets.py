"""E1-0 characterization: the isolated route's result-envelope read-back, pinned as it behaves.

The isolated route's staging and commit (`stage_tables`, `atomic_commit`, the sibling stage)
were replaced by F22's publication stage and `publish_unit` (plan 5.7); their name-set pins are
gone with them. The staged-name rule is the sink's one rule (a single path component, no `..`),
tested with the route in `tests/unit/execution/publication/test_isolated_parent_measures.py`.
"""

from __future__ import annotations

from pathlib import Path

from decoy_engine.execution import _isolated_commit as commit


def test_isolated_row_errors_and_envelope_read_back_degrade_to_empty(tmp_path: Path) -> None:
    assert commit.row_errors_of({}) == ()
    assert commit.row_errors_of({"row_errors": [{"unknown": 1}]}) == ()
    assert commit.read_envelope(tmp_path / "missing.json") is None
    (tmp_path / "env.json").write_text("{bad")
    assert commit.read_envelope(tmp_path / "env.json") is None
    (tmp_path / "list.json").write_text("[1]")
    assert commit.read_envelope(tmp_path / "list.json") is None
    (tmp_path / "ok.json").write_text('{"k": 1}')
    assert commit.read_envelope(tmp_path / "ok.json") == {"k": 1}
