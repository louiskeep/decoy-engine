"""One generated-at shape: `datetime.now(timezone.utc).isoformat()`, ending `+00:00`.

A writer that appends `Z` to an ISO text, or reads the naive `datetime.utcnow()`, gives a second
shape that readers must keep parsing. The scan is AST-based and resolves how `datetime` is
imported, so a renamed import does not hide a site. `pd.Timestamp.utcnow()` is a different API
and is not a hit.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src" / "decoy_engine"
_NAIVE_UTC = {"utcnow", "utcfromtimestamp"}


def _datetime_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Names bound to the `datetime` class, and names bound to the `datetime` module."""
    classes: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "datetime":
            classes.update(a.asname or a.name for a in node.names if a.name == "datetime")
        elif isinstance(node, ast.Import):
            modules.update(a.asname or a.name for a in node.names if a.name == "datetime")
    return classes, modules


def _has_isoformat_call(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "isoformat"
        for n in ast.walk(node)
    )


def _is_z(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value == "Z"


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _chain(node: ast.AST) -> list[ast.AST]:
    """The receiver chain of a call, innermost last: `a.b().c()` gives `[a.b().c(), a.b(), a]`."""
    out: list[ast.AST] = []
    while True:
        out.append(node)
        if isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Attribute):
            node = node.value
        else:
            return out


def _on_datetime(base: ast.AST, classes: set[str], modules: set[str]) -> bool:
    if isinstance(base, ast.Name):
        return base.id in classes
    return (
        isinstance(base, ast.Attribute)
        and base.attr == "datetime"
        and isinstance(base.value, ast.Name)
        and base.value.id in modules
    )


def _naive_clock(node: ast.AST, classes: set[str], modules: set[str]) -> bool:
    """`datetime.now()` or `datetime.today()` called with no timezone argument."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"now", "today"}
        and _on_datetime(node.func.value, classes, modules)
        and not node.args
        and not any(k.arg == "tz" for k in node.keywords)
    )


def shape_hits(source: str) -> list[str]:
    """Each place the source builds a `Z`-suffixed ISO stamp or calls naive `datetime` UTC."""
    tree = ast.parse(source)
    classes, modules = _datetime_names(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            if _is_z(node.right) and _has_isoformat_call(node.left):
                hits.append(f"line {node.lineno}: isoformat plus Z")
        elif isinstance(node, ast.JoinedStr) and node.values:
            tail = node.values[-1]
            if (
                isinstance(tail, ast.Constant)
                and isinstance(tail.value, str)
                and tail.value.startswith("Z")
                and any(
                    isinstance(v, ast.FormattedValue) and _has_isoformat_call(v)
                    for v in node.values
                )
            ):
                hits.append(f"line {node.lineno}: f-string isoformat plus Z")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            if func.attr == "replace" and len(node.args) == 2:
                old, new = _const_str(node.args[0]), _const_str(node.args[1])
                if old == "+00:00" and new == "Z" and _has_isoformat_call(func.value):
                    hits.append(f"line {node.lineno}: isoformat offset replaced by Z")
                continue
            if func.attr == "isoformat" and any(
                _naive_clock(link, classes, modules) for link in _chain(func.value)
            ):
                hits.append(f"line {node.lineno}: isoformat of a clock with no timezone")
                continue
            if func.attr not in _NAIVE_UTC:
                continue
            if _on_datetime(func.value, classes, modules):
                hits.append(f"line {node.lineno}: naive {func.attr}")
    return hits


def test_no_second_generated_at_shape_in_the_source() -> None:
    found = {
        f"{path.relative_to(SRC).as_posix()}: {hit}"
        for path in sorted(SRC.rglob("*.py"))
        for hit in shape_hits(path.read_text(encoding="utf-8"))
    }
    assert found == set()


@pytest.mark.parametrize(
    "source",
    [
        "from datetime import datetime\nx = datetime.utcnow().isoformat() + 'Z'\n",
        "import datetime\nx = f'{datetime.datetime.now().isoformat()}Z'\n",
        "from datetime import datetime\nx = datetime.utcnow()\n",
        "from datetime import datetime as D\nx = D.utcnow()\n",
        "import datetime as dt\nx = dt.datetime.utcfromtimestamp(0)\n",
        'x = stamp.isoformat() + "Z"\n',
    ],
)
def test_the_scan_flags_each_seeded_spelling(source: str) -> None:
    assert shape_hits(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "from datetime import datetime, timezone\nx = datetime.now(timezone.utc).isoformat()\n",
        "import pandas as pd\nx = pd.Timestamp.utcnow()\n",
        "x = value.replace('Z', '+00:00')\n",
        "from datetime import datetime, timezone\nx = datetime.now(tz=timezone.utc).isoformat()\n",
        "from datetime import datetime\nx = datetime.now().year\n",
    ],
)
def test_the_scan_leaves_the_one_shape_and_unrelated_calls_alone(source: str) -> None:
    assert shape_hits(source) == []
