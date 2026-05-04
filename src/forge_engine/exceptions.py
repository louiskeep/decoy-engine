"""
Public exceptions raised by forge_engine.

The engine raises these typed exceptions where the caller benefits from
catching a specific failure mode (config errors, connector auth issues,
license problems). Internal code that has not been migrated to typed
exceptions yet may still raise generic ValueError / RuntimeError; that
is fine and will be tightened incrementally.

Anything not listed here is private and may change without a version bump.
"""


class ForgeError(Exception):
    """Base class for all forge_engine exceptions."""


class ConfigError(ForgeError):
    """Raised when a pipeline config is malformed."""


class PipelineValidationError(ConfigError):
    """Raised when a pipeline configuration fails validation."""


class ConnectorError(ForgeError):
    """Base class for connector-related errors."""


class ConnectorAuthError(ConnectorError):
    """Raised when a connector cannot authenticate to its source or destination."""


class LicenseError(ForgeError):
    """Base class for license-related errors."""


class LicenseExpiredError(LicenseError):
    """Raised when a license has expired."""
