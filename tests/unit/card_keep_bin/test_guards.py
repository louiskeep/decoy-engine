"""Choke-point guards for keep_bin.

1. Every production use of `fpe_encrypt_value` / `fpe_decrypt_value` is a direct call that
   passes `keep_bin=`. Any other load of either name (an alias, a conditional expression, a
   `functools.partial`, a container) is itself a violation, so an alias can never hide a call
   that drops the option.
2. Only the resolver module and the two plan checks read the `keep_bin` key.

Both walk the real source tree, and each proves on seeded modules that it sees a bypass.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "decoy_engine"
_TARGETS = frozenset({"fpe_encrypt_value", "fpe_decrypt_value"})
_FPE_MODULE = "decoy_engine.transforms.fpe"

# The one caller that has no keep_bin: text-span masking reads its own detector table.
_CALL_ALLOWLIST = {"transforms/text_mask.py": "detector config has no keep_bin"}
_KEY_READERS = frozenset(
    {"transforms/_fpe_checksum.py", "plan/_checks_fpe.py", "plan/_checks_fpe_join.py"}
)


def _bound_names(tree: ast.AST) -> set[str]:
    """Local names bound to a target function by `from ... import`."""
    funcs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _FPE_MODULE:
            funcs |= {a.asname or a.name for a in node.names if a.name in _TARGETS}
    return funcs


def _is_use(node: ast.AST, funcs: set[str]) -> bool:
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        return node.id in funcs
    # Any attribute spelled like a target counts, whatever its base: conservative on purpose.
    return isinstance(node, ast.Attribute) and node.attr in _TARGETS


def violations(source: str) -> list[int]:
    """Line numbers of target uses that are not a direct call passing `keep_bin=`."""
    tree = ast.parse(source)
    funcs = _bound_names(tree)
    good: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_use(node.func, funcs):
            if any(k.arg == "keep_bin" for k in node.keywords):
                good.add(id(node.func))
    return [n.lineno for n in ast.walk(tree) if _is_use(n, funcs) and id(n) not in good]


def _modules() -> list[tuple[str, str]]:
    return [
        (path.relative_to(_SRC).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(_SRC.rglob("*.py"))
    ]


def test_every_fpe_value_call_passes_keep_bin() -> None:
    bad = {rel: lines for rel, text in _modules() if (lines := violations(text))}
    assert set(bad) == set(_CALL_ALLOWLIST), bad
    assert len(_CALL_ALLOWLIST) == 1


def test_the_walk_sees_real_call_sites() -> None:
    callers = {rel for rel, text in _modules() if _TARGETS & set(_names_used(text))}
    assert {
        "execution/_strategies/_fpe.py",
        "execution/out_of_core/_mask_group_b.py",
        "execution/native/_crypto_reference.py",
        "unmask.py",
        "transforms/text_mask.py",
    } <= callers


def _names_used(text: str) -> list[str]:
    return [n.id for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Name)]


_SEEDED = {
    "missing_keyword": (
        "from decoy_engine.transforms.fpe import fpe_encrypt_value\n"
        "fpe_encrypt_value(v, k, c, t, True, False, 'luhn')\n"
    ),
    "conditional_alias": (
        "from decoy_engine.transforms.fpe import fpe_decrypt_value, fpe_encrypt_value\n"
        "t = fpe_encrypt_value if f else fpe_decrypt_value\n"
        "t(v, k, c, t, True, False, 'luhn', keep_bin=6)\n"
    ),
    "import_as": (
        "from decoy_engine.transforms.fpe import fpe_encrypt_value as enc\nenc(v, k, c, t)\n"
    ),
    "partial": (
        "import functools\n"
        "from decoy_engine.transforms.fpe import fpe_encrypt_value\n"
        "f = functools.partial(fpe_encrypt_value, keep_bin=6)\n"
    ),
    "module_attribute": (
        "from decoy_engine.transforms import fpe\nfpe.fpe_encrypt_value(v, k, c, t)\n"
    ),
    "dotted_import": ("import decoy_engine.transforms.fpe as m\nx = [m.fpe_decrypt_value]\n"),
}


def test_seeded_bypasses_are_flagged() -> None:
    for name, source in _SEEDED.items():
        assert violations(source), name
    ok = (
        "from decoy_engine.transforms.fpe import fpe_encrypt_value as enc\n"
        "enc(v, k, c, t, keep_bin=kb)\n"
    )
    assert violations(ok) == []


def _key_reads(text: str) -> int:
    return sum(
        1
        for n in ast.walk(ast.parse(text))
        if isinstance(n, ast.Constant) and n.value == "keep_bin"
    )


def test_only_the_resolver_and_plan_checks_read_keep_bin() -> None:
    readers = {rel for rel, text in _modules() if _key_reads(text)}
    assert readers == _KEY_READERS


def test_the_key_walk_sees_a_seeded_read() -> None:
    assert _key_reads('x = cfg.get("keep_bin")\n') == 1
    assert _key_reads('x = cfg["keep_bin"]\n') == 1
