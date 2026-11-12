"""The search groups nothing itself: every class comes from FrequencySet.

One class identity (null policy, type families) for the search and the
measures holds only if ``anonymize.search`` never groups rows or values on
its own. This walks the syntax tree of every module in the package.
"""

from __future__ import annotations

import ast
import pkgutil
from pathlib import Path

import decoy_engine.anonymize.search as search

FORBIDDEN = frozenset(
    {
        "factorize",
        "groupby",
        "unique",
        "drop_duplicates",
        "value_counts",
        "dictionary_encode",
        "Counter",
    }
)


def grouping_calls(source: str) -> list[str]:
    tree = ast.parse(source)
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Name):
            name = aliases.get(func.id, func.id)
            if name == "getattr" and len(node.args) >= 2:
                arg = node.args[1]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    name = arg.value
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name in FORBIDDEN:
            found.append(name)
    return found


def test_guard_sees_seeded_bypasses() -> None:
    assert grouping_calls("import pandas as pd\npd.factorize(x)") == ["factorize"]
    assert grouping_calls("from pandas import factorize as f\nf(x)") == ["factorize"]
    assert grouping_calls("from numpy import unique as u\nu(x)") == ["unique"]
    assert grouping_calls("import numpy as np\nnp.unique(x)") == ["unique"]
    assert grouping_calls('import pandas as pd\ngetattr(pd, "factorize")(x)') == ["factorize"]
    assert grouping_calls("frame.groupby(['a']).size()") == ["groupby"]
    assert grouping_calls("np.bincount(x)") == []


def test_search_package_has_no_grouping_calls() -> None:
    package_dir = Path(search.__file__).parent
    modules = [info.name for info in pkgutil.iter_modules([str(package_dir)])]
    assert {"_search", "_loss", "_apply", "_result"} <= set(modules)
    for name in [*modules, "__init__"]:
        source = (package_dir / f"{name}.py").read_text(encoding="utf-8")
        assert grouping_calls(source) == [], name
