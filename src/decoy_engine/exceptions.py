"""
Public exceptions raised by decoy_engine.

The engine raises these typed exceptions where the caller benefits from
catching a specific failure mode (config errors, connector auth issues,
license problems). Internal code that has not been migrated to typed
exceptions yet may still raise generic ValueError / RuntimeError; that
is fine and will be tightened incrementally.

Anything not listed here is private and may change without a version bump.
"""


class DecoyError(Exception):
    """Base class for all decoy_engine exceptions."""


class ConfigError(DecoyError):
    """Raised when a pipeline config is malformed."""


class PipelineValidationError(ConfigError):
    """Raised when a pipeline configuration fails validation.

    Carries the optional `path` attribute (dotted location of the
    bad config, e.g. ``nodes[2].config.path``) so callers can map
    the failure back to a specific node / inspector field instead
    of parsing the message string. None when validation failed at
    a level above any single node (e.g. invalid top-level mode).
    """

    def __init__(self, message: str, path: str | None = None) -> None:
        self.path = path
        super().__init__(message)


class ConnectorError(DecoyError):
    """Base class for connector-related errors."""


class ConnectorAuthError(ConnectorError):
    """Raised when a connector cannot authenticate to its source or destination."""


class LicenseError(DecoyError):
    """Base class for license-related errors."""


class LicenseExpiredError(LicenseError):
    """Raised when a license has expired."""


class FlagPauseSignal(DecoyError):
    """Raised by flag_gate op when review conditions fail.

    Not a crash — the platform runner catches this and transitions the
    job to `review_pending` rather than `failed`. The conditions_failed
    list is stored in the `job_reviews` table for the approver.
    """

    def __init__(self, conditions_failed: list[dict], gate_id: str = "") -> None:
        self.conditions_failed = conditions_failed
        self.gate_id = gate_id
        detail = "; ".join(c.get("message", str(c)) for c in conditions_failed)
        prefix = f"flag gate {gate_id!r}: " if gate_id else "flag gate: "
        super().__init__(f"{prefix}conditions failed: {detail}")


ForgeError = DecoyError
