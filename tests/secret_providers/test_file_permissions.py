"""E1: a `file:` secret is accepted only when owner-only, checked on the descriptor that is read.

Plan `docs/plans/2026-10-31-f13-slices-3-4-consumers.md` section 3.6 / T18. The pattern is
OpenSSH's refusal of a group- or world-accessible private key, done with `fstat` on the opened
descriptor (never a path `stat` followed by a second open, the check-then-use race).
"""

from __future__ import annotations

import os
import traceback
import uuid
from pathlib import Path

import pytest

from decoy_engine.keyprovider import MaskSecretError, MissingMaskSecret, key_provider_from_ref

UNSAFE = "secret_file_permissions_unsafe"
TOKEN = "E1PATH" + uuid.uuid4().hex[:10]
GOOD = ("ab" * 32).encode()
OTHER = ("cd" * 32).encode()


def _write(tmp: Path, mode: int, data: bytes = GOOD, name: str | None = None) -> Path:
    path = tmp / (name or f"{TOKEN}-{mode:o}.key")
    path.write_bytes(data)
    os.chmod(path, mode)
    return path


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _key(path: Path) -> bytes:
    return key_provider_from_ref(f"file:{path}").mask_key()


@pytest.mark.parametrize("mode", [0o600, 0o400], ids=lambda m: f"{m:o}")
def test_owner_only_modes_are_accepted(tmp_path: Path, registry, mode: int) -> None:
    assert len(_key(_write(tmp_path, mode))) == 32


@pytest.mark.parametrize(
    "mode", [0o640, 0o644, 0o604, 0o660, 0o666, 0o610, 0o601], ids=lambda m: f"{m:o}"
)
def test_group_or_other_bits_are_refused(tmp_path: Path, registry, mode: int) -> None:
    secret = ("SECRET-" + TOKEN).encode()
    path = _write(tmp_path, mode, secret)
    with pytest.raises(MaskSecretError) as info:
        _key(path)
    exc = info.value
    assert exc.code == UNSAFE
    rendered = [str(exc), repr(exc), repr(exc.args), "".join(traceback.format_exception(exc))]
    for text in rendered:
        assert TOKEN not in text and str(path) not in text
        assert f"{mode:o}" not in text and oct(mode) not in text
    assert exc.__cause__ is None and exc.__context__ is None
    # As AT-X1: engine frame locals never hold the file's bytes. (The caller's own `ref`
    # argument is in the caller-facing frames by design and is not secret material.)
    frames = traceback.TracebackException.from_exception(exc, capture_locals=True).stack
    engine = [f for f in frames if "/src/decoy_engine/" in f.filename]
    assert engine, "positive control: the engine frames were rendered"
    assert all("SECRET-" + TOKEN not in repr(f.locals) for f in engine)


def test_a_symlink_is_judged_by_its_target(tmp_path: Path, registry) -> None:
    safe = _write(tmp_path, 0o600, name="safe.key")
    unsafe = _write(tmp_path, 0o644, name="unsafe.key")
    (tmp_path / "to-safe").symlink_to(safe)
    (tmp_path / "to-unsafe").symlink_to(unsafe)
    assert len(_key(tmp_path / "to-safe")) == 32
    with pytest.raises(MaskSecretError) as info:
        _key(tmp_path / "to-unsafe")
    assert info.value.code == UNSAFE


def test_a_kubernetes_secret_volume_layout_passes(tmp_path: Path, registry) -> None:
    # kubelet: <dir>/..<timestamp>/key (mode 0400), <dir>/..data -> ..<timestamp>,
    # <dir>/key -> ..data/key. The directories are 0755; only the final file's mode counts.
    stamp = tmp_path / "..2026_10_31_12_00_00.000000001"
    stamp.mkdir()
    _write(stamp, 0o400, name="key")
    (tmp_path / "..data").symlink_to(stamp.name)
    (tmp_path / "key").symlink_to("..data/key")
    assert len(_key(tmp_path / "key")) == 32


def test_a_directory_is_a_missing_secret_not_a_permission_error(tmp_path: Path, registry) -> None:
    folder = tmp_path / "secrets"
    folder.mkdir(mode=0o755)
    with pytest.raises(MissingMaskSecret):
        _key(folder)


def test_a_missing_file_is_still_the_missing_secret_error(tmp_path: Path, registry) -> None:
    with pytest.raises(MissingMaskSecret):
        _key(tmp_path / "absent.key")


def test_the_mode_is_checked_on_the_descriptor_that_is_read(
    tmp_path: Path, registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swap the path for a 0644 file with other bytes right after the mode check: the read must
    return the checked descriptor's bytes, so nothing re-resolves the path after `fstat`."""
    expected = _key(_write(tmp_path, 0o600, OTHER, name="control.key"))
    path = _write(tmp_path, 0o600, OTHER, name="target.key")
    swap = _write(tmp_path, 0o644, GOOD, name="swap.key")
    real_fstat = os.fstat
    calls: list[int] = []

    def spy(fd: int) -> os.stat_result:
        result = real_fstat(fd)
        if not calls:
            os.replace(swap, path)
        calls.append(fd)
        return result

    monkeypatch.setattr(os, "fstat", spy)
    assert _key(path) == expected
    assert calls, "the provider never checked the opened descriptor"


@pytest.mark.parametrize("outcome", ["success", "refusal", "read_error", "missing"])
def test_the_descriptor_is_closed_on_every_exit(
    tmp_path: Path, registry, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    path = {
        "success": lambda: _write(tmp_path, 0o600),
        "refusal": lambda: _write(tmp_path, 0o644),
        "read_error": lambda: _write(tmp_path, 0o600),
        "missing": lambda: tmp_path / "absent.key",
    }[outcome]()
    if outcome == "read_error":

        def failing_read(fd: int, n: int, *offset: int) -> bytes:
            raise OSError(5, "EIO " + TOKEN)

        monkeypatch.setattr(os, "read", failing_read)
        monkeypatch.setattr(os, "pread", failing_read)
    before = _fd_count()
    try:
        _key(path)
    except MaskSecretError as exc:
        assert outcome != "success"
        if outcome == "read_error":
            assert isinstance(exc, MissingMaskSecret) and TOKEN not in str(exc)
    else:
        assert outcome == "success"
    monkeypatch.undo()
    assert _fd_count() == before


def test_windows_skips_the_mode_check(
    tmp_path: Path, registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NTFS ACLs are not inspected (documented): with `os.name == "nt"` a 0644 file passes."""
    path = _write(tmp_path, 0o644)
    monkeypatch.setattr(os, "name", "nt")
    assert len(_key(path)) == 32
