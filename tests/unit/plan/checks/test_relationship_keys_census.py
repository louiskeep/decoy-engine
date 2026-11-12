"""The generated-parent rule leaves no refused config in shipped examples or current docs (AT-23b).

Four test fixture files declared a generated relationship parent whose key the plan cannot show
is unique. They moved to provable keys with the rule; the suites that build their configs are the
red-before, green-after evidence. This file pins the rest of the census: the files exist under
the names the census lists, and no shipped example or docs snippet declares a refused parent.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from decoy_engine.plan.checks._relationship_keys import check_generated_parent_keys

ROOT = Path(__file__).resolve().parents[4]

FIXTURE_CENSUS = (
    "tests/parity/_f0_fixtures.py",
    "tests/parity/test_phase_f_f2_mixed_parity.py",
    "tests/parity/test_phase_f_f2_mixed_sweep.py",
    "tests/parity/test_phase_f_f2_mixed_admission.py",
)

_FENCE = re.compile(r"```ya?ml\n(.*?)```", re.DOTALL)


def _docs_configs() -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for path in sorted((ROOT / "docs").rglob("*.md")):
        for block in _FENCE.findall(path.read_text(encoding="utf-8")):
            if "relationships:" not in block or "generate_columns" not in block:
                continue
            try:
                loaded = yaml.safe_load(block)
            except yaml.YAMLError:
                continue
            if isinstance(loaded, dict):
                found.append((str(path.relative_to(ROOT)), loaded))
    return found


def test_the_census_names_four_existing_files() -> None:
    assert len(set(FIXTURE_CENSUS)) == 4
    for name in FIXTURE_CENSUS:
        assert (ROOT / name).is_file(), name


@pytest.mark.parametrize(
    "example", sorted((ROOT / "examples").glob("*.yaml")), ids=lambda p: p.name
)
def test_shipped_example_configs_have_no_refused_parent(example: Path) -> None:
    loaded = yaml.safe_load(example.read_text(encoding="utf-8"))
    check_generated_parent_keys(loaded)


def test_current_docs_snippets_have_no_refused_parent() -> None:
    for _name, loaded in _docs_configs():
        check_generated_parent_keys(loaded)
