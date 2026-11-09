"""Class 3 and class 6 (facade) sentries.

Class 3: ``raise X from None`` inside an ``except`` block hides the cause but keeps
``__context__`` (PEP 415), so it neither helps a reader nor makes a message value-free. The
repo-wide count is a shrink-only ratchet, one baseline key per site
(``path::qualified function::ordinal``). The bulk hoists are V2 corrective work; a new site
fails here, and a removed site must leave the baseline.

Class 6, public facades: ``decoy_engine.progress`` fronts ``_progress_events`` and
``decoy_engine.reasons`` fronts its ``_*`` modules. An import of the private module from
outside the facade fails. When the import-linter layer contract merges, ``protected`` stanzas
replace this exact AST check.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from tests.sentry.defects import _defect_scan as scan

BASELINE = Path(__file__).resolve().parents[1] / "baselines" / "exception-context.json"


def _baseline() -> dict[str, dict[str, str]]:
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert data["sentry_id"] == "exception-context"
    return data["entries"]  # type: ignore[no-any-return]


def _keys(source: str, rel: str = "decoy_engine/x.py") -> list[str]:
    return scan.from_none_tree_keys(ast.parse(source), rel)


def _tree(tmp_path: Path, files: dict[str, str]) -> scan.ModuleIndex:
    pkg = tmp_path / "decoy_engine"
    for name, text in files.items():
        target = pkg / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return scan.ModuleIndex(pkg)


# ----- class 3 on the real tree -------------------------------------------------------------


def test_no_new_raise_from_none_inside_an_except_block() -> None:
    found = set(scan.from_none_keys(scan.ModuleIndex()))
    new = sorted(found - set(_baseline()))
    assert not new, f"new `raise ... from None` inside an except block (hoist it): {new}"


def test_the_baseline_only_lists_sites_that_still_exist() -> None:
    found = set(scan.from_none_keys(scan.ModuleIndex()))
    stale = sorted(set(_baseline()) - found)
    assert not stale, f"sites were removed or hoisted; delete these baseline keys: {stale}"


# ----- class 3 seeded cells -----------------------------------------------------------------


def test_a_raise_from_none_in_a_handler_is_counted() -> None:
    src = "def f():\n    try:\n        g()\n    except ValueError:\n        raise E() from None\n"
    assert _keys(src) == ["decoy_engine/x.py::f::0"]


def test_ordinals_count_per_function_and_nested_handlers_count_once() -> None:
    src = (
        "def f():\n"
        "    try:\n        a()\n    except A:\n"
        "        try:\n            b()\n        except B:\n            raise E() from None\n"
        "        raise F() from None\n"
        "class C:\n"
        "    def m(self):\n"
        "        try:\n            a()\n        except A:\n            raise E() from None\n"
    )
    assert _keys(src) == [
        "decoy_engine/x.py::f::0",
        "decoy_engine/x.py::f::1",
        "decoy_engine/x.py::C.m::0",
    ]


@pytest.mark.parametrize(
    "src",
    [
        "def f():\n    raise E() from None\n",
        "def f():\n    try:\n        a()\n    except A as e:\n        raise E() from e\n",
        "def f():\n    try:\n        a()\n    except A:\n        raise E()\n",
        "def f():\n    try:\n        a()\n    except A:\n        pass\n    raise E() from None\n",
    ],
)
def test_forms_that_are_not_the_class_are_not_counted(src: str) -> None:
    assert _keys(src) == []


def test_adding_one_site_to_a_real_module_is_detected(tmp_path: Path) -> None:
    real = Path(scan.SRC / "io" / "bounded_reader.py").read_text(encoding="utf-8")
    before = len(_keys(real, "decoy_engine/io/bounded_reader.py"))
    planted = (
        real
        + "\n\ndef _planted():\n    try:\n        pass\n    except OSError:\n        raise ValueError() from None\n"
    )
    assert len(_keys(planted, "decoy_engine/io/bounded_reader.py")) == before + 1


# ----- class 6: public facades --------------------------------------------------------------


def test_nothing_imports_a_private_module_behind_a_public_facade() -> None:
    assert scan.facade_import_findings(scan.ModuleIndex()) == []


@pytest.mark.parametrize(
    "text",
    [
        "from decoy_engine._progress_events import PhaseEvent\n",
        "from decoy_engine import _progress_events\n",
        "import decoy_engine._progress_events\n",
        "from decoy_engine.reasons._record import COUNT_KEYS\n",
        "from decoy_engine.reasons import _record\n",
        "from decoy_engine.reasons._record import _is_count as is_count\n",
        "from .._progress_events import safe_error_class\n",
    ],
)
def test_seeded_private_facade_imports_are_flagged(tmp_path: Path, text: str) -> None:
    index = _tree(tmp_path, {"telemetry/_x.py": text, "telemetry/__init__.py": ""})
    assert scan.facade_import_findings(index) != []


def test_the_facades_themselves_and_public_names_are_allowed(tmp_path: Path) -> None:
    index = _tree(
        tmp_path,
        {
            "progress.py": "from decoy_engine._progress_events import PhaseEvent\n",
            "_progress_events.py": "from decoy_engine.reasons import is_count\n",
            "reasons/__init__.py": "from decoy_engine.reasons._record import COUNT_KEYS\n",
            "reasons/_x.py": "from decoy_engine.reasons._record import COUNT_KEYS\n",
            "telemetry/_x.py": "from decoy_engine.progress import PhaseEvent\n",
        },
    )
    assert scan.facade_import_findings(index) == []


@pytest.mark.parametrize(
    ("rel", "needle", "bad"),
    [
        (
            "telemetry/_vocab.py",
            "from decoy_engine.progress import safe_error_class",
            "from decoy_engine._progress_events import safe_error_class",
        ),
        (
            "safe_output/_event.py",
            "    COUNT_KEYS,\n    MAX_COUNTS,\n",
            "    COUNT_KEYS,\n    MAX_COUNTS,\n)\nfrom decoy_engine.reasons._record import _is_count\nfrom decoy_engine.reasons import (\n",
        ),
    ],
)
def test_restoring_either_private_import_turns_the_guard_red(
    tmp_path: Path, rel: str, needle: str, bad: str
) -> None:
    files = {
        p.relative_to(scan.SRC).as_posix(): p.read_text(encoding="utf-8")
        for p in scan.source_files()
        if p.relative_to(scan.SRC).as_posix() == rel
    }
    assert needle in files[rel]
    files[rel] = files[rel].replace(needle, bad, 1)
    assert scan.facade_import_findings(_tree(tmp_path, files)) != []
