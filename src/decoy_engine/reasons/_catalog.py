"""Section profiles for the catalog modules.

A profile fixes the defaults of one namespace (kind, allowed phases, severity, outcome, retry
class) so each catalog line is a code and a fixed safe summary. Overrides are explicit keyword
arguments on the line that needs them. The phase-dependent mapping therefore lives in one place:
the :class:`~decoy_engine.reasons._types.PhaseRule` tuple built here.
"""

from __future__ import annotations

from decoy_engine.reasons._registry import Declarer
from decoy_engine.reasons._types import Kind, Outcome, Phase, RetryClass, Severity

_P = Phase


def config(owner: str) -> Declarer:
    """A pipeline-config validation reason: the config must be repaired."""
    return Declarer(
        "config",
        owner,
        kind=Kind.FAILURE,
        phases=(_P.CONFIGURATION,),
        severity=Severity.ERROR,
        outcome=Outcome.CONFIGURATION,
        retry=RetryClass.CONFIGURATION_REPAIR,
    )


def plan(owner: str) -> Declarer:
    """A compile-time plan rejection: the plan inputs must be repaired."""
    return Declarer(
        "plan",
        owner,
        kind=Kind.FAILURE,
        phases=(_P.COMPILE,),
        severity=Severity.ERROR,
        outcome=Outcome.CONFIGURATION,
        retry=RetryClass.CONFIGURATION_REPAIR,
    )


def runtime(owner: str) -> Declarer:
    """A failure while masking, generating or finalizing. Retry advice is unknown by default."""
    return Declarer(
        "runtime",
        owner,
        kind=Kind.FAILURE,
        phases=(_P.MASK, _P.GENERATE, _P.FINALIZE, _P.UNKNOWN),
        severity=Severity.ERROR,
        outcome=Outcome.EXECUTION,
        retry=RetryClass.UNKNOWN,
    )


def provider(owner: str) -> Declarer:
    """A provider-routing failure: the provider configuration must be repaired."""
    return Declarer(
        "provider",
        owner,
        kind=Kind.FAILURE,
        phases=(_P.COMPILE, _P.GENERATE),
        severity=Severity.ERROR,
        outcome=Outcome.CONFIGURATION,
        retry=RetryClass.CONFIGURATION_REPAIR,
    )


def quality(owner: str) -> Declarer:
    """A failure in a quality, privacy or provenance computation."""
    return Declarer(
        "quality",
        owner,
        kind=Kind.FAILURE,
        phases=(_P.VERIFY, _P.UNKNOWN),
        severity=Severity.ERROR,
        outcome=Outcome.EXECUTION,
        retry=RetryClass.UNKNOWN,
    )


def connector(owner: str) -> Declarer:
    """A connector failure while resolving a source or sink."""
    return Declarer(
        "connector",
        owner,
        kind=Kind.FAILURE,
        phases=(_P.RESOLVE, _P.UNKNOWN),
        severity=Severity.ERROR,
        outcome=Outcome.EXECUTION,
        retry=RetryClass.UNKNOWN,
    )


def subset(owner: str) -> Declarer:
    """A subset preflight or planning rejection."""
    return Declarer(
        "subset",
        owner,
        kind=Kind.FAILURE,
        phases=(_P.CONFIGURATION, _P.RESOLVE),
        severity=Severity.ERROR,
        outcome=Outcome.CONFIGURATION,
        retry=RetryClass.CONFIGURATION_REPAIR,
    )


def route(owner: str) -> Declarer:
    """A routing or admission decision. A decline is information, never a failure."""
    return Declarer(
        "route",
        owner,
        kind=Kind.DECISION,
        phases=(_P.RESOLVE, _P.COMPILE),
        severity=Severity.INFO,
        outcome=Outcome.SUCCESS,
        retry=RetryClass.NEVER,
    )


def warning(owner: str) -> Declarer:
    """A non-fatal warning emitted while a run continues."""
    return Declarer(
        "warning",
        owner,
        kind=Kind.WARNING,
        phases=(_P.MASK, _P.GENERATE, _P.FINALIZE),
        severity=Severity.WARNING,
        outcome=Outcome.FINDINGS,
        retry=RetryClass.NEVER,
    )


def verification(owner: str) -> Declarer:
    """A check that completed and reported findings. Whether it enforces is the owner's call."""
    return Declarer(
        "verification",
        owner,
        kind=Kind.FINDING,
        phases=(_P.VERIFY,),
        severity=Severity.WARNING,
        outcome=Outcome.FINDINGS,
        retry=RetryClass.NEVER,
    )


def physical(owner: str) -> Declarer:
    """A shadow-plan comparison or admission difference. Internal diagnostics, never replayable."""
    return Declarer(
        "physical",
        owner,
        kind=Kind.FAILURE,
        phases=(_P.COMPILE, _P.UNKNOWN),
        severity=Severity.ERROR,
        outcome=Outcome.EXECUTION,
        retry=RetryClass.NEVER,
    )


def decoy(owner: str) -> Declarer:
    """A cross-cutting reason the engine itself assigns (fallbacks, outcomes, control)."""
    return Declarer(
        "decoy",
        owner,
        kind=Kind.FAILURE,
        phases=tuple(Phase),
        severity=Severity.ERROR,
        outcome=Outcome.EXECUTION,
        retry=RetryClass.UNKNOWN,
    )
