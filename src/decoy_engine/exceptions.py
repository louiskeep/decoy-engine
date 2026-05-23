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

    R2.1: also carries an optional stable ``code`` from
    :mod:`decoy_engine.validation_result.CODES` so UI consumers can
    route the failure without string-matching the message text.
    """

    def __init__(
        self,
        message: str,
        path: str | None = None,
        code: str | None = None,
    ) -> None:
        self.path = path
        self.code = code
        super().__init__(message)


class ConnectorError(DecoyError):
    """Base class for connector-related errors."""


class ConnectorAuthError(ConnectorError):
    """Raised when a connector cannot authenticate to its source or destination."""


class LicenseError(DecoyError):
    """Base class for license-related errors."""


class LicenseExpiredError(LicenseError):
    """Raised when a license has expired."""


class FlagPauseSignal(DecoyError):  # noqa: N818 -- this is a control-flow Signal, not a runtime Error; the "Signal" suffix is intentional and contrasts with sibling *Error classes that are real failures.
    """Raised by flag_gate op when review conditions fail.

    Not a crash: the platform runner catches this and transitions the
    job to `review_pending` rather than `failed`. The conditions_failed
    list is stored in the `job_reviews` table for the approver.
    """

    def __init__(self, conditions_failed: list[dict], gate_id: str = "") -> None:
        self.conditions_failed = conditions_failed
        self.gate_id = gate_id
        detail = "; ".join(c.get("message", str(c)) for c in conditions_failed)
        prefix = f"flag gate {gate_id!r}: " if gate_id else "flag gate: "
        super().__init__(f"{prefix}conditions failed: {detail}")


# ── FK preservation (Sprint 4, item 4) ────────────────────────────────
#
# Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG;
# materialize parent pool; child samples with replacement.
#
# Raised by the pool_resolver closure built in graph/runner.py when a
# declared FK in `column_relationships` cannot be resolved at runtime.
# The graph errors translator (graph/errors.py::translate) maps these
# to the corresponding fk.* stable codes from validation_result.CODES.
# Strict mode aborts the run; lenient mode drops the offending child
# rows + writes a manifest warning.


class FKPreservationError(DecoyError):
    """Base class for FK preservation runtime errors. Carries the
    parent/child identity so the manifest can record which FK failed."""

    def __init__(
        self,
        message: str,
        parent_node: str,
        parent_column: str,
    ) -> None:
        self.parent_node = parent_node
        self.parent_column = parent_column
        super().__init__(message)


class EmptyParentPoolError(FKPreservationError):
    """Parent node + column resolved, but the column has zero distinct
    non-null values. The child has nothing to sample from.

    Maps to code fk.empty_parent_pool. Common when a filter upstream
    removed every row from the parent table, or every value in the
    parent column was null."""


class UnknownFKColumnError(FKPreservationError):
    """Parent node resolved, but the named column is missing from its
    output schema. Indicates a stale relationship declaration -- the
    parent's column set changed since the operator confirmed the FK.

    Maps to code fk.unknown_column. Validation-time catches the
    common case (the column exists in the node config); the runtime
    raise covers schema drift inside the op."""


class PKDuplicatesError(DecoyError):
    """Column declared ``primary_key: true`` produced duplicate values
    after generation. Strict-by-default since a non-unique PK breaks
    join semantics downstream (same key identifies multiple rows).

    Set ``DECOY_PK_LENIENT=1`` to downgrade to a logged warning + a
    manifest entry so a one-off scrub with a faker-based PK + small
    row_count can still ship. Tier-1 audit (2026-05-20) flipped the
    default from lenient to strict — analytics pipelines should not
    silently ship duplicate primary keys.

    Maps to code ``pk.duplicates``. Carries the column name + counts
    on the exception for the manifest assembler."""

    code: str = "pk.duplicates"

    def __init__(
        self,
        column: str,
        total_non_null: int,
        unique_values: int,
        strategy: str | None = None,
    ) -> None:
        self.column = column
        self.total_non_null = total_non_null
        self.unique_values = unique_values
        self.duplicate_count = total_non_null - unique_values
        self.strategy = strategy
        message = (
            f"PK column {column!r} has {self.duplicate_count} duplicate value(s) "
            f"out of {total_non_null} non-null rows"
        )
        if strategy:
            message += (
                f". The declared strategy ({strategy!r}) doesn't guarantee "
                f"uniqueness at this row count. Switch to 'sequence' for a "
                f"hard uniqueness guarantee, or set DECOY_PK_LENIENT=1 to "
                f"downgrade this to a warning."
            )
        super().__init__(message)


ForgeError = DecoyError
