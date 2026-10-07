"""The `FileAccess` policy contract: roles, requests, capability protocols and the two errors.

Pattern: object capabilities (Miller, "Robust Composition", 2006) as in Capsicum and
WASI preopens. A path string in a config is only a request; authority is the handle,
directory capability or output transaction a policy issued for it. Docs:
docs/security/file-access-policy.md.
"""

from __future__ import annotations

import enum
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO, Literal, Protocol, TextIO, runtime_checkable

from decoy_engine.errors import DecoyError

if TYPE_CHECKING:
    from decoy_engine.fileaccess._errors import EncodedKey

# Bumped when a platform built against an older policy must refuse the engine.
CONTRACT_VERSION = 1


class FileRole(str, enum.Enum):
    """What the engine wants a file for; the policy decides per role."""

    SOURCE = "source"
    SUBSET_INPUT = "subset_input"
    REFERENCE = "reference"
    CORPUS = "corpus"
    SNAPSHOT = "snapshot"
    SECRET = "secret"  # noqa: S105 - a role name, not a credential
    VAULT = "vault"
    CONFIG = "config"
    FIXED_WIDTH_LAYOUT = "fixed_width_layout"
    QUARANTINE_OUTPUT = "quarantine_output"
    TARGET_OUTPUT = "target_output"
    SPILL_HINT = "spill_hint"
    JOB_OUTPUT = "job_output"
    CUSTOM_PROVIDER_ROOT = "custom_provider_root"
    MODEL_PACK_ROOT = "model_pack_root"
    LOG_OUTPUT = "log_output"


Access = Literal["read", "stat", "read_dir", "write"]
StageLabel = Literal[
    "table_sink", "quarantine", "quarantine_direct", "isolated", "hold", "subset", "vault", "pack"
]


@dataclass(frozen=True)
class FileRequest:
    """One request: a bare locator (scheme already stripped) for a role at a config position."""

    role: FileRole
    access: Access
    locator: str
    field_key: str


class FileAccessDenied(DecoyError):  # noqa: N818 - the plan's name for the one denial
    """The one denial. `str()` is `file access denied: <encoded field key>` and nothing else.

    Raised `from None` so no cause, context or locator rides with it across a boundary."""

    def __init__(self, field_key: str) -> None:
        from decoy_engine.fileaccess._errors import encode_field_key

        self.field_key: str = encode_field_key(field_key)
        super().__init__(f"file access denied: {self.field_key}")

    def __reduce__(self) -> tuple[object, tuple[str]]:
        return (FileAccessDenied, (self.field_key,))


class FileAccessUnavailable(DecoyError):  # noqa: N818 - the plan's name
    """No policy where one is required, or a route a strict policy cannot serve."""


@runtime_checkable
class FileHandle(Protocol):
    """An open, already-authorized regular file."""

    display: str
    strict: bool

    def fileno(self) -> int: ...

    def library_path(self) -> str:
        """Only the adapter modules may call this (the sentry enforces it)."""
        ...

    def stat(self) -> os.stat_result: ...

    def identity(self) -> str: ...

    def read_all(self, limit: int | None = None) -> bytes: ...

    def open_binary(self) -> BinaryIO: ...

    def open_text(self, encoding: str = "utf-8") -> TextIO: ...

    def close(self) -> None: ...

    def __enter__(self) -> FileHandle: ...

    def __exit__(self, *exc: object) -> None: ...


@runtime_checkable
class DirHandle(Protocol):
    """An open, already-authorized directory; children open relative to its own descriptor."""

    display: str

    def entries(self) -> Iterator[os.DirEntry[str]]: ...

    def open_file(self, rel: str) -> FileHandle: ...

    def open_dir(self, rel: str) -> DirHandle: ...

    def close(self) -> None: ...

    def __enter__(self) -> DirHandle: ...

    def __exit__(self, *exc: object) -> None: ...


@runtime_checkable
class StageDir(Protocol):
    """Name-taking operations over one stage directory. Every name is validated by the stage."""

    display: str
    strict: bool

    def create_file(self, component: str) -> int: ...

    def open_file(self, component: str) -> int: ...

    def create_dir(self, component: str) -> StageDir: ...

    def remove(self, component: str) -> None: ...

    def rename(self, src: str, dst: str) -> None: ...

    def local_stage_path(self) -> str:
        """The stage path, for the one subprocess route; strict policies raise."""
        ...


@runtime_checkable
class OutputTxn(StageDir, Protocol):
    """An opaque, policy-issued stage capability: publishes once or aborts, never both."""

    def publish(self) -> None: ...

    def abort(self) -> None: ...

    def __enter__(self) -> OutputTxn: ...

    def __exit__(self, *exc: object) -> None: ...


@runtime_checkable
class OutputDir(Protocol):
    """A write capability: an output parent plus one allowed leaf."""

    display: str
    locator: str
    strict: bool

    def begin(
        self,
        kind: Literal["file", "dir"],
        mode: Literal["exclusive", "replace"],
        *,
        label: StageLabel,
    ) -> OutputTxn: ...

    def ensure_dir(self) -> None: ...

    def is_empty_or_absent(self) -> bool: ...


@runtime_checkable
class FileAccess(Protocol):
    def open_read(self, req: FileRequest) -> FileHandle: ...

    def open_dir(self, req: FileRequest) -> DirHandle: ...

    def stat(self, req: FileRequest) -> os.stat_result: ...

    def output_dir(self, req: FileRequest) -> OutputDir: ...

    def allows_subprocess(self) -> bool: ...


__all__ = [
    "CONTRACT_VERSION",
    "Access",
    "DirHandle",
    "EncodedKey",
    "FileAccess",
    "FileAccessDenied",
    "FileAccessUnavailable",
    "FileHandle",
    "FileRequest",
    "FileRole",
    "OutputDir",
    "OutputTxn",
    "StageDir",
    "StageLabel",
]
