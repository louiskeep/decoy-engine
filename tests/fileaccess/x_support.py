"""Shared pieces for the AT-X2, AT-X3 and AT-X4 tests: a rooted tree whose files sit under a
directory carrying a sentinel, a spy on the locator handed to each library, and renderers."""

from __future__ import annotations

import logging
import traceback
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from decoy_engine.fileaccess import _rooted
from tests.fileaccess.support import RootRelative

LOCATOR_SENTINEL = "LOCSENT" + uuid.uuid4().hex[:10]


@dataclass
class XWorld:
    root: Path
    sub: Path
    policy: RootRelative
    library_paths: list[str] = field(default_factory=list)

    def forbidden(self) -> list[str]:
        """Every string that must never appear: the sentinel directory, the root, the path
        handed to each library (`/proc/self/fd/<n>`), and the descriptor directory itself."""
        return [LOCATOR_SENTINEL, str(self.root), "/proc/self/fd", *self.library_paths]


@pytest.fixture
def xworld(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[XWorld]:
    root = tmp_path / "root"
    sub = root / LOCATOR_SENTINEL
    sub.mkdir(parents=True)
    policy = RootRelative(root)
    world = XWorld(root, sub, policy)
    real = _rooted.FdHandle.library_path

    def spy(self: _rooted.FdHandle) -> str:
        value = real(self)
        world.library_paths.append(value)
        return value

    monkeypatch.setattr(_rooted.FdHandle, "library_path", spy)
    try:
        yield world
    finally:
        policy.close()


def rendered(exc: BaseException, caplog: pytest.LogCaptureFixture | None = None) -> list[str]:
    """Every form an error or a log line can reach a reader in."""
    out = [str(exc), repr(exc), "".join(traceback.format_exception(exc)), repr(exc.args)]
    link: BaseException | None = exc
    seen: set[int] = set()
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        out.append(str(link))
        link = link.__cause__ or link.__context__
    log = logging.getLogger(f"x.{uuid.uuid4().hex}")
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(logging.Formatter().format(record))

    log.addHandler(Capture())
    log.propagate = False
    log.error("failed", exc_info=exc)
    out.extend(records)
    if caplog is not None:
        out.extend(logging.Formatter().format(r) for r in caplog.records)
    return out


def assert_no_locator(world: XWorld, texts: list[str]) -> None:
    for text in texts:
        for needle in world.forbidden():
            assert needle not in text, f"{needle!r} leaked in: {text[:300]!r}"
