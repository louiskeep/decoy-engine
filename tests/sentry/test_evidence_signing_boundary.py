"""Signing choke-point sentry (F33 plan section 2.2).

Three independent parts, so no single weakness can make the sentry pass vacuously:

1. Architecture: an AST scan proves the signed carrier, the release permit, the low-level JWS
   primitives and the evidence filenames are reachable only from the reviewed modules, and that
   every reviewed emitter has a behavioural runner below. The scanner is a pure function so part 3
   can prove it detects bypasses.
2. Fault injection: with the signing boundary replaced by a refusal, every inventoried emitter is
   exercised on real files and NO evidence file, release directory or rejection directory may
   appear. The same paths with the real boundary are the controls (so the refusal test cannot pass
   because the path simply never ran).
3. Non-vacuity: killed bypass mutants. Source mutants must trip the scanner; behavioural mutants
   (unsigned write, early commit, permissive evaluator, omitted required check, forged carrier)
   must trip the tree-invariant checker.

Engine-only. The CLI and platform emitters (API download, HTML/PDF render, archive, admin export,
cache, worker) are reviewed in their own repos; end-to-end F33 completion needs those companions.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.evidence_release import ArtifactSpec, ReleaseError, StagingCoordinator
from decoy_engine.evidence_release import _staging as staging_mod
from decoy_engine.signed_evidence import Purpose, verify_signed_evidence
from decoy_engine.signed_evidence import _boundary as boundary_mod
from decoy_engine.signed_evidence import _release as release_mod
from decoy_engine.signed_evidence._release import (
    DenyAllReleaseEvaluator,
    ReleaseDecision,
    StrictReleaseEvaluator,
)
from tests.unit.signed_evidence import _factory as f

SRC = Path(__file__).resolve().parents[2] / "src" / "decoy_engine"
BOUNDARY = "signed_evidence/_boundary.py"
RELEASE = "signed_evidence/_release.py"
STAGING = "evidence_release/_staging.py"
EVIDENCE_NAMES = {"evidence.json", "evidence.sig.json"}
EVIDENCE_CONSTS = {"EVIDENCE_FILENAME", "SIGNATURE_FILENAME"}
VISIBILITY_FUNCTIONS = {"_publish_dir"}
EVIDENCE_WRITERS = {"_write_evidence_files"}
# The reviewed emitter inventory. A new emitter is a gate failure until it is added here AND given
# a behavioural runner below; it is never auto-allowlisted.
EMITTERS = (
    ("decoy_engine.signed_evidence._boundary", "sign_evidence"),
    ("decoy_engine.signed_evidence._boundary", "adopt_verified_evidence"),
    ("decoy_engine.evidence_release._staging", "StagingArea.publish"),
    ("decoy_engine.evidence_release._staging", "StagingArea.reject"),
    ("decoy_engine.evidence_release._staging", "StagingCoordinator.export_verified"),
)
CALLERS_OF_EMITTERS = {
    BOUNDARY,
    STAGING,
    "signed_evidence/__init__.py",
    "evidence_release/__init__.py",
}
JWS_ALLOWED = {"signed_evidence/_jws.py", BOUNDARY, "signed_evidence/_verifier.py"}


# ------------------------------------------------------------------ part 1: scanner


def _owners(tree: ast.AST) -> dict[ast.AST, str]:
    """Map each node to its innermost enclosing function name ('' at module level)."""
    owner: dict[ast.AST, str] = {}

    def visit(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = (
                child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else current
            )
            owner[child] = name
            visit(child, name)

    visit(tree, "")
    return owner


def _call_name(node: ast.Call) -> str:
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    return fn.attr if isinstance(fn, ast.Attribute) else ""


def _is_fs_move(node: ast.Call) -> bool:
    fn = node.func
    return (
        isinstance(fn, ast.Attribute)
        and isinstance(fn.value, ast.Name)
        and fn.value.id in {"os", "shutil"}
        and fn.attr in {"replace", "rename", "renames", "move"}
    )


def scan_source(rel: str, text: str) -> list[str]:
    """Return violation codes for one module (``rel`` is its path under src/decoy_engine)."""
    tree = ast.parse(text)
    owner = _owners(tree)
    out: set[str] = set()
    for node in ast.walk(tree):
        fn = owner.get(node, "")
        if isinstance(node, ast.Name | ast.Attribute):
            ident = node.id if isinstance(node, ast.Name) else node.attr
            if ident == "_CARRIER_TOKEN" and rel != BOUNDARY:
                out.add("CARRIER_TOKEN_OUTSIDE_BOUNDARY")
            if ident == "_PERMIT_TOKEN" and rel != RELEASE:
                out.add("PERMIT_TOKEN_OUTSIDE_RELEASE")
            if ident in EVIDENCE_CONSTS:
                if rel not in {BOUNDARY, STAGING}:
                    out.add("EVIDENCE_FILENAME_OUTSIDE_WRITER")
                elif rel == STAGING and fn not in EVIDENCE_WRITERS:
                    out.add("EVIDENCE_WRITE_OUTSIDE_WRITER")
            if ident in {"sign_detached", "verify_es256_signature"} and rel not in JWS_ALLOWED:
                out.add("LOW_LEVEL_JWS_OUTSIDE_BOUNDARY")
        if (
            isinstance(node, ast.Constant)
            and node.value in EVIDENCE_NAMES
            and rel not in {BOUNDARY, STAGING}
        ):
            out.add("EVIDENCE_FILENAME_OUTSIDE_WRITER")
        if isinstance(node, ast.Import | ast.ImportFrom):
            mod = node.module if isinstance(node, ast.ImportFrom) else None
            imports_jws = bool(mod and mod.endswith("_jws")) or any(
                a.name == "_jws" for a in node.names
            )
            if imports_jws and rel not in JWS_ALLOWED:
                out.add("LOW_LEVEL_JWS_OUTSIDE_BOUNDARY")
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name == "SignedEvidence" and rel != BOUNDARY:
                out.add("CARRIER_CONSTRUCTED_OUTSIDE_BOUNDARY")
            if name == "ReleasePermit" and rel != RELEASE:
                out.add("PERMIT_CONSTRUCTED_OUTSIDE_RELEASE")
            if name == "build_statement_bytes" and rel != BOUNDARY:
                out.add("RAW_STATEMENT_SERIALIZATION_OUTSIDE_BOUNDARY")
            if (
                name in {"sign_evidence", "adopt_verified_evidence"}
                and rel not in CALLERS_OF_EMITTERS
            ):
                out.add("UNREVIEWED_EMITTER_CALL")
            if rel == STAGING and _is_fs_move(node) and fn not in VISIBILITY_FUNCTIONS:
                out.add("VISIBILITY_COMMIT_OUTSIDE_COMMIT")
    return sorted(out)


def _tree_sources() -> dict[str, str]:
    return {
        p.relative_to(SRC).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(SRC.rglob("*.py"))
    }


def test_scanner_is_clean_on_the_real_tree() -> None:
    sources = _tree_sources()
    for rel in (BOUNDARY, RELEASE, STAGING):
        assert rel in sources, f"inventory module moved: {rel}"
    offenders = {rel: v for rel, text in sources.items() if (v := scan_source(rel, text))}
    assert not offenders, offenders


def test_emitter_inventory_entries_exist_and_each_has_a_behavioural_runner() -> None:
    assert set(EMITTERS) == set(RUNNERS), (
        "inventory and runners diverged: add a runner for every emitter"
    )
    for module, dotted in EMITTERS:
        obj: Any = importlib.import_module(module)
        for part in dotted.split("."):
            obj = getattr(obj, part)
        assert callable(obj), (module, dotted)


def test_staging_module_uses_the_visibility_and_writer_functions_it_is_allowed() -> None:
    """Non-vacuity of the scanner's allow-lists: the functions it exempts must really exist."""
    tree = ast.parse(_tree_sources()[STAGING])
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert defined >= VISIBILITY_FUNCTIONS and defined >= EVIDENCE_WRITERS
    moves = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_fs_move(n)]
    assert moves, "no filesystem move found in the coordinator: the commit rule would be vacuous"


# ------------------------------------------------------------ part 2: fault injection

SPECS = tuple(ArtifactSpec(id=a, path=p, media_type=m) for a, p, m in f.ARTIFACT_SPECS)


def _draft() -> dict[str, Any]:
    d = f.base_draft()
    del d["artifacts"], d["publication"]
    return d


def _stage(area: Any) -> None:
    for rel, data in f.base_artifacts().items():
        area.write_artifact(rel, data)


def _files(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def _coordinator(root: Path, signer: Any, evaluator: Any) -> StagingCoordinator:
    return StagingCoordinator(root=root, signer=signer, evaluator=evaluator)


def _run_publish(root: Path, signer: Any, evaluator: Any, policy: Any) -> None:
    area = _coordinator(root, signer, evaluator).begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    area.publish(_draft(), SPECS, context=f.context(policy), policy_generation="g")


def _run_publish_twice(root: Path, signer: Any, evaluator: Any, policy: Any) -> None:
    for _ in range(2):  # the retry path: the second attempt must never mix with the first
        try:
            _run_publish(root, signer, evaluator, policy)
        except ReleaseError:
            continue


def _run_reject(root: Path, signer: Any, evaluator: Any, policy: Any) -> None:
    area = _coordinator(root, signer, evaluator).begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    area.reject(_draft(), SPECS, reason_code="OPERATOR_REJECTED")


_SEED_SIGNER = f.new_signer(kid="kv-seed")
_SEED = boundary_mod.sign_evidence(
    f.base_draft(), signer=_SEED_SIGNER
)  # minted before any patching


def _run_export(root: Path, signer: Any, evaluator: Any, policy: Any) -> None:
    ctx = f.context(f.policy_for(_SEED_SIGNER), purpose=Purpose.CONSUMER_ACCEPTANCE)
    _coordinator(root, signer, evaluator).export_verified(
        root / "exported", statement=_SEED.statement, signature=_SEED.signature, context=ctx
    )


def _run_adopt(root: Path, signer: Any, evaluator: Any, policy: Any) -> None:
    ctx = f.context(f.policy_for(_SEED_SIGNER), purpose=Purpose.CONSUMER_ACCEPTANCE)
    carrier = boundary_mod.adopt_verified_evidence(
        statement=_SEED.statement, signature=_SEED.signature, context=ctx
    )
    # adoption only wraps; materializing it still goes through the coordinator's writer
    dest = root / "adopted"
    dest.mkdir(parents=True)
    staging_mod._write_evidence_files(dest, carrier)


RUNNERS: dict[tuple[str, str], Callable[..., None]] = {
    ("decoy_engine.signed_evidence._boundary", "sign_evidence"): _run_publish,
    ("decoy_engine.signed_evidence._boundary", "adopt_verified_evidence"): _run_adopt,
    ("decoy_engine.evidence_release._staging", "StagingArea.publish"): _run_publish_twice,
    ("decoy_engine.evidence_release._staging", "StagingArea.reject"): _run_reject,
    ("decoy_engine.evidence_release._staging", "StagingCoordinator.export_verified"): _run_export,
}


def _refuse(*_a: Any, **_k: Any) -> Any:
    raise boundary_mod.SignedEvidenceError("SIGNING_FAILED")


def _evidence_paths(root: Path) -> list[str]:
    return [p for p in _files(root) if p.endswith(("evidence.json", "evidence.sig.json"))]


@pytest.mark.parametrize("entry", sorted(RUNNERS), ids=lambda e: e[1])
def test_control_with_the_real_boundary_emits_signed_evidence(
    entry: tuple[str, str], tmp_path: Path
) -> None:
    signer = f.new_signer()
    RUNNERS[entry](tmp_path / "out", signer, StrictReleaseEvaluator(), f.policy_for(signer))
    emitted = _evidence_paths(tmp_path)
    assert emitted, (
        f"{entry}: the control path emitted nothing, so the refusal test below would be vacuous"
    )
    for path in emitted:
        if path.endswith("evidence.json"):
            assert (tmp_path / path).with_name("evidence.sig.json").exists(), (
                f"{path} has no signature"
            )


# Paths whose control run only wraps already-signed evidence do not depend on sign_evidence, so
# patching it cannot (and should not) stop them; they are covered by the adopt refusal below.
_NEEDS_SIGNING = {
    k for k in RUNNERS if k[1] in {"sign_evidence", "StagingArea.publish", "StagingArea.reject"}
}


@pytest.mark.parametrize("entry", sorted(_NEEDS_SIGNING), ids=lambda e: e[1])
def test_refusing_boundary_emits_nothing_on_any_signing_path(
    entry: tuple[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signer = f.new_signer()
    monkeypatch.setattr(boundary_mod, "sign_evidence", _refuse)
    root = tmp_path / "out"
    try:
        RUNNERS[entry](root, signer, StrictReleaseEvaluator(), f.policy_for(signer))
    except (ReleaseError, boundary_mod.SignedEvidenceError):
        pass
    assert _evidence_paths(tmp_path) == [], f"{entry}: evidence emitted while the boundary refused"
    for forbidden in ("releases", "rejections"):
        assert not (root / forbidden).exists(), f"{entry}: {forbidden}/ appeared"


@pytest.mark.parametrize("entry", sorted(set(RUNNERS) - _NEEDS_SIGNING), ids=lambda e: e[1])
def test_refusing_adoption_exports_nothing(
    entry: tuple[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signer = f.new_signer()
    monkeypatch.setattr(boundary_mod, "adopt_verified_evidence", _refuse)
    try:
        RUNNERS[entry](tmp_path / "out", signer, StrictReleaseEvaluator(), f.policy_for(signer))
    except (ReleaseError, boundary_mod.SignedEvidenceError):
        pass
    assert _evidence_paths(tmp_path) == [], f"{entry}: evidence exported while adoption refused"


def test_default_deny_all_evaluator_releases_no_data(tmp_path: Path) -> None:
    signer = f.new_signer()
    with pytest.raises(ReleaseError):
        _run_publish(tmp_path / "out", signer, DenyAllReleaseEvaluator(), f.policy_for(signer))
    assert _files(tmp_path / "out") == []


# ------------------------------------------------------------ part 3: killed mutants

SOURCE_MUTANTS: dict[str, tuple[str, str, str]] = {
    "forge_carrier": (
        STAGING,
        "from decoy_engine.signed_evidence import SignedEvidence\nx = SignedEvidence(statement=b'')\n",
        "CARRIER_CONSTRUCTED_OUTSIDE_BOUNDARY",
    ),
    "steal_carrier_token": (
        STAGING,
        "from x import _CARRIER_TOKEN\n_CARRIER_TOKEN\n",
        "CARRIER_TOKEN_OUTSIDE_BOUNDARY",
    ),
    "forge_permit": (
        STAGING,
        "p = ReleasePermit(statement_id='x')\n",
        "PERMIT_CONSTRUCTED_OUTSIDE_RELEASE",
    ),
    "steal_permit_token": (
        "execution/_pipeline.py",
        "import m\nm._PERMIT_TOKEN\n",
        "PERMIT_TOKEN_OUTSIDE_RELEASE",
    ),
    "raw_serialize_elsewhere": (
        "quality/report.py",
        "from x import build_statement_bytes\nb = build_statement_bytes({})\n",
        "RAW_STATEMENT_SERIALIZATION_OUTSIDE_BOUNDARY",
    ),
    "write_evidence_file_elsewhere": (
        "execution/_pipeline.py",
        "open('evidence.json', 'wb').write(b'{}')\n",
        "EVIDENCE_FILENAME_OUTSIDE_WRITER",
    ),
    "evidence_constant_elsewhere": (
        "execution/_pipeline.py",
        "from decoy_engine.signed_evidence import EVIDENCE_FILENAME\nname = EVIDENCE_FILENAME\n",
        "EVIDENCE_FILENAME_OUTSIDE_WRITER",
    ),
    "low_level_jws_import": (
        "execution/_pipeline.py",
        "from decoy_engine.signed_evidence import _jws\n",
        "LOW_LEVEL_JWS_OUTSIDE_BOUNDARY",
    ),
    "low_level_jws_call": (
        "execution/_pipeline.py",
        "from x import verify_es256_signature\nverify_es256_signature()\n",
        "LOW_LEVEL_JWS_OUTSIDE_BOUNDARY",
    ),
    "unreviewed_signing_call": (
        "execution/_pipeline.py",
        "from decoy_engine.signed_evidence import sign_evidence\nsign_evidence({}, signer=None)\n",
        "UNREVIEWED_EMITTER_CALL",
    ),
    "unreviewed_adoption_call": (
        "connectors/s3.py",
        "adopt_verified_evidence(statement=b'', signature=b'', context=None)\n",
        "UNREVIEWED_EMITTER_CALL",
    ),
    "commit_outside_commit_fn": (
        STAGING,
        "import os\ndef publish_early(a, b):\n    os.replace(a, b)\n",
        "VISIBILITY_COMMIT_OUTSIDE_COMMIT",
    ),
    "rename_outside_commit_fn": (
        STAGING,
        "import os\ndef sneaky(a, b):\n    os.rename(a, b)\n",
        "VISIBILITY_COMMIT_OUTSIDE_COMMIT",
    ),
    "shutil_move_outside_commit_fn": (
        STAGING,
        "import shutil\ndef sneaky(a, b):\n    shutil.move(a, b)\n",
        "VISIBILITY_COMMIT_OUTSIDE_COMMIT",
    ),
    "evidence_write_outside_writer": (
        STAGING,
        "def leak(p):\n    (p / EVIDENCE_FILENAME).write_bytes(b'x')\n",
        "EVIDENCE_WRITE_OUTSIDE_WRITER",
    ),
}


@pytest.mark.parametrize("name", sorted(SOURCE_MUTANTS))
def test_scanner_kills_each_source_bypass_mutant(name: str) -> None:
    rel, source, expected = SOURCE_MUTANTS[name]
    assert expected in scan_source(rel, source), name


def test_scanner_does_not_flag_the_reviewed_modules_it_exempts() -> None:
    sources = _tree_sources()
    for rel in (BOUNDARY, RELEASE, STAGING):
        assert scan_source(rel, sources[rel]) == [], rel


def tree_violations(root: Path, policy: Any) -> list[str]:
    """Behavioural invariants over an output tree. A correct coordinator never produces any."""
    found: list[str] = []
    for kind in ("releases", "rejections"):
        for attempt_dir in sorted(p for p in (root / kind).glob("*/*") if p.is_dir()):
            statement = attempt_dir / "evidence.json"
            if not statement.exists():
                if (attempt_dir / "artifacts").exists():
                    found.append("DATA_WITHOUT_EVIDENCE")
                continue
            signature = attempt_dir / "evidence.sig.json"
            if not signature.exists():
                found.append("UNSIGNED")
                continue
            artifacts = attempt_dir / "artifacts"
            result = verify_signed_evidence(
                statement=statement.read_bytes(),
                signature=signature.read_bytes(),
                artifact_root=artifacts if artifacts.exists() else None,
                context=f.context(policy),
            )
            if kind == "rejections":
                if artifacts.exists():
                    found.append("REJECTION_CARRIES_DATA")
                if result.release.name != "DENY":
                    found.append("REJECTION_ALLOWS_RELEASE")
            elif not result.exit_class.is_success:
                found.append(f"RELEASE_NOT_VERIFIED_{result.exit_class.name}")
    return found


def _scenario(
    tmp_path: Path,
    *,
    signer: Any,
    evaluator: Any,
    draft: dict[str, Any] | None = None,
    tamper: bool = False,
) -> Path:
    root = tmp_path / "out"
    area = _coordinator(root, signer, evaluator).begin(job_id="job-1001", attempt_id="attempt-1")
    _stage(area)
    if tamper:
        victim = area.artifacts_dir / "outputs" / "customers.csv"
        victim.chmod(0o600)
        victim.write_bytes(b"id,name\n1,Mallory\n")
    try:
        area.publish(
            draft or _draft(), SPECS, context=f.context(f.policy_for(signer)), policy_generation="g"
        )
    except ReleaseError:
        pass
    return root


class _PermissiveEvaluator(StrictReleaseEvaluator):
    """Mutant: an evaluator that authorizes whatever it is shown, as a real bypass would."""

    def evaluate(
        self, signed: Any, *, artifact_root: Any, context: Any, policy_generation: str
    ) -> ReleaseDecision:
        real = super().evaluate(
            signed,
            artifact_root=artifact_root,
            context=context,
            policy_generation=policy_generation,
        )
        if real.permit is not None:
            return real
        permit = release_mod.ReleasePermit(
            release_mod._PERMIT_TOKEN,
            statement_id=signed.statement_id,
            inventory_digest=signed.inventory_digest,
            attempt_id=signed.attempt_id,
            policy_generation=policy_generation,
            trust_policy_digest="sha256:" + "0" * 64,
            catalog_digest="sha256:" + "0" * 64,
        )
        return ReleaseDecision(permit, (), real.result)


def test_invariant_checker_passes_the_real_coordinator(tmp_path: Path) -> None:
    signer = f.new_signer()
    root = _scenario(tmp_path, signer=signer, evaluator=StrictReleaseEvaluator())
    assert list((root / "releases").glob("*/*/evidence.json")), "control produced no release"
    assert tree_violations(root, f.policy_for(signer)) == []


def test_mutant_permissive_evaluator_shipping_tampered_data_is_killed(tmp_path: Path) -> None:
    signer = f.new_signer()
    root = _scenario(tmp_path, signer=signer, evaluator=_PermissiveEvaluator(), tamper=True)
    assert list((root / "releases").glob("*/*/evidence.json")), (
        "mutant did not release: scenario is vacuous"
    )
    assert tree_violations(root, f.policy_for(signer)), "tampered data shipped undetected"


def test_mutant_omitted_required_check_is_killed(tmp_path: Path) -> None:
    signer = f.new_signer()
    draft = _draft()
    draft["checks"] = [c for c in draft["checks"] if c["check_id"] != "residual_detection"]
    root = _scenario(tmp_path, signer=signer, evaluator=_PermissiveEvaluator(), draft=draft)
    assert list((root / "releases").glob("*/*/evidence.json")), (
        "mutant did not release: scenario is vacuous"
    )
    assert tree_violations(root, f.policy_for(signer)), (
        "an omitted required check shipped undetected"
    )


def test_mutant_stripped_signature_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signer = f.new_signer()
    real = staging_mod._write_evidence_files

    def strip(dest: Path, signed: Any) -> None:
        real(dest, signed)
        (dest / "evidence.sig.json").unlink()

    monkeypatch.setattr(staging_mod, "_write_evidence_files", strip)
    root = _scenario(tmp_path, signer=signer, evaluator=StrictReleaseEvaluator())
    assert "UNSIGNED" in tree_violations(root, f.policy_for(signer))


def test_mutant_forged_signature_in_a_valid_carrier_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutant: the boundary returns a structurally valid carrier whose signature is garbage."""
    signer = f.new_signer()
    real_sign = boundary_mod.sign_evidence

    def forged(draft: Mapping[str, Any], *, signer: Any) -> Any:
        real = real_sign(draft, signer=signer)
        fields = {fld.name: getattr(real, fld.name) for fld in dataclasses.fields(real)}
        fields["signature"] = b'{"protected":"AAAA","signature":"AAAA"}'
        return boundary_mod.SignedEvidence(boundary_mod._CARRIER_TOKEN, **fields)

    monkeypatch.setattr(boundary_mod, "sign_evidence", forged)
    root = _scenario(tmp_path, signer=signer, evaluator=_PermissiveEvaluator())
    assert list((root / "releases").glob("*/*/evidence.json")), (
        "mutant did not release: scenario is vacuous"
    )
    assert tree_violations(root, f.policy_for(signer)), "a forged signature shipped undetected"


def test_mutant_early_commit_is_killed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant: data becomes visible before signing finishes, then signing fails."""
    signer = f.new_signer()
    root = tmp_path / "out"
    area = _coordinator(root, signer, StrictReleaseEvaluator()).begin(
        job_id="job-1001", attempt_id="attempt-1"
    )
    _stage(area)
    final = root / "releases" / "job-1001" / "attempt-1"

    def early(*_a: Any, **_k: Any) -> Any:
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(area.path, final)  # visible, unsigned
        raise boundary_mod.SignedEvidenceError("SIGNING_FAILED")

    monkeypatch.setattr(boundary_mod, "sign_evidence", early)
    with pytest.raises(ReleaseError):
        area.publish(
            _draft(), SPECS, context=f.context(f.policy_for(signer)), policy_generation="g"
        )
    assert "DATA_WITHOUT_EVIDENCE" in tree_violations(root, f.policy_for(signer))
