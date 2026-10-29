"""A8/A9: private staging, atomic visibility, crash injection and substitution, on real files."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from decoy_engine.errors import DecoyError
from decoy_engine.evidence_release import (
    ArtifactSpec,
    ReleaseConflict,
    ReleaseDenied,
    ReleaseError,
    Stage,
    StagingCoordinator,
)
from decoy_engine.signed_evidence import (
    ExitClass,
    Purpose,
    SignedEvidenceError,
    verify_signed_evidence,
)
from decoy_engine.signed_evidence._release import StrictReleaseEvaluator
from tests.unit.signed_evidence import _factory as f

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX staging semantics")

SPECS = tuple(ArtifactSpec(id=a, path=p, media_type=m) for a, p, m in f.ARTIFACT_SPECS)


def _draft() -> dict:
    d = f.base_draft()
    for key in ("artifacts", "publication"):
        del d[key]
    return d


@pytest.fixture
def signer():
    return f.new_signer()


@pytest.fixture
def policy(signer):
    return f.policy_for(signer)


def _coordinator(tmp_path, signer, *, evaluator=None, failpoint=None) -> StagingCoordinator:
    return StagingCoordinator(
        root=tmp_path / "out",
        signer=signer,
        evaluator=evaluator,
        failpoint=failpoint,
    )


def _stage(area) -> None:
    for rel, data in f.base_artifacts().items():
        area.write_artifact(rel, data)


def _publish(area, policy, **kw):
    return area.publish(_draft(), SPECS, context=f.context(policy), policy_generation="g1", **kw)


def _evidence_files(root: Path) -> list[Path]:
    return sorted(root.rglob("evidence*.json")) if root.exists() else []


def test_errors_are_engine_errors() -> None:
    assert issubclass(ReleaseError, DecoyError)
    assert issubclass(ReleaseDenied, ReleaseError) and issubclass(ReleaseConflict, ReleaseError)


def test_default_evaluator_denies_so_nothing_is_released(tmp_path, signer, policy) -> None:
    coord = _coordinator(tmp_path, signer)  # no evaluator => deny-by-default
    area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    with pytest.raises(ReleaseDenied) as ei:
        _publish(area, policy)
    assert "RELEASE_EVALUATOR_NOT_ENABLED" in ei.value.reasons
    assert not (tmp_path / "out" / "releases").exists()
    assert _evidence_files(tmp_path / "out") == []
    assert not any((tmp_path / "out" / ".staging").glob("*")), "denied candidate must be discarded"


def test_publish_is_complete_signed_and_verifiable(tmp_path, signer, policy) -> None:
    coord = _coordinator(tmp_path, signer, evaluator=StrictReleaseEvaluator())
    area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    rel = _publish(area, policy)
    assert rel.path == tmp_path / "out" / "releases" / "job-1001" / "attempt-1"
    assert sorted(p.name for p in rel.path.iterdir()) == [
        "artifacts",
        "evidence.json",
        "evidence.sig.json",
    ]
    r = verify_signed_evidence(
        statement=(rel.path / "evidence.json").read_bytes(),
        signature=(rel.path / "evidence.sig.json").read_bytes(),
        artifact_root=rel.path / "artifacts",
        context=f.context(policy),
    )
    assert r.exit_class is ExitClass.OK
    assert not (tmp_path / "out" / ".staging").exists() or not list(
        (tmp_path / "out" / ".staging").iterdir()
    )


@pytest.mark.parametrize("stage", [s for s in Stage if s is not Stage.AFTER_COMMIT])
def test_crash_before_commit_exposes_nothing(tmp_path, signer, policy, stage) -> None:
    def boom(name: str) -> None:
        if name == stage.value:
            raise RuntimeError("injected crash")

    coord = _coordinator(tmp_path, signer, evaluator=StrictReleaseEvaluator(), failpoint=boom)
    area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    with pytest.raises(RuntimeError):
        _publish(area, policy)
    assert not (tmp_path / "out" / "releases").exists(), stage
    assert _evidence_files(tmp_path / "out" / "releases") == []
    # A retry is a fresh, complete attempt and does not inherit the crashed one.
    retry = _coordinator(tmp_path, signer, evaluator=StrictReleaseEvaluator()).begin(
        job_id="job-1001", attempt_id="attempt-1"
    )
    _stage(retry)
    assert _publish(retry, policy).path.exists()


def test_crash_after_commit_leaves_a_complete_verifiable_release(tmp_path, signer, policy) -> None:
    def boom(name: str) -> None:
        if name == Stage.AFTER_COMMIT.value:
            raise RuntimeError("injected crash")

    coord = _coordinator(tmp_path, signer, evaluator=StrictReleaseEvaluator(), failpoint=boom)
    area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    with pytest.raises(RuntimeError):
        _publish(area, policy)
    final = tmp_path / "out" / "releases" / "job-1001" / "attempt-1"
    r = verify_signed_evidence(
        statement=(final / "evidence.json").read_bytes(),
        signature=(final / "evidence.sig.json").read_bytes(),
        artifact_root=final / "artifacts",
        context=f.context(policy),
    )
    assert r.exit_class is ExitClass.OK
    again = _coordinator(tmp_path, signer, evaluator=StrictReleaseEvaluator()).begin(
        job_id="job-1001", attempt_id="attempt-1"
    )
    _stage(again)
    with pytest.raises(ReleaseConflict):
        _publish(again, policy)
    assert (final / "evidence.json").exists(), "the earlier release must be untouched by the retry"


@pytest.mark.parametrize("stage", [Stage.HASHED, Stage.SIGNED, Stage.CHECKED, Stage.BEFORE_COMMIT])
def test_substitution_between_seal_and_commit_is_refused(tmp_path, signer, policy, stage) -> None:
    """Whatever the moment, bytes that differ from the sealed inventory never become visible."""

    def swap(name: str) -> None:
        if name == stage.value:
            target = next((tmp_path / "out" / ".staging").rglob("customers.csv"))
            target.chmod(0o644)
            target.write_bytes(b"id,name\n1,Mallory\n2,Bob\n3,Chlo\xc3\xa9\n")

    coord = _coordinator(tmp_path, signer, evaluator=StrictReleaseEvaluator(), failpoint=swap)
    area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    with pytest.raises(ReleaseDenied):
        _publish(area, policy)
    assert not (tmp_path / "out" / "releases").exists()
    assert _evidence_files(tmp_path / "out") == []


@pytest.mark.parametrize("stage", [Stage.HASHED, Stage.SIGNED, Stage.CHECKED, Stage.BEFORE_COMMIT])
def test_symlink_swapped_in_after_seal_is_refused(tmp_path, signer, policy, stage) -> None:
    secret = tmp_path / "outside.csv"
    secret.write_bytes(f.base_artifacts()["outputs/customers.csv"])  # identical bytes elsewhere

    def swap(name: str) -> None:
        if name == stage.value:
            target = next((tmp_path / "out" / ".staging").rglob("customers.csv"))
            target.parent.chmod(0o755)
            target.unlink()
            target.symlink_to(secret)

    coord = _coordinator(tmp_path, signer, evaluator=StrictReleaseEvaluator(), failpoint=swap)
    area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    with pytest.raises(ReleaseError):
        _publish(area, policy)
    assert not (tmp_path / "out" / "releases").exists()


def test_unbound_staged_file_blocks_the_release(tmp_path, signer, policy) -> None:
    coord = _coordinator(tmp_path, signer, evaluator=StrictReleaseEvaluator())
    area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    area.write_artifact("outputs/extra.csv", b"surprise")
    with pytest.raises(ReleaseError) as ei:
        _publish(area, policy)
    assert ei.value.code == "STAGING_UNBOUND_FILE"
    assert not (tmp_path / "out" / "releases").exists()


def test_missing_declared_artifact_blocks_the_release(tmp_path, signer, policy) -> None:
    coord = _coordinator(tmp_path, signer, evaluator=StrictReleaseEvaluator())
    area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
    for rel, data in f.base_artifacts().items():
        if "orders" not in rel:
            area.write_artifact(rel, data)
    with pytest.raises(ReleaseError) as ei:
        _publish(area, policy)
    assert ei.value.code == "STAGING_ARTIFACT_MISSING"


@pytest.mark.parametrize(
    "bad", ["../escape.csv", "/abs.csv", "a/../../b.csv", "a\\b.csv", "", "a//b"]
)
def test_staging_paths_cannot_escape_the_area(tmp_path, signer, bad) -> None:
    area = _coordinator(tmp_path, signer).begin(job_id="job-1001", attempt_id="attempt-1")
    with pytest.raises((ReleaseError, SignedEvidenceError)):
        area.write_artifact(bad, b"x")
    assert not (tmp_path / "escape.csv").exists()


def test_symlinked_staging_subdirectory_is_refused(tmp_path, signer) -> None:
    area = _coordinator(tmp_path, signer).begin(job_id="job-1001", attempt_id="attempt-1")
    outside = tmp_path / "outside"
    outside.mkdir()
    (area.path / "artifacts" / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ReleaseError):
        area.write_artifact("link/x.csv", b"x")
    assert list(outside.iterdir()) == []


def test_staging_area_is_private_and_not_publicly_constructible(tmp_path, signer) -> None:
    coord = _coordinator(tmp_path, signer)
    area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
    assert (area.path.stat().st_mode & 0o777) == 0o700
    assert area.path.parent.name == ".staging"
    with pytest.raises(TypeError):
        type(area)()  # type: ignore[call-arg]


def test_sealed_files_and_directories_are_read_only(tmp_path, signer, policy) -> None:
    seen: dict[str, int] = {}

    def look(name: str) -> None:
        if name == Stage.HASHED.value:
            base = tmp_path / "out" / ".staging"
            for p in base.rglob("*"):
                if p.is_file() or (p.is_dir() and p.name in {"artifacts", "outputs"}):
                    seen[str(p.relative_to(base))] = p.stat().st_mode & 0o222

    coord = _coordinator(tmp_path, signer, evaluator=StrictReleaseEvaluator(), failpoint=look)
    area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    _publish(area, policy)
    kinds = {Path(k).name for k in seen}
    assert {"customers.csv", "orders.parquet", "quarantine.ndjson", "outputs", "artifacts"} <= kinds
    assert all(mode == 0 for mode in seen.values()), seen


def test_policy_generation_recheck_revokes_the_permit(tmp_path, signer, policy) -> None:
    coord = _coordinator(tmp_path, signer, evaluator=StrictReleaseEvaluator())
    area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    with pytest.raises(ReleaseDenied) as ei:
        _publish(area, policy, recheck=lambda permit: permit.policy_generation == "g2")
    assert "PERMIT_REVOKED" in ei.value.reasons
    assert not (tmp_path / "out" / "releases").exists()


def test_signed_rejection_exports_evidence_without_any_data(tmp_path, signer, policy) -> None:
    coord = _coordinator(tmp_path, signer, evaluator=StrictReleaseEvaluator())
    area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    d = _draft()
    d["checks"][1]["status"] = "INCONCLUSIVE"
    d["checks"][1]["reason_code"] = "DETECTOR_UNAVAILABLE"
    rej = area.reject(d, SPECS, reason_code="REQUIRED_CHECK_INCONCLUSIVE")
    assert rej.path == tmp_path / "out" / "rejections" / "job-1001" / "attempt-1"
    assert sorted(p.name for p in rej.path.iterdir()) == ["evidence.json", "evidence.sig.json"]
    assert not (tmp_path / "out" / "releases").exists()
    r = verify_signed_evidence(
        statement=(rej.path / "evidence.json").read_bytes(),
        signature=(rej.path / "evidence.sig.json").read_bytes(),
        artifact_root=None,
        context=f.context(policy),
    )
    assert r.signature.name == "VALID" and r.release.name == "DENY"
    assert "PUBLICATION_STATE_REJECTED" in r.reasons and r.artifacts.name in {
        "WITHHELD",
        "NOT_EVALUATED",
    }
    assert not any((tmp_path / "out" / ".staging").glob("*")), "withheld data is discarded"


def test_rejection_is_independent_of_release_capability(tmp_path, signer, policy) -> None:
    coord = _coordinator(tmp_path, signer)  # deny-all default evaluator
    area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    assert area.reject(_draft(), SPECS, reason_code="OPERATOR_REJECTED").path.exists()


def test_signing_unavailable_leaves_no_evidence_anywhere(tmp_path, policy) -> None:
    class Down:
        identity = f.new_signer().identity

        def public_jwk(self):
            return f.new_signer().public_jwk()

        def sign_signing_input(self, _):
            raise TimeoutError("kms unreachable arn:aws:kms:SECRET")

    coord = StagingCoordinator(
        root=tmp_path / "out", signer=Down(), evaluator=StrictReleaseEvaluator()
    )
    area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    with pytest.raises(ReleaseError) as ei:
        _publish(area, policy)
    assert ei.value.code == "SIGNING_UNAVAILABLE" and "SECRET" not in str(ei.value)
    area2 = coord.begin(job_id="job-1001", attempt_id="attempt-2")
    _stage(area2)
    with pytest.raises(ReleaseError):
        area2.reject(_draft(), SPECS, reason_code="OPERATOR_REJECTED")
    assert _evidence_files(tmp_path / "out") == []
    assert (
        not (tmp_path / "out" / "releases").exists()
        and not (tmp_path / "out" / "rejections").exists()
    )


def test_draft_must_not_carry_inventory_or_publication(tmp_path, signer) -> None:
    policy = f.policy_for(signer)
    for n, key in enumerate(("artifacts", "publication")):
        area = _coordinator(tmp_path, signer, evaluator=StrictReleaseEvaluator()).begin(
            job_id="job-1001", attempt_id=f"attempt-{n}"
        )
        _stage(area)
        d = _draft()
        d[key] = f.base_draft()[key]
        d["attempt_id"] = f"attempt-{n}"
        with pytest.raises(ReleaseError) as ei:
            area.publish(d, SPECS, context=f.context(policy), policy_generation="g")
        assert ei.value.code == "STAGING_DRAFT_INVALID"
        assert not (tmp_path / "out" / "releases").exists()


def test_draft_identity_must_match_the_area(tmp_path, signer) -> None:
    area = _coordinator(tmp_path, signer, evaluator=StrictReleaseEvaluator()).begin(
        job_id="job-1001", attempt_id="attempt-1"
    )
    _stage(area)
    d = _draft()
    d["attempt_id"] = "attempt-OTHER"
    with pytest.raises(ReleaseError) as ei:
        area.publish(d, SPECS, context=f.context(f.policy_for(signer)), policy_generation="g")
    assert ei.value.code == "STAGING_DRAFT_INVALID"


def test_a_failed_attempt_cannot_be_reused(tmp_path, signer, policy) -> None:
    area = _coordinator(tmp_path, signer).begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    with pytest.raises(ReleaseDenied):
        _publish(area, policy)
    with pytest.raises(ReleaseError) as ei:
        area.write_artifact("outputs/x.csv", b"x")
    assert ei.value.code == "STAGING_AREA_FINISHED"


def test_orphaned_staging_from_a_crashed_process_can_be_swept(tmp_path, signer) -> None:
    coord = _coordinator(tmp_path, signer)
    area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    os.utime(area.path, (1, 1))  # looks ancient
    fresh = coord.begin(job_id="job-1001", attempt_id="attempt-2")
    assert coord.sweep_orphans(older_than_seconds=3600) == 1
    assert not area.path.exists() and fresh.path.exists()


def test_export_of_existing_evidence_preserves_bytes_and_requires_verification(
    tmp_path, signer, policy
) -> None:
    coord = _coordinator(tmp_path, signer, evaluator=StrictReleaseEvaluator())
    area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    rel = _publish(area, policy)
    statement = (rel.path / "evidence.json").read_bytes()
    signature = (rel.path / "evidence.sig.json").read_bytes()
    ctx = f.context(policy, purpose=Purpose.CONSUMER_ACCEPTANCE)
    out = coord.export_verified(
        tmp_path / "export", statement=statement, signature=signature, context=ctx
    )
    assert (out / "evidence.json").read_bytes() == statement
    assert (out / "evidence.sig.json").read_bytes() == signature
    bad = bytearray(statement)
    bad[7] ^= 1
    with pytest.raises(ReleaseError):
        coord.export_verified(
            tmp_path / "export2", statement=bytes(bad), signature=signature, context=ctx
        )
    assert not (tmp_path / "export2").exists()


def test_an_existing_empty_target_directory_is_a_conflict_not_replaced(
    tmp_path, signer, policy
) -> None:
    """os.replace would silently swap an EMPTY directory; the contract is that a target is never replaced."""
    final = tmp_path / "out" / "releases" / "job-1001" / "attempt-1"
    final.mkdir(parents=True)
    coord = _coordinator(tmp_path, signer, evaluator=StrictReleaseEvaluator())
    area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    with pytest.raises(ReleaseConflict):
        _publish(area, policy)
    assert final.is_dir() and list(final.iterdir()) == []


def test_an_artifact_cannot_be_silently_overwritten_in_staging(tmp_path, signer) -> None:
    area = _coordinator(tmp_path, signer).begin(job_id="job-1001", attempt_id="attempt-1")
    area.write_artifact("outputs/customers.csv", b"first")
    with pytest.raises(ReleaseError) as ei:
        area.write_artifact("outputs/customers.csv", b"second")
    assert ei.value.code == "STAGING_ARTIFACT_EXISTS"
    assert (area.artifacts_dir / "outputs" / "customers.csv").read_bytes() == b"first"


def test_overwriting_through_a_pre_existing_symlink_is_refused(tmp_path, signer) -> None:
    area = _coordinator(tmp_path, signer).begin(job_id="job-1001", attempt_id="attempt-1")
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"precious")
    (area.artifacts_dir / "leak.csv").symlink_to(victim)
    with pytest.raises(ReleaseError):
        area.write_artifact("leak.csv", b"clobber")
    assert victim.read_bytes() == b"precious"


def test_a_failing_recheck_denies_rather_than_permits(tmp_path, signer, policy) -> None:
    def boom(_permit):
        raise ConnectionError("authorization service unreachable")

    coord = _coordinator(tmp_path, signer, evaluator=StrictReleaseEvaluator())
    area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    with pytest.raises(ReleaseDenied) as ei:
        _publish(area, policy, recheck=boom)
    assert "PERMIT_RECHECK_FAILED" in ei.value.reasons
    assert "unreachable" not in str(ei.value) + repr(ei.value.reasons)
    assert not (tmp_path / "out" / "releases").exists()


def test_a_recheck_returning_a_falsy_value_denies(tmp_path, signer, policy) -> None:
    for falsy in (False, None, 0, ""):
        coord = _coordinator(
            tmp_path / str(repr(falsy)), signer, evaluator=StrictReleaseEvaluator()
        )
        area = coord.begin(job_id="job-1001", attempt_id="attempt-1")
        _stage(area)
        with pytest.raises(ReleaseDenied):
            _publish(area, policy, recheck=lambda _p, v=falsy: v)
