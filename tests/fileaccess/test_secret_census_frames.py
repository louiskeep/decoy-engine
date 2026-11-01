"""Every census function, made to raise on a bad input, leaves no secret material in an engine frame.

One cell per function of `tests/sentry/secret_material_census.json` (derived from the code by
`scripts/secret_material_census.py`, so no function is chosen by hand) that takes secret material
as a parameter. Its secret parameters get a canary key; every other argument (and `self`) is a
`_Bomb`, an object whose every operation raises, so the call fails on the first use of a
non-secret input. Every frame of a `decoy_engine` module on every link of the chain (cause and
context included) is then walked: no local, parameter or closure cell may hold the canary as raw
bytes, latin-1 text, hex or escaped repr, and no secret-named local may hold bytes at all (a key
derived from the canary). Nested functions are reached through their enclosing function.
"""

from __future__ import annotations

import importlib
import inspect
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.fileaccess import test_secret_census_dedicated as dedicated
from tests.fileaccess.census_support import leaks

REPO = Path(__file__).resolve().parents[2]
CENSUS = REPO / "tests" / "sentry" / "secret_material_census.json"

# Admissible lengths reach the deepest code; the str form trips a function that requires bytes
# while a str (an env value, a hex text) is live.
CANARIES: tuple[bytes | str, ...] = (
    b"CENSUS-CANARY-32-" + b"k" * 15,
    b"CENSUS-CANARY16-",
    b"CANARY-8",
    "CENSUS-CANARY-TEXT-" + "t" * 13,
)

# Functions only a real policy refusal makes raise: their cells are in the dedicated file.
_DEDICATED = {
    "VaultWriter.__init__": "test_vault_writer_refused_by_the_installed_policy_keeps_no_key"
}

# Arguments that make a function whose bomb inputs return early raise past its entry instead.
_ARGUMENTS: dict[str, dict[str, Any]] = {
    "mask_cell": {"text": "call 555-867-5309 or mail a@b.co", "detector_ids": None},
    "mask_cell_with_spans": {"text": "call 555-867-5309 or mail a@b.co", "spans": None},
}


class _BombError(Exception):
    pass


def _boom(*_: object, **__: object) -> Any:
    raise _BombError("bad input")


class _Bomb:
    """A bad input: any attribute, operator, conversion or call raises."""

    __slots__ = ()

    def __getattr__(self, _name: str) -> Any:
        raise _BombError("bad input")

    __len__ = __iter__ = __bool__ = __eq__ = __ne__ = __hash__ = __index__ = __int__ = _boom
    __float__ = __str__ = __bytes__ = __format__ = __getitem__ = __contains__ = _boom
    __lt__ = __le__ = __gt__ = __ge__ = __add__ = __radd__ = __mul__ = __rmul__ = _boom
    __fspath__ = __call__ = __enter__ = __exit__ = __mod__ = __rmod__ = __sub__ = _boom
    __rsub__ = __and__ = __or__ = __xor__ = __neg__ = __buffer__ = _boom

    def __repr__(self) -> str:
        return "<bomb>"


def _census() -> list[dict[str, Any]]:
    return list(json.loads(CENSUS.read_text(encoding="utf-8"))["entries"])


def _callable_cells() -> list[tuple[str, str, frozenset[str]]]:
    cells: dict[tuple[str, str], set[str]] = {}
    for entry in _census():
        if "." in entry["function"] and entry["arrival"].startswith("closure"):
            continue
        if entry["arrival"] in {"parameter", "parameter:unannotated"}:
            cells.setdefault((entry["file"], entry["function"]), set()).add(entry["name"])
    return [(f, q, frozenset(n)) for (f, q), n in sorted(cells.items())]


def _resolve(file: str, qual: str) -> tuple[Any, Callable[..., Any], bool]:
    module = importlib.import_module(file[: -len(".py")].replace("/", "."))
    owner: Any = module
    parts = qual.split(".")
    for part in parts[:-1]:
        owner = getattr(owner, part)
    raw = inspect.getattr_static(owner, parts[-1])
    is_method = inspect.isclass(owner) and not isinstance(raw, (staticmethod, classmethod))
    if isinstance(raw, classmethod):
        return owner, raw.__func__, True
    if isinstance(raw, staticmethod):
        return owner, raw.__func__, False
    if isinstance(raw, property):
        assert raw.fget is not None
        return owner, raw.fget, True
    return owner, raw, is_method


def _call(
    owner: Any, fn: Callable[..., Any], bound: bool, secrets: frozenset[str], key: bytes | str
) -> BaseException | None:
    params = list(inspect.signature(fn).parameters.values())
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    if bound:
        first = params.pop(0)
        if first.name == "cls":
            args.append(owner)
        else:
            try:
                args.append(object.__new__(owner))
            except TypeError:
                args.append(_Bomb())
    given = _ARGUMENTS.get(fn.__name__, {})
    for p in params:
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        value = key if p.name in secrets else given.get(p.name, _Bomb())
        if p.kind == p.KEYWORD_ONLY:
            kwargs[p.name] = value
        else:
            args.append(value)
    del key
    try:
        result = fn(*args, **kwargs)
        if inspect.isgenerator(result):
            list(result)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        return exc
    return None


CELLS = _callable_cells()


def _verdicts(file: str, qual: str) -> set[str]:
    return {e["verdict"] for e in _census() if (e["file"], e["function"]) == (file, qual)}


@pytest.mark.parametrize(("file", "qual", "secrets"), CELLS, ids=[f"{f}::{q}" for f, q, _ in CELLS])
def test_census_function_keeps_no_secret_in_any_engine_frame(
    file: str, qual: str, secrets: frozenset[str]
) -> None:
    try:
        owner, fn, bound = _resolve(file, qual)
    except ImportError as exc:  # a module of an optional extra (mimesis) not installed here
        missing = exc.name or getattr(exc.__cause__, "name", None)
        if not missing:
            raise
        pytest.importorskip(missing, reason=str(exc))
        raise
    raised = 0
    for canary in CANARIES:
        exc = _call(owner, fn, bound, secrets, canary)
        if exc is not None:
            raised += 1
            assert leaks(exc, canary) == [], f"canary {canary!r}"
    if not raised and qual in _DEDICATED:
        assert hasattr(dedicated, _DEDICATED[qual])  # the cell that makes it raise
        return
    if not raised:
        # A function that returns on every bad input is a no-raise declaration or a boxed
        # entry (both checked from the AST by the census sentry); anything else needs arguments.
        assert _verdicts(file, qual) <= {"no-raise", "boxed"}, f"{qual} returned on every input"


def test_the_bomb_harness_detects_a_kept_key() -> None:
    def keeps(signing_key: bytes, other: object) -> None:
        str(other)

    canary = CANARIES[0]
    exc = _call(None, keeps, False, frozenset({"signing_key"}), canary)
    assert exc is not None
    # The test frame itself is not an engine frame; walk it explicitly for the self-check.
    tb = exc.__traceback__
    while tb is not None and tb.tb_frame.f_code.co_name != "keeps":
        tb = tb.tb_next
    assert tb is not None and tb.tb_frame.f_locals["signing_key"] == canary
