"""Value types of the reason registry.

Pattern sources: RFC 9457 (a stable problem identity separate from occurrence detail and
transport status) and the gRPC status-code guidance (retry the operation, restart a larger
operation, or repair state first are different actions). The engine has no HTTP or RPC surface,
so these are plain enums and frozen dataclasses, not a Problem Details response.

This module imports only the standard library: the registry is a leaf that every producer
package may import, and it must never import execution, planning, the CLI or the platform.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Phase(str, Enum):
    CONFIGURATION = "configuration"
    RESOLVE = "resolve"
    COMPILE = "compile"
    MASK = "mask"
    GENERATE = "generate"
    FINALIZE = "finalize"
    VERIFY = "verify"
    PUBLISH = "publish"
    CLEANUP = "cleanup"
    UNKNOWN = "unknown"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Kind(str, Enum):
    FAILURE = "failure"
    FINDING = "finding"
    WARNING = "warning"
    DECISION = "decision"
    CONTROL = "control"


class RetryClass(str, Enum):
    """Advice about the required action. It never authorizes replay or starts a retry loop."""

    NEVER = "never"
    CONFIGURATION_REPAIR = "configuration_repair"
    ENVIRONMENT_REPAIR = "environment_repair"
    RETRY_OPERATION = "retry_operation"
    RESTART_ATTEMPT = "restart_attempt"
    UNKNOWN = "unknown"


class Outcome(str, Enum):
    EXECUTION = "execution"
    FINDINGS = "findings"
    VERIFICATION = "verification"
    PUBLICATION = "publication"
    CONFIGURATION = "configuration"
    CANCELLED = "cancelled"
    SUCCESS = "success"


@dataclass(frozen=True)
class PhaseRule:
    """What a reason means when it is raised at one operation boundary."""

    phase: Phase
    severity: Severity
    outcome: Outcome
    retry: RetryClass


@dataclass(frozen=True)
class ReasonSpec:
    """One immutable registry entry, identified by ``(namespace, code)``."""

    namespace: str
    code: str
    symbol: str
    summary: str
    owner: str
    kind: Kind
    rules: tuple[PhaseRule, ...]
    context: tuple[str, ...] = ()
    since: str = "1.0.0"
    deprecated_in: str | None = None
    replaced_by: tuple[str, str] | None = None

    @property
    def identity(self) -> tuple[str, str]:
        return (self.namespace, self.code)

    @property
    def default_rule(self) -> PhaseRule:
        return self.rules[0]

    def rule_for(self, phase: Phase) -> PhaseRule | None:
        for rule in self.rules:
            if rule.phase is phase:
                return rule
        return None


class ReasonCode(str):
    """The wire string of a registered reason, carrying its namespace.

    It compares, hashes, formats and serializes exactly like the legacy string, so every
    existing ``==``, ``in``, f-string and JSON use of a code is unchanged. The namespace is what
    lets a consumer look the entry up when one wire string exists in two namespaces.
    """

    __slots__ = ("namespace",)
    namespace: str

    def __new__(cls, code: str, namespace: str) -> ReasonCode:
        obj = super().__new__(cls, code)
        obj.namespace = namespace
        return obj

    def __reduce__(self) -> tuple[type[ReasonCode], tuple[str, str]]:
        return (ReasonCode, (str(self), self.namespace))

    @property
    def spec(self) -> ReasonSpec:
        from decoy_engine.reasons._registry import get

        return get(self.namespace, str(self))
