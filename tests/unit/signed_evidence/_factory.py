"""Python-side builders for signing-boundary tests (not for conformance: see `_corpus`).

Keys here are synthetic and generated per test; nothing is shared with a production key.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec

from decoy_engine.signed_evidence import (
    ExpectedArtifact,
    LocalEs256Signer,
    Purpose,
    RequiredCatalog,
    SigningIdentity,
    TrustPolicy,
    VerificationContext,
)
from tests.unit.signed_evidence import _corpus

TENANT = "tenant-acme"
SIGNER_ID = "decoy:acme-prod"
EVAL_TIME = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)


def new_signer(
    kid: str = "kv-test-1", version: str = "2026-01", signer: str = SIGNER_ID
) -> LocalEs256Signer:
    return LocalEs256Signer(
        ec.generate_private_key(ec.SECP256R1()), SigningIdentity(signer, kid, version)
    )


def policy_for(
    *signers: LocalEs256Signer, states: dict[str, dict[str, Any]] | None = None
) -> TrustPolicy:
    from joserfc.jwk import ECKey

    keys = []
    for s in signers:
        jwk = dict(s.public_jwk())
        entry: dict[str, Any] = {
            "tenant": TENANT,
            "signer": s.identity.signer,
            "kid": s.identity.kid,
            "key_version": s.identity.key_version,
            "alg": "ES256",
            "jwk_thumbprint": ECKey.import_key(jwk).thumbprint(),
            "jwk": jwk,
            "state": "ACTIVE",
            "retired_at": None,
            "revoked_at": None,
            "compromised_since": None,
        }
        entry.update((states or {}).get(s.identity.kid, {}))
        keys.append(entry)
    raw = json.dumps(
        {
            "schema": "decoy.trust-policy/v1",
            "id": "tp-test",
            "version": "1",
            "issued_at": "2026-10-01T00:00:00Z",
            "expires_at": "2027-04-01T00:00:00Z",
            "keys": keys,
        },
        indent=2,
    ).encode()
    return TrustPolicy.from_bytes(raw, pinned_sha256="sha256:" + hashlib.sha256(raw).hexdigest())


def catalog() -> RequiredCatalog:
    return RequiredCatalog.from_bytes(
        (_corpus.CORPUS / "shared" / "catalog-strict.json").read_bytes()
    )


def base_artifacts() -> dict[str, bytes]:
    """Relative path -> bytes of the corpus artifact set."""
    root = _corpus.CORPUS / "artifacts" / "base"
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


ARTIFACT_SPECS = (
    ("out_customers", "outputs/customers.csv", "text/csv"),
    ("out_orders", "outputs/orders.parquet", "application/vnd.apache.parquet"),
    ("quarantine", "outputs/quarantine.ndjson", "application/x-ndjson"),
)


def expected_artifacts() -> tuple[ExpectedArtifact, ...]:
    files = base_artifacts()
    return tuple(
        ExpectedArtifact(
            id=aid, sha256="sha256:" + hashlib.sha256(files[path]).hexdigest(), media_type=media
        )
        for aid, path, media in ARTIFACT_SPECS
    )


def base_draft(signer: str = SIGNER_ID) -> dict[str, Any]:
    """A complete, valid PUBLISHED statement draft (the corpus statement under our signer)."""
    doc: dict[str, Any] = json.loads(_corpus.load("valid_release_openssl").statement)
    doc["issuer"]["signer"] = signer
    return copy.deepcopy(doc)


def context(
    policy: TrustPolicy,
    *,
    purpose: Purpose = Purpose.RELEASE_GATE,
    artifacts: tuple[ExpectedArtifact, ...] | None = None,
    **over: Any,
) -> VerificationContext:
    args: dict[str, Any] = {
        "purpose": purpose,
        "trust_policy": policy,
        "required_catalog": catalog(),
        "expected_tenant": TENANT,
        "expected_bundle_id": "bundle-001",
        "expected_job_id": "job-1001",
        "expected_attempt_id": "attempt-1",
        "expected_snapshot_id": "snap-001",
        "expected_artifacts": expected_artifacts() if artifacts is None else artifacts,
        "evaluation_time": EVAL_TIME,
    }
    args.update(over)
    return VerificationContext(**args)


def write_artifacts(root: Path) -> Path:
    for rel, data in base_artifacts().items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root
