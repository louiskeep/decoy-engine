"""What `TracebackException(capture_locals=True)` would show of the engine's own frames."""

from __future__ import annotations

from collections.abc import Iterator

_ENGINE = "/src/decoy_engine/"


def _strings(value: object, depth: int = 0) -> Iterator[str]:
    """Every str/bytes reachable through tuples, lists, sets, frozensets and dict keys/values."""
    if depth > 8:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, (bytes, bytearray, memoryview)):
        yield bytes(value).decode("latin1")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key, depth + 1)
            yield from _strings(item, depth + 1)
    elif isinstance(value, (tuple, list, set, frozenset)):
        for item in value:
            yield from _strings(item, depth + 1)


def engine_locals(exc: BaseException) -> Iterator[tuple[str, str, str]]:
    """(function, local name, text) for every engine frame on every link of the chain: the
    local's repr (what `capture_locals` prints) and each string nested inside it."""
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        link = stack.pop()
        if link is None or id(link) in seen:
            continue
        seen.add(id(link))
        stack.extend([link.__cause__, link.__context__])
        tb = link.__traceback__
        while tb is not None:
            frame = tb.tb_frame
            if _ENGINE in frame.f_code.co_filename:
                for name, value in frame.f_locals.items():
                    try:
                        shown = repr(value)
                    except Exception:
                        shown = ""
                    yield frame.f_code.co_name, name, shown
                    for text in _strings(value):
                        yield frame.f_code.co_name, name, text
            tb = tb.tb_next


def assert_no_engine_local_holds(exc: BaseException, *canaries: str) -> None:
    for function, name, text in engine_locals(exc):
        for canary in canaries:
            assert canary not in text, f"{function}() local {name!r} holds {canary!r}"
