"""The generated-parent rule leaves no refused config in shipped examples or current docs (AT-23b).

Five test fixture files declared a generated relationship parent whose key the plan cannot show
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
    "tests/parity/test_numeric_typing_readers.py",
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


def test_the_census_names_five_files_that_declare_a_provable_parent() -> None:
    # Whole-file token match: it shows the file still mentions a proof, not that the proof sits
    # on the generated parent; the suites that build these configs carry that evidence.
    assert len(set(FIXTURE_CENSUS)) == 5
    for name in FIXTURE_CENSUS:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert re.search(r'"unique":\s*True|"type":\s*"sequence"|unique_together', text), name


@pytest.mark.parametrize(
    "example", sorted((ROOT / "examples").glob("*.yaml")), ids=lambda p: p.name
)
def test_shipped_example_configs_have_no_refused_parent(example: Path) -> None:
    loaded = yaml.safe_load(example.read_text(encoding="utf-8"))
    check_generated_parent_keys(loaded)


def test_current_docs_snippets_have_no_refused_parent() -> None:
    # Guards snippets added later; the docs hold no generated-parent snippet today, so the loop
    # may run zero times.
    for _name, loaded in _docs_configs():
        check_generated_parent_keys(loaded)
