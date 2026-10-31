"""Sentry + dry-run for the native-companion RELEASE workflow.

GitHub Actions is off for this repo (billing), so the signed-release pipeline in
`.github/workflows/native-companion-release.yml` cannot be exercised there. Its
shell logic therefore lives in `scripts/release/native_release_checks.sh`, which
the workflow calls and this file runs locally against a THROWAWAY Ed25519 key
generated in the test (never the real release key, nothing is published).

Two layers:

1. Structural sentry over the workflow YAML: the release-minting job is the only
   job with `contents: write` (root is read-only), it runs under a protected
   environment, nothing builds until the commit is proven an ancestor of
   `origin/main`, every signed file is verified against the COMMITTED public key
   before the release exists, the release is created as a draft and un-drafted
   only after every asset uploaded, and the private key never leaves one job.
2. Functional dry-run of the script: sign/verify round trip, tamper and wrong-key
   rejection, placeholder-key refusal, key-matches-committed, version agreement
   (tag == pyproject == Cargo == every wheel), and the exact 12-wheel target set.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "native-companion-release.yml"
SCRIPT = ROOT / "scripts" / "release" / "native_release_checks.sh"
PUBKEY = ROOT / ".github" / "release-keys" / "decoy-release-key.pub.pem"
PLACEHOLDER = "__DECOY_RELEASE_PUBLIC_KEY__"

PYTHONS = ("cp310", "cp311", "cp312")
PLATFORMS = (
    "manylinux_2_28_x86_64",
    "manylinux_2_28_aarch64",
    "win_amd64",
    "macosx_11_0_arm64",
)

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
# Resolved once so every subprocess argv is a fixed absolute executable + test literals.
BASH = shutil.which("bash") or "/bin/bash"
GIT = shutil.which("git") or "/usr/bin/git"
OPENSSL = shutil.which("openssl") or "/usr/bin/openssl"


# --- helpers -----------------------------------------------------------------


def _wf() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _run(
    *args: str, cwd: Path | None = None, env: dict | None = None
) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(  # noqa: S603 -- static argv built from test literals
        [BASH, str(SCRIPT), *args],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


def _keypair(tmp: Path, name: str = "k") -> tuple[Path, Path]:
    """A throwaway Ed25519 keypair (PKCS8 private + SPKI public), via openssl."""
    priv = tmp / f"{name}.key.pem"
    pub = tmp / f"{name}.pub.pem"
    subprocess.run([OPENSSL, "genpkey", "-algorithm", "ed25519", "-out", str(priv)], check=True)  # noqa: S603
    subprocess.run([OPENSSL, "pkey", "-in", str(priv), "-pubout", "-out", str(pub)], check=True)  # noqa: S603
    return priv, pub


def _wheel_dir(tmp: Path, version: str = "0.1.0", *, names: list[str] | None = None) -> Path:
    """A fake release set: 12 wheels (or the given names) + SHA256SUMS."""
    d = tmp / "wheels"
    d.mkdir(parents=True)
    if names is None:
        names = [
            f"decoy_engine_native-{version}-{py}-{py}-{plat}.whl"
            for py in PYTHONS
            for plat in PLATFORMS
        ]
    for n in names:
        (d / n).write_bytes(f"wheel bytes for {n}\n".encode())
    lines = []
    for n in sorted(names):
        digest = hashlib.sha256((d / n).read_bytes()).hexdigest()
        lines.append(f"{digest}  {n}")
    (d / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return d


def _native_dir(tmp: Path, py_version: str = "0.1.0", cargo_version: str | None = None) -> Path:
    d = tmp / "decoy-engine-native"
    d.mkdir(parents=True)
    (d / "pyproject.toml").write_text(
        f'[project]\nname = "decoy-engine-native"\nversion = "{py_version}"\n', encoding="utf-8"
    )
    (d / "Cargo.toml").write_text(
        f'[package]\nname = "decoy-engine-native"\nversion = "{cargo_version or py_version}"\n',
        encoding="utf-8",
    )
    return d


# --- 1. structural sentry over the workflow ----------------------------------


def test_workflow_and_script_and_committed_key_exist() -> None:
    assert WORKFLOW.exists()
    assert SCRIPT.exists() and os.access(SCRIPT, os.X_OK), "script must be executable"
    assert PUBKEY.exists()


def test_committed_public_key_is_the_placeholder_until_launch() -> None:
    # The engine carries its own copy of the release public key (the CLI repo's
    # release_key.pub.pem is the other copy; both are filled at launch with the
    # same key). Until then it is the placeholder and the workflow must refuse it.
    text = PUBKEY.read_text(encoding="utf-8")
    assert text.startswith("-----BEGIN PUBLIC KEY-----")
    assert PLACEHOLDER in text


def test_root_permissions_are_read_only_and_only_sign_publish_writes() -> None:
    wf = _wf()
    assert wf["permissions"] == {"contents": "read"}
    writers = {
        name
        for name, job in wf["jobs"].items()
        if (job.get("permissions") or {}).get("contents") == "write"
    }
    assert writers == {"sign-publish"}
    for name, job in wf["jobs"].items():
        perms = job.get("permissions") or {}
        assert set(perms) <= {"contents"}, (name, perms)


def test_sign_publish_runs_under_a_protected_environment() -> None:
    job = _wf()["jobs"]["sign-publish"]
    env = job.get("environment")
    name = env if isinstance(env, str) else (env or {}).get("name")
    assert name == "native-release"


def test_nothing_builds_before_the_ancestor_of_main_check() -> None:
    wf = _wf()
    jobs = wf["jobs"]
    assert "preflight" in jobs
    run_text = "\n".join(s.get("run", "") for s in jobs["preflight"]["steps"])
    assert "assert-on-main" in run_text, "preflight must call the script's ancestor-of-main check"
    checkout = [
        s
        for s in jobs["preflight"]["steps"]
        if str(s.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkout and checkout[0].get("with", {}).get("fetch-depth") == 0, (
        "ancestor check needs full history"
    )
    for name, job in jobs.items():
        if name == "preflight":
            continue
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else list(needs)
        assert "preflight" in needs, f"job {name} does not depend on preflight"


def test_every_job_has_a_timeout() -> None:
    missing = [n for n, j in _wf()["jobs"].items() if "timeout-minutes" not in j]
    assert not missing, missing


def test_signing_key_is_confined_to_sign_publish_and_handled_hygienically() -> None:
    wf = _wf()
    for name, job in wf["jobs"].items():
        text = yaml.safe_dump(job)
        if name != "sign-publish":
            assert "DECOY_RELEASE_KEY_PEM" not in text, (
                f"key referenced outside sign-publish: {name}"
            )
    sign_steps = [
        s
        for s in wf["jobs"]["sign-publish"]["steps"]
        if "DECOY_RELEASE_KEY_PEM" in yaml.safe_dump(s)
    ]
    assert len(sign_steps) == 1, "exactly one step may see the key"
    run = sign_steps[0]["run"]
    assert "umask 077" in run
    assert "trap " in run and "EXIT" in run
    assert "printf '%s\\n'" in run
    assert "printf '%s' \"$DECOY_RELEASE_KEY_PEM\"" not in run


def test_sign_publish_verifies_version_set_and_signatures_before_creating_the_release() -> None:
    steps = _wf()["jobs"]["sign-publish"]["steps"]
    runs = [(i, s.get("run", "")) for i, s in enumerate(steps)]

    def first(sub: str) -> int:
        hits = [i for i, r in runs if sub in r]
        assert hits, f"no step runs `{sub}`"
        return hits[0]

    release = [
        i
        for i, s in enumerate(steps)
        if str(s.get("uses", "")).startswith("softprops/action-gh-release@")
    ]
    assert len(release) == 1
    for sub in ("assert-key-matches", "check-version", "check-wheel-set", "verify-signatures"):
        assert first(sub) < release[0], f"`{sub}` must run before the release is created"
    # verify runs AFTER signing and against the committed key, not a key derived
    # from the secret (a self-check with the private key proves nothing).
    assert first("verify-signatures") > first(" sign ")
    verify_run = runs[first("verify-signatures")][1]
    assert ".github/release-keys/decoy-release-key.pub.pem" in verify_run


def test_release_is_a_draft_until_every_asset_is_attached() -> None:
    steps = _wf()["jobs"]["sign-publish"]["steps"]
    rel = [s for s in steps if str(s.get("uses", "")).startswith("softprops/action-gh-release@")]
    assert rel[0]["with"].get("draft") is True
    assert rel[0]["with"].get("fail_on_unmatched_files") is True
    idx = steps.index(rel[0])
    later = "\n".join(s.get("run", "") for s in steps[idx + 1 :])
    assert "gh release edit" in later and "--draft=false" in later


def test_third_party_actions_in_the_signing_job_are_sha_pinned() -> None:
    # The job that holds the key may only run first-party (actions/*) steps or
    # third-party steps pinned to a full commit SHA.
    for s in _wf()["jobs"]["sign-publish"]["steps"]:
        uses = s.get("uses")
        if not uses or uses.startswith("actions/"):
            continue
        ref = uses.split("@", 1)[1]
        assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), uses


# --- 2. functional dry-run of the script with a throwaway key ----------------


def test_sign_then_verify_round_trip(tmp_path: Path) -> None:
    priv, pub = _keypair(tmp_path)
    d = _wheel_dir(tmp_path)
    assert _run("sign", "--privkey", str(priv), "--dir", str(d)).returncode == 0
    assert sorted(p.name for p in d.glob("*.sig")) == sorted(
        f"{p.name}.sig" for p in d.iterdir() if not p.name.endswith(".sig")
    )
    r = _run("verify-signatures", "--pubkey", str(pub), "--dir", str(d))
    assert r.returncode == 0, r.stderr
    # the script must not write raw-sig scratch files next to the release assets
    assert not list(d.glob("*.raw"))


def test_verify_rejects_a_tampered_file(tmp_path: Path) -> None:
    priv, pub = _keypair(tmp_path)
    d = _wheel_dir(tmp_path)
    _run("sign", "--privkey", str(priv), "--dir", str(d))
    target = next(d.glob("*cp311*win_amd64.whl"))
    target.write_bytes(target.read_bytes() + b"x")
    r = _run("verify-signatures", "--pubkey", str(pub), "--dir", str(d))
    assert r.returncode != 0
    assert target.name in r.stderr


def test_verify_rejects_a_tampered_manifest(tmp_path: Path) -> None:
    priv, pub = _keypair(tmp_path)
    d = _wheel_dir(tmp_path)
    _run("sign", "--privkey", str(priv), "--dir", str(d))
    # the manifest's own signature still matches, but a line no longer matches
    # the file it describes: sha256sum -c must catch it. Re-sign the edited
    # manifest so this proves the checksum cross-check, not the signature.
    lines = (d / "SHA256SUMS").read_text().splitlines()
    lines[0] = "0" * 64 + lines[0][64:]
    (d / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    (d / "SHA256SUMS.sig").unlink()
    _run("sign", "--privkey", str(priv), "--dir", str(d))
    r = _run("verify-signatures", "--pubkey", str(pub), "--dir", str(d))
    assert r.returncode != 0


def test_verify_rejects_signatures_from_another_key(tmp_path: Path) -> None:
    priv, _pub = _keypair(tmp_path, "signer")
    _priv2, pub2 = _keypair(tmp_path, "other")
    d = _wheel_dir(tmp_path)
    _run("sign", "--privkey", str(priv), "--dir", str(d))
    assert _run("verify-signatures", "--pubkey", str(pub2), "--dir", str(d)).returncode != 0


def test_verify_refuses_the_placeholder_public_key(tmp_path: Path) -> None:
    priv, _pub = _keypair(tmp_path)
    d = _wheel_dir(tmp_path)
    _run("sign", "--privkey", str(priv), "--dir", str(d))
    r = _run("verify-signatures", "--pubkey", str(PUBKEY), "--dir", str(d))
    assert r.returncode != 0
    assert "placeholder" in r.stderr.lower()


def test_verify_rejects_a_missing_or_stray_signature(tmp_path: Path) -> None:
    priv, pub = _keypair(tmp_path)
    d = _wheel_dir(tmp_path)
    _run("sign", "--privkey", str(priv), "--dir", str(d))
    sig = next(d.glob("*.whl.sig"))
    sig.unlink()
    assert _run("verify-signatures", "--pubkey", str(pub), "--dir", str(d)).returncode != 0
    _run("sign", "--privkey", str(priv), "--dir", str(d))
    (d / "ghost.whl.sig").write_text("AAAA")
    assert _run("verify-signatures", "--pubkey", str(pub), "--dir", str(d)).returncode != 0


def test_verify_rejects_an_empty_release_set(tmp_path: Path) -> None:
    _priv, pub = _keypair(tmp_path)
    d = tmp_path / "wheels"
    d.mkdir()
    assert _run("verify-signatures", "--pubkey", str(pub), "--dir", str(d)).returncode != 0


def test_assert_key_matches_committed(tmp_path: Path) -> None:
    priv, pub = _keypair(tmp_path, "a")
    _priv2, pub2 = _keypair(tmp_path, "b")
    assert _run("assert-key-matches", "--pubkey", str(pub), "--privkey", str(priv)).returncode == 0
    assert _run("assert-key-matches", "--pubkey", str(pub2), "--privkey", str(priv)).returncode != 0
    r = _run("assert-key-matches", "--pubkey", str(PUBKEY), "--privkey", str(priv))
    assert r.returncode != 0 and "placeholder" in r.stderr.lower()


def test_check_version_agrees_everywhere(tmp_path: Path) -> None:
    native = _native_dir(tmp_path)
    d = _wheel_dir(tmp_path)
    ok = _run("check-version", "--expected", "0.1.0", "--native-dir", str(native), "--dir", str(d))
    assert ok.returncode == 0, ok.stderr
    assert (
        _run(
            "check-version", "--expected", "0.2.0", "--native-dir", str(native), "--dir", str(d)
        ).returncode
        != 0
    )


def test_check_version_without_wheels_checks_the_tree_only(tmp_path: Path) -> None:
    native = _native_dir(tmp_path)
    assert _run("check-version", "--expected", "0.1.0", "--native-dir", str(native)).returncode == 0
    assert _run("check-version", "--expected", "0.1.1", "--native-dir", str(native)).returncode != 0
    # but once a --dir is given it must hold at least one wheel
    empty = tmp_path / "empty"
    empty.mkdir()
    assert (
        _run(
            "check-version", "--expected", "0.1.0", "--native-dir", str(native), "--dir", str(empty)
        ).returncode
        != 0
    )


def test_check_version_rejects_cargo_or_wheel_drift(tmp_path: Path) -> None:
    native = _native_dir(tmp_path, "0.1.0", cargo_version="0.1.1")
    d = _wheel_dir(tmp_path)
    assert (
        _run(
            "check-version", "--expected", "0.1.0", "--native-dir", str(native), "--dir", str(d)
        ).returncode
        != 0
    )
    native2 = _native_dir(tmp_path / "n2", "0.1.0")
    bad = d / "decoy_engine_native-0.9.9-cp310-cp310-win_amd64.whl"
    next(d.glob("*cp310*win_amd64.whl")).rename(bad)
    assert (
        _run(
            "check-version", "--expected", "0.1.0", "--native-dir", str(native2), "--dir", str(d)
        ).returncode
        != 0
    )


def test_check_version_rejects_a_non_pep440_looking_tag(tmp_path: Path) -> None:
    native = _native_dir(tmp_path)
    d = _wheel_dir(tmp_path)
    r = _run("check-version", "--expected", "v0.1.0", "--native-dir", str(native), "--dir", str(d))
    assert r.returncode != 0


def test_check_wheel_set_requires_exactly_the_twelve_targets(tmp_path: Path) -> None:
    d = _wheel_dir(tmp_path)
    assert _run("check-wheel-set", "--dir", str(d)).returncode == 0
    # 11: one target missing
    missing = next(d.glob("*cp312*aarch64.whl"))
    missing.unlink()
    r = _run("check-wheel-set", "--dir", str(d))
    assert r.returncode != 0 and "cp312" in r.stderr


def test_check_wheel_set_rejects_two_builds_of_one_cell(tmp_path: Path) -> None:
    # Two macOS wheels for cp312 (different deployment targets) both match the
    # macosx_*_arm64 cell: the per-cell exactness check must name it, not only
    # the total count.
    names = [
        f"decoy_engine_native-0.1.0-{py}-{py}-{plat}.whl" for py in PYTHONS for plat in PLATFORMS
    ]
    names.append("decoy_engine_native-0.1.0-cp312-cp312-macosx_12_0_arm64.whl")
    d = _wheel_dir(tmp_path, names=names)
    r = _run("check-wheel-set", "--dir", str(d))
    assert r.returncode != 0
    assert "expected exactly 1 wheel for cp312/macosx_*_arm64, found 2" in r.stderr


def test_check_wheel_set_rejects_an_unexpected_platform_or_python(tmp_path: Path) -> None:
    d = _wheel_dir(tmp_path)
    (d / "decoy_engine_native-0.1.0-cp313-cp313-win_amd64.whl").write_bytes(b"x")
    assert _run("check-wheel-set", "--dir", str(d)).returncode != 0
    d2 = _wheel_dir(tmp_path / "two")
    (d2 / "decoy_engine_native-0.1.0-cp310-cp310-manylinux_2_28_i686.whl").write_bytes(b"x")
    assert _run("check-wheel-set", "--dir", str(d2)).returncode != 0


def test_check_wheel_set_cross_checks_the_manifest(tmp_path: Path) -> None:
    d = _wheel_dir(tmp_path)
    lines = (d / "SHA256SUMS").read_text().splitlines()
    (d / "SHA256SUMS").write_text("\n".join(lines[1:]) + "\n")
    assert _run("check-wheel-set", "--dir", str(d)).returncode != 0


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 -- static argv built from test literals
        [GIT, "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_assert_on_main_accepts_only_ancestors_of_main(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a").write_text("a")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "one")
    on_main = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "b").write_text("b")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "side")
    off_main = _git(repo, "rev-parse", "HEAD")
    assert _run("assert-on-main", "--sha", on_main, "--main-ref", "main", cwd=repo).returncode == 0
    r = _run("assert-on-main", "--sha", off_main, "--main-ref", "main", cwd=repo)
    assert r.returncode != 0 and "ancestor" in r.stderr.lower()


def test_script_refuses_unknown_subcommands_and_missing_args(tmp_path: Path) -> None:
    assert _run("frobnicate").returncode != 0
    assert _run("verify-signatures").returncode != 0
    assert _run("check-version", "--expected", "0.1.0").returncode != 0
