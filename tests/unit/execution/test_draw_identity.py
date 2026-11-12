"""The (table, column) draw identity has one owner, `execution/_adapter.py`.

Non-deterministic Faker's default selection namespace and the unkeyed shuffle both key on it.
A second hand-rolled copy would drift (separators, the nested outer column, the empty-table
check), and once shipped each copy is an output contract of its own.
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path
from types import SimpleNamespace

import pytest

from decoy_engine.execution._adapter import (
    column_draw_identity,
    context_draw_identity,
    draw_column,
)
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._faker_positional import faker_selection_namespace

_SRC = Path(__file__).resolve().parents[3] / "src" / "decoy_engine"
_OWNER = "execution/_adapter.py"


def _length_prefixed_fstrings(root: Path) -> list[str]:
    """Files under `root` with an f-string that joins two length-prefixed names.

    One `{len(x)}:{x}` alone is a value token (the FK key canonicaliser has those); two in
    one string is a compound name identity like `<len>:<table>/<len>:<column>`."""
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            prefixes = sum(
                1
                for value, nxt in itertools.pairwise(node.values)
                if isinstance(value, ast.FormattedValue)
                and isinstance(value.value, ast.Call)
                and isinstance(value.value.func, ast.Name)
                and value.value.func.id == "len"
                and isinstance(nxt, ast.Constant)
                and str(nxt.value).startswith(":")
            )
            if prefixes >= 2:
                found.add(path.relative_to(root).as_posix())
    return sorted(found)


def test_only_the_owner_builds_a_length_prefixed_identity() -> None:
    assert _length_prefixed_fstrings(_SRC) == [_OWNER]


def test_the_scan_flags_a_second_copy(tmp_path: Path) -> None:
    (tmp_path / "copy.py").write_text('def f(t, c):\n    return f"{len(t)}:{t}/{len(c)}:{c}"\n')
    assert _length_prefixed_fstrings(tmp_path) == ["copy.py"]


def test_identity_format_and_its_collision_case() -> None:
    assert column_draw_identity("t", "email") == "1:t/5:email"
    assert column_draw_identity("a/b", "c") != column_draw_identity("a", "b/c")


def test_faker_default_namespace_is_the_shared_identity() -> None:
    # Byte-identical to the pre-unification Faker default; the faker goldens pin the rest.
    assert faker_selection_namespace("t", "c", None) == "faker-nd/1:t/1:c"
    assert faker_selection_namespace("t", "c", None) == f"faker-nd/{column_draw_identity('t', 'c')}"


def test_context_identity_uses_the_outer_column_of_a_nested_child() -> None:
    ctx = SimpleNamespace(current_table="t", nested_outer_column="addr")
    assert draw_column(ctx, "_nested_leaves") == "addr"
    assert context_draw_identity(ctx, "_nested_leaves", strategy="shuffle") == "1:t/4:addr"


@pytest.mark.parametrize("strategy", ["faker", "shuffle"])
def test_context_identity_fails_closed_without_a_table(strategy: str) -> None:
    with pytest.raises(StrategyError) as exc:
        context_draw_identity(SimpleNamespace(), "c", strategy=strategy)
    assert exc.value.code == "faker_positional_table_unknown"
    assert exc.value.strategy == strategy
