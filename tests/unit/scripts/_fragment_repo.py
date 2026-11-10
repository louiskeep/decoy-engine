"""A scratch git repository for the changelog-fragment tests (check_fragment, the compiler)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

GIT = shutil.which("git") or "/usr/bin/git"
REPO = Path(__file__).resolve().parents[3]


def fragment(
    body: str = "It does the thing.", *, breaking: str | None = None, name: str = "thing"
) -> str:
    first = f"Breaking: {breaking}\n\n" if breaking else ""
    return f"{first}### Added ({name}, SLICE, 2026-11-10)\n\n{body}\n"


class ScratchRepo:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        proc = subprocess.run(  # noqa: S603 - fixed git executable, test-literal arguments
            [GIT, "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
            cwd=self.path,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()

    def commit(self, files: dict[str, str], message: str) -> str:
        for name, text in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def delete(self, name: str) -> None:
        self.git("rm", "-q", name)
