"""A9: artifact substitution and path handling, on real files (no mocks of the filesystem)."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from decoy_engine.signed_evidence import ArtifactStatus, ExitClass, verify_signed_evidence
from tests.unit.signed_evidence import _corpus

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file semantics")


def _verify(vector_id: str, root: Path):
    vec = _corpus.load(vector_id)
    return verify_signed_evidence(
        statement=vec.statement,
        signature=vec.signature,
        artifact_root=root,
        context=_corpus.build_context(vec),
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return _corpus.materialize_artifacts(_corpus.load("valid_release_openssl"), tmp_path / "art")


def test_control_clean_tree_verifies(root: Path) -> None:
    r = _verify("valid_release_openssl", root)
    assert r.artifacts is ArtifactStatus.MATCH and r.exit_class is ExitClass.OK


def test_symlink_to_identical_bytes_elsewhere_is_refused(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.csv"
    shutil.copy(root / "outputs/customers.csv", outside)
    (root / "outputs/customers.csv").unlink()
    (root / "outputs/customers.csv").symlink_to(outside)
    r = _verify("valid_release_openssl", root)
    assert r.artifacts is not ArtifactStatus.MATCH
    assert "ARTIFACT_PATH_UNSAFE" in r.reasons and not r.exit_class.is_success


def test_symlinked_directory_component_is_refused(root: Path, tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    shutil.copytree(root / "outputs", real)
    shutil.rmtree(root / "outputs")
    (root / "outputs").symlink_to(real, target_is_directory=True)
    r = _verify("valid_release_openssl", root)
    assert r.artifacts is not ArtifactStatus.MATCH and "ARTIFACT_PATH_UNSAFE" in r.reasons


def test_fifo_in_place_of_file_is_refused_without_blocking(root: Path) -> None:
    p = root / "outputs/quarantine.ndjson"
    p.unlink()
    os.mkfifo(p)
    r = _verify("valid_release_openssl", root)  # must return, not hang opening a FIFO
    assert r.artifacts is not ArtifactStatus.MATCH and "ARTIFACT_NOT_REGULAR_FILE" in r.reasons


def test_directory_in_place_of_file_is_refused(root: Path) -> None:
    p = root / "outputs/quarantine.ndjson"
    p.unlink()
    p.mkdir()
    r = _verify("valid_release_openssl", root)
    assert r.artifacts is not ArtifactStatus.MATCH and "ARTIFACT_NOT_REGULAR_FILE" in r.reasons


def test_unbound_symlink_extra_is_reported_not_followed(root: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read")
    (root / "outputs/leak.csv").symlink_to(secret)
    r = _verify("valid_release_openssl", root)
    assert r.artifacts is ArtifactStatus.UNEXPECTED
    assert "outputs/leak.csv" in r.unbound_files
    assert r.release.name == "DENY"


def test_replaced_after_hash_is_detected_by_a_second_pass(root: Path) -> None:
    """A mutable replacement between two verifications must not ride on the first result."""
    first = _verify("valid_release_openssl", root)
    assert first.exit_class is ExitClass.OK
    p = root / "outputs/customers.csv"
    data = bytearray(p.read_bytes())
    data[0] ^= 0x01
    p.write_bytes(bytes(data))
    second = _verify("valid_release_openssl", root)
    assert second.artifacts is ArtifactStatus.MISMATCH


def test_unreadable_file_is_unavailable_not_pass(root: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses file modes")
    p = root / "outputs/orders.parquet"
    p.chmod(0)
    try:
        r = _verify("valid_release_openssl", root)
    finally:
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert r.artifacts is ArtifactStatus.UNAVAILABLE and not r.exit_class.is_success


def test_missing_root_is_incomplete_not_a_crash(tmp_path: Path) -> None:
    r = _verify("valid_release_openssl", tmp_path / "does-not-exist")
    assert r.artifacts is ArtifactStatus.MISSING and not r.exit_class.is_success


def test_root_that_is_a_file_is_refused(tmp_path: Path) -> None:
    f = tmp_path / "file"
    f.write_text("x")
    r = _verify("valid_release_openssl", f)
    assert r.artifacts is not ArtifactStatus.MATCH


def test_large_file_is_streamed_and_bounded(root: Path) -> None:
    """A file far larger than the signed length is a mismatch decided from metadata, not read whole."""
    p = root / "outputs/customers.csv"
    with p.open("ab") as fh:
        fh.truncate(200 * 1024 * 1024)  # sparse; reading it all would be slow
    r = _verify("valid_release_openssl", root)
    assert r.artifacts is ArtifactStatus.MISMATCH
    assert "ARTIFACT_LENGTH_MISMATCH" in r.reasons


def test_signed_length_wrong_even_when_prefix_hashes_equal(root: Path) -> None:
    p = root / "outputs/customers.csv"
    p.write_bytes(p.read_bytes() + b"\n")
    r = _verify("valid_release_openssl", root)
    assert r.artifacts is ArtifactStatus.MISMATCH


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
@pytest.mark.skipif(
    os.geteuid() != 0,
    reason="drops privileges from root; the non-root variant above covers other users",
)
def test_unreadable_file_is_unavailable_even_when_the_suite_runs_as_root() -> None:
    """Root bypasses file modes, which would skip the test above; verify as `nobody` in a forked child."""
    import json
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="f33-unreadable-"))
    try:
        base.chmod(0o755)
        root = _corpus.materialize_artifacts(_corpus.load("valid_release_openssl"), base / "art")
        for p in [base / "art", *root.rglob("*")]:
            p.chmod(0o755 if p.is_dir() else 0o644)
        (root / "outputs/orders.parquet").chmod(0o000)
        vec = _corpus.load("valid_release_openssl")
        context = _corpus.build_context(vec)
        # Warm every lazily imported module while still root; `nobody` cannot read the interpreter's libs.
        verify_signed_evidence(
            statement=vec.statement, signature=vec.signature, artifact_root=root, context=context
        )
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - runs in the child
            code = 1
            try:
                os.close(read_fd)
                os.setgroups([])
                os.setgid(65534)
                os.setuid(65534)
                r = verify_signed_evidence(
                    statement=vec.statement,
                    signature=vec.signature,
                    artifact_root=root,
                    context=context,
                )
                os.write(
                    write_fd,
                    json.dumps(
                        {"a": r.artifacts.name, "e": r.exit_class.name, "r": list(r.reasons)}
                    ).encode(),
                )
                code = 0
            finally:
                os._exit(code)
        os.close(write_fd)
        with os.fdopen(read_fd, "rb") as fh:
            payload = fh.read()
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, (
            "the unprivileged child failed to run"
        )
        out = json.loads(payload)
        assert out["a"] == "UNAVAILABLE" and out["e"] == "INCOMPLETE"
        assert "ARTIFACT_UNREADABLE" in out["r"]
    finally:
        for p in [*base.rglob("*"), base]:
            if p.exists() and not p.is_symlink():
                p.chmod(0o755 if p.is_dir() else 0o644)
        import shutil

        shutil.rmtree(base, ignore_errors=True)
