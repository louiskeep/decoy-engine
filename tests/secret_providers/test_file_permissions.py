"""F13 slice E1: a ``file:`` secret is admitted only when its OPENED descriptor is owner-only.

The OpenSSH ``UNPROTECTED PRIVATE KEY FILE`` rule, applied where the file is read: the mode comes
from ``fstat`` on the descriptor the bytes are read from, so a path swapped or re-pointed between
the check and the read cannot change the verdict (the check-then-use race of a path ``stat``).
A directory is still the plain unreadable-file case (``missing_mask_secret``), never a
permissions refusal. Windows is not checked (NTFS ACLs are not mode bits).

Deterministic behavior of non-regular files, by design: a FIFO is judged by its own mode like any
file (0600 with a writer is read; 0644 is refused); ``/dev/null`` is a 0666 character device and
is refused with ``secret_file_permissions_unsafe``.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

from decoy_engine import reasons
from decoy_engine.keyprovider import MaskSecretError, SecretKeyProvider, key_provider_from_ref
from decoy_engine.secret_providers import admit_mask_key, parse_ref, prepare_ref
from decoy_engine.secret_providers._errors import error_for
from decoy_engine.secret_providers._local import FileSecretProvider
from tests.secret_providers._helpers import keyed_config
from tests.secret_providers.test_leak_sentry import assert_absent, render_everywhere

UNSAFE = "secret_file_permissions_unsafe"
MISSING = "missing_mask_secret"

SECRET_MARKER = b"E1-SECRET-CANARY-5be1f00d-never-print"  # 37 bytes, a valid mask root
PATH_MARKER = "E1-PATH-CANARY-a9d3"
OTHER_SECRET = b"E1-SWAPPED-IN-SECRET-0000-different!"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX modes; Windows is unchecked")


def _secret_file(base: Path, mode: int, *, material: bytes = SECRET_MARKER) -> Path:
    folder = base / PATH_MARKER
    folder.mkdir(exist_ok=True)
    path = folder / f"mask-{mode:o}.key"
    path.write_text(material.hex())
    path.chmod(mode)
    return path


def _admit(path: Path | str) -> bytes:
    return key_provider_from_ref(f"file:{path}").mask_key()


def _expected() -> bytes:
    return SecretKeyProvider(SECRET_MARKER).mask_key()


def _refusal(path: Path | str) -> MaskSecretError:
    with pytest.raises(MaskSecretError) as caught:
        _admit(path)
    return caught.value


def _boxed_refusal(path: Path) -> MaskSecretError:
    # Through the boxed choke point (`prepare_ref` drops its argument before the provider runs).
    with pytest.raises(MaskSecretError) as caught:
        prepare_ref(f"file:{path}")
    return caught.value


def _engine_frames_only(exc: MaskSecretError) -> MaskSecretError:
    # The caller's own frames hold the path it passed in; only engine frames are under test.
    tb = exc.__traceback__
    while tb is not None and "decoy_engine" not in tb.tb_frame.f_code.co_filename:
        tb = tb.tb_next
    assert tb is not None  # positive control: the engine raised it
    return exc.with_traceback(tb)


# -- mode table --------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [0o600, 0o400, 0o700], ids=oct)
def test_owner_only_modes_are_admitted(tmp_path, mode):
    assert _admit(_secret_file(tmp_path, mode)) == _expected()


@pytest.mark.parametrize(
    "mode",
    [0o640, 0o644, 0o604, 0o660, 0o666, 0o440, 0o444, 0o610, 0o601, 0o602, 0o620, 0o777],
    ids=oct,
)
def test_group_or_other_bits_are_refused(tmp_path, mode):
    exc = _refusal(_secret_file(tmp_path, mode))
    assert exc.code == UNSAFE
    assert type(exc).__name__ == "SecretFilePermissionsUnsafe"


@pytest.mark.parametrize("mode", [0o755, 0o700], ids=oct)
def test_a_directory_is_the_unreadable_file_case_not_a_permissions_refusal(tmp_path, mode):
    folder = tmp_path / PATH_MARKER / "secret-dir"
    folder.mkdir(parents=True)
    folder.chmod(mode)
    exc = _refusal(folder)
    assert exc.code == MISSING


def test_a_missing_file_still_reports_missing_mask_secret(tmp_path):
    assert _refusal(tmp_path / PATH_MARKER / "absent.key").code == MISSING


# -- symlinks: the descriptor decides -----------------------------------------------------------


def test_symlink_to_owner_only_target_is_admitted(tmp_path):
    target = _secret_file(tmp_path, 0o600)
    link = tmp_path / "link.key"
    link.symlink_to(target)
    assert _admit(link) == _expected()


def test_symlink_to_world_readable_target_is_refused(tmp_path):
    target = _secret_file(tmp_path, 0o644)
    link = tmp_path / "link.key"
    link.symlink_to(target)
    assert _refusal(link).code == UNSAFE


def test_kubernetes_secret_volume_layout_with_0400_is_admitted(tmp_path):
    # kubelet's AtomicWriter: <vol>/key -> ..data/key, ..data -> ..<timestamp>/ (defaultMode 0400).
    volume = tmp_path / "vol"
    stamp = volume / "..2026_10_31_00_00_00.000000001"
    stamp.mkdir(parents=True)
    (stamp / "key").write_text(SECRET_MARKER.hex())
    (stamp / "key").chmod(0o400)
    (volume / "..data").symlink_to(stamp.name)
    (volume / "key").symlink_to(Path("..data") / "key")
    assert _admit(volume / "key") == _expected()
    (stamp / "key").chmod(0o644)  # the kubelet default, deliberately refused
    assert _refusal(volume / "key").code == UNSAFE


# -- non-regular files -----------------------------------------------------------------------


def _fifo_with_writer(path: Path, mode: int, payload: bytes) -> threading.Thread:
    os.mkfifo(path)
    path.chmod(mode)

    def write() -> None:
        try:
            with open(path, "wb") as sink:
                sink.write(payload)
        except BrokenPipeError:
            pass  # the reader refused and closed first

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    return writer


def test_owner_only_fifo_is_read_like_a_file(tmp_path):
    writer = _fifo_with_writer(tmp_path / "fifo", 0o600, SECRET_MARKER.hex().encode())
    assert _admit(tmp_path / "fifo") == _expected()
    writer.join(5)
    assert not writer.is_alive()


def test_world_readable_fifo_is_refused(tmp_path):
    writer = _fifo_with_writer(tmp_path / "fifo", 0o644, SECRET_MARKER.hex().encode())
    assert _refusal(tmp_path / "fifo").code == UNSAFE
    writer.join(5)
    assert not writer.is_alive()


def test_dev_null_is_refused_as_unsafe():
    assert stat.S_IMODE(os.stat("/dev/null").st_mode) & 0o077  # precondition: 0666 device
    assert _refusal("/dev/null").code == UNSAFE


# -- check-then-use: the verdict and the bytes come from one descriptor --------------------------


def _swap(path: Path, mode: int, material: bytes) -> None:
    replacement = path.with_name("replacement.tmp")
    replacement.write_text(material.hex())
    replacement.chmod(mode)
    os.replace(replacement, path)


def _hook_after(monkeypatch, name: str, path: Path, mode: int, material: bytes) -> list[int]:
    real = getattr(os, name)
    fired: list[int] = []

    def hooked(*args, **kwargs):
        result = real(*args, **kwargs)
        if not fired:
            fired.append(1)
            _swap(path, mode, material)
        return result

    monkeypatch.setattr(os, name, hooked)
    return fired


def test_path_swapped_to_unsafe_file_after_open_reads_the_opened_file(tmp_path, monkeypatch):
    path = _secret_file(tmp_path, 0o600)
    fired = _hook_after(monkeypatch, "open", path, 0o644, OTHER_SECRET)
    assert _admit(path) == _expected()
    assert fired  # positive control: the swap happened between open and the check


def test_path_swapped_to_safe_file_after_open_still_refuses_the_opened_file(tmp_path, monkeypatch):
    path = _secret_file(tmp_path, 0o644)
    fired = _hook_after(monkeypatch, "open", path, 0o600, OTHER_SECRET)
    assert _refusal(path).code == UNSAFE
    assert fired


def test_path_swapped_after_the_check_does_not_change_the_bytes_read(tmp_path, monkeypatch):
    path = _secret_file(tmp_path, 0o600)
    fired = _hook_after(monkeypatch, "fstat", path, 0o644, OTHER_SECRET)
    assert _admit(path) == _expected()
    assert fired  # positive control: the swap happened between the check and the read


# -- descriptor lifetime -------------------------------------------------------------------------


def _open_fds() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc/self/fd")
@pytest.mark.parametrize("case", ["admitted", "unsafe", "directory"])
def test_the_descriptor_is_closed_on_every_exit(tmp_path, monkeypatch, case):
    if case == "directory":
        path = tmp_path / "d"
        path.mkdir()
    else:
        path = _secret_file(tmp_path, 0o600 if case == "admitted" else 0o644)
    opened: list[int] = []
    closed: list[int] = []
    real_open, real_close = os.open, os.close

    def spy_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def spy_close(fd):
        closed.append(fd)
        return real_close(fd)

    before = _open_fds()
    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "close", spy_close)
    try:
        _admit(path)
    except MaskSecretError:
        pass
    monkeypatch.undo()
    assert opened and set(opened) <= set(closed)
    assert _open_fds() <= before


# -- Windows is not checked ------------------------------------------------------------------


def test_windows_skips_the_mode_check(tmp_path, monkeypatch):
    path = _secret_file(tmp_path, 0o644)
    ref = parse_ref(f"file:{path}")
    with pytest.raises(MaskSecretError) as posix:
        FileSecretProvider()._read(ref)
    assert posix.value.code == UNSAFE  # positive control: the same file is refused on POSIX
    outcome: bytes | BaseException
    # Patched for the one call only: pytest itself cannot report a failure while os.name is "nt".
    with monkeypatch.context() as patched:
        patched.setattr(os, "name", "nt")
        try:
            outcome = FileSecretProvider()._read(ref)
        except MaskSecretError as exc:
            outcome = exc
    assert outcome == SECRET_MARKER.hex().encode()


# -- registered, rebuilt from the code, value- and path-free -----------------------------------


def test_the_code_is_registered_with_the_secret_provider_semantics():
    spec = reasons.find("runtime", UNSAFE)
    assert spec is not None
    assert reasons.runtime.SECRET_FILE_PERMISSIONS_UNSAFE == UNSAFE
    rule = spec.default_rule
    assert rule.retry is reasons.RetryClass.CONFIGURATION_REPAIR
    assert rule.outcome is reasons.Outcome.CONFIGURATION
    sibling = reasons.find("runtime", "secret_material_invalid")
    assert spec.rules == sibling.rules and spec.owner == sibling.owner


def test_the_admission_boundary_rebuilds_it_from_the_code_alone():
    rebuilt = error_for((UNSAFE, ""))
    assert rebuilt.code == UNSAFE
    assert type(rebuilt).__name__ == "SecretFilePermissionsUnsafe"


@pytest.mark.parametrize("case", ["unsafe", "symlink", "directory", "missing"])
def test_refusals_carry_no_path_mode_uid_or_secret(tmp_path, caplog, capsys, case):
    if case == "unsafe":
        path = _secret_file(tmp_path, 0o644)
    elif case == "symlink":
        path = tmp_path / PATH_MARKER / "link.key"
        _secret_file(tmp_path, 0o640)
        path.symlink_to(path.with_name("mask-640.key"))
    elif case == "directory":
        path = tmp_path / PATH_MARKER / "dir.key"
        path.mkdir(parents=True)
    else:
        path = tmp_path / PATH_MARKER / "absent.key"
    exc = _engine_frames_only(_boxed_refusal(path))
    assert exc.code == (MISSING if case in ("directory", "missing") else UNSAFE)
    assert exc.__cause__ is None and exc.__context__ is None
    surfaces = render_everywhere(exc, caplog, capsys)
    needles = [
        PATH_MARKER,
        str(path),
        SECRET_MARKER.decode(),
        SECRET_MARKER.hex(),
        "0o644",
        "0o640",
        "0644",
        "0640",
        "100644",
        "rw-r--r--",
        "st_mode",
        "st_uid",
    ]
    assert_absent(surfaces, needles)
    # Constant text only: no mode, uid or other per-file detail in the message or its args.
    assert exc.args == (f"{exc.code}: {exc.message}",)
    assert not any(ch.isdigit() for ch in exc.message)


def test_an_admitted_file_secret_leaves_no_path_in_the_config_path_either(tmp_path, caplog):
    path = _secret_file(tmp_path, 0o644)
    with pytest.raises(MaskSecretError) as caught:
        admit_mask_key(keyed_config(tmp_path, mask_secret_ref=f"file:{path}"))
    assert caught.value.code == UNSAFE
    assert PATH_MARKER not in str(caught.value) + repr(caught.value) + caplog.text
    prepare_ref(f"file:{_secret_file(tmp_path, 0o600)}").close()  # positive control
