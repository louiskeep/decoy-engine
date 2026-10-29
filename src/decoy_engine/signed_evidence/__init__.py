"""Signed evidence: detached ES256 JWS over a JCS statement, with offline verification (F33).

Public surface: the full Decoy-profile verifier and its typed inputs and results. The low-level
ES256 check is private by design; see ``_verifier``. This package imports only the standard
library, ``joserfc``, ``rfc8785`` and ``cryptography``, so a standalone verifier can ship it
without ``decoy_engine`` initialisation or any data library.

Plan: docs/plans/2026-10-29-f33-signed-evidence-offline-verify.md.
"""

from __future__ import annotations

from ._artifacts import hash_file_under
from ._boundary import (
    EVIDENCE_FILENAME,
    SIGNATURE_FILENAME,
    SignedEvidence,
    adopt_verified_evidence,
    sign_evidence,
)
from ._catalog import RequiredCatalog, RequiredCheck
from ._context import ExpectedArtifact, HistoricalDecision, Purpose, VerificationContext
from ._errors import SignedEvidenceError
from ._release import (
    DenyAllReleaseEvaluator,
    ReleaseDecision,
    ReleaseEvaluator,
    ReleasePermit,
    StrictReleaseEvaluator,
)
from ._result import (
    ArtifactResult,
    ArtifactStatus,
    CheckResult,
    CheckStatus,
    ExitClass,
    HistoricalAuthorization,
    IdentityBinding,
    ReleaseVerdict,
    SchemaStatus,
    SignatureStatus,
    TimeInfo,
    TrustStatus,
    VerificationResult,
)
from ._signer import LocalEs256Signer, ResolvedSigner, SigningIdentity, der_to_p1363
from ._statement import EvidenceStatementV1, check_relative_path, parse_statement
from ._trust import KeyState, TrustedKey, TrustPolicy
from ._verifier import verify_signed_evidence

__all__ = [
    "EVIDENCE_FILENAME",
    "SIGNATURE_FILENAME",
    "ArtifactResult",
    "ArtifactStatus",
    "CheckResult",
    "CheckStatus",
    "DenyAllReleaseEvaluator",
    "EvidenceStatementV1",
    "ExitClass",
    "ExpectedArtifact",
    "HistoricalAuthorization",
    "HistoricalDecision",
    "IdentityBinding",
    "KeyState",
    "LocalEs256Signer",
    "Purpose",
    "ReleaseDecision",
    "ReleaseEvaluator",
    "ReleasePermit",
    "ReleaseVerdict",
    "RequiredCatalog",
    "RequiredCheck",
    "ResolvedSigner",
    "SchemaStatus",
    "SignatureStatus",
    "SignedEvidence",
    "SignedEvidenceError",
    "SigningIdentity",
    "StrictReleaseEvaluator",
    "TimeInfo",
    "TrustPolicy",
    "TrustStatus",
    "TrustedKey",
    "VerificationContext",
    "VerificationResult",
    "adopt_verified_evidence",
    "check_relative_path",
    "der_to_p1363",
    "hash_file_under",
    "parse_statement",
    "sign_evidence",
    "verify_signed_evidence",
]
