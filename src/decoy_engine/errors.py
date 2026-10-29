"""Public exceptions raised by decoy_engine (V2.0-C).

Single canonical location for every typed exception the engine raises
that a caller might want to catch. Pre-V2.0-C this surface was split
across ``decoy_engine.exceptions`` (orchestration/config/connector
errors) and ``decoy_engine.internal.validator`` (ValidationError);
V2.0-C consolidates them here so callers depend on one module and
neither needs to reach into ``decoy_engine.internal.*``.

Anything not listed here is private and may change without a version
bump. Internal code may still raise generic ValueError / RuntimeError;
that is fine and will be tightened incrementally as those callsites
move to typed exceptions.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from decoy_engine import reasons

if TYPE_CHECKING:
    from decoy_engine.execution._row_error_summary import RowErrorSummary


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


class ValidationError(DecoyError):
    """Lower-level validation failure raised by individual modular
    validators (the per-op ``validate_config`` callbacks).

    The public boundary catches this and re-wraps as
    :class:`PipelineValidationError` for the raise-on-first-error
    public API, or collects it into a
    :class:`decoy_engine.validation_result.ValidationResult` for the
    collect-all API. Callers should normally catch
    PipelineValidationError or read the ValidationResult; this type
    is exposed mainly because the per-op ``validate_config``
    callbacks raise it.

    R2.1: carries an optional stable ``code`` so callers can map the
    failure to UI inspector fields without parsing the message
    string. Codes live in
    ``decoy_engine.validation_result.CODES``. Older raises with no
    code are tolerated -- they surface as ``CODES.UNTAGGED`` when
    converted to a ValidationResult.

    Moved from ``decoy_engine.internal.validator`` to this public
    module in V2.0-C so platform/CLI callers stop importing from
    ``decoy_engine.internal.*``.
    """

    def __init__(
        self,
        message: str,
        path: str | None = None,
        code: str | None = None,
    ) -> None:
        self.path = path
        self.code = code
        if path:
            full_message = f"Validation error at '{path}': {message}"
        else:
            full_message = f"Validation error: {message}"
        super().__init__(full_message)
        self._raw_message = message

    @property
    def raw_message(self) -> str:
        """The original message without the `Validation error at '<path>':` wrapper."""
        return self._raw_message


class ConnectorError(DecoyError):
    """Base class for connector-related errors."""


class ConnectorAuthError(ConnectorError):
    """Raised when a connector cannot authenticate to its source or destination."""


class LicenseError(DecoyError):
    """Base class for license-related errors."""


class LicenseExpiredError(LicenseError):
    """Raised when a license has expired."""


class FlagPauseSignal(DecoyError):  # noqa: N818 -- control-flow Signal, not a runtime Error.
    """Raised by flag_gate op when review conditions fail.

    Not a crash: the platform runner catches this and transitions the
    job to `review_pending` rather than `failed`. The conditions_failed
    list is stored in the `job_reviews` table for the approver.
    """

    def __init__(self, conditions_failed: list[dict[str, Any]], gate_id: str = "") -> None:
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
# Raised when a declared FK in `column_relationships` cannot be resolved
# at runtime. The matching stable fk.* codes live in
# validation_result.CODES.
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
    default from lenient to strict -- analytics pipelines should not
    silently ship duplicate primary keys.

    Maps to code ``pk.duplicates``. Carries the column name + counts
    on the exception for the manifest assembler."""

    code: str = reasons.runtime.PK_DUPLICATES

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


class MaskKeyDerivationError(DecoyError):
    """A value-preserving mask strategy (FPE, date-shift) could not derive its
    per-column key from the configured master key.

    The engine refuses to silently degrade to a weaker seed-only path; the
    operator must fix the master-key infrastructure and re-run. Raised as a
    ``DecoyError`` (not a bare ``RuntimeError``) so an upstream
    ``except DecoyError`` catches it like every other typed engine failure.

    Maps to code ``mask.key_derivation_failed``. Carries the originating
    strategy name on ``.strategy`` for the manifest / operator message."""

    code: str = reasons.runtime.MASK_KEY_DERIVATION_FAILED

    def __init__(self, message: str, *, strategy: str | None = None) -> None:
        self.strategy = strategy
        super().__init__(message)


class MaskKeyRequiredError(DecoyError):
    """A keyed-mask kernel was asked to run with no mask key at all.

    Distinct from ``MaskKeyDerivationError`` (a present master key whose
    per-column derivation failed): here the ``mask_key`` argument itself is
    ``None`` or empty, so there is no key material to derive from. The keyed
    crypto kernels fail closed on this BEFORE producing any output rather than
    emit unkeyed or default-keyed ciphertext that would silently not protect
    the data. Maps to code ``mask.key_required``; carries the originating
    kernel name on ``.kernel`` for the operator message."""

    code: str = reasons.runtime.MASK_KEY_REQUIRED

    def __init__(self, message: str, *, kernel: str | None = None) -> None:
        self.kernel = kernel
        super().__init__(message)


class FpeChecksumError(DecoyError):
    """FPE checksum mode cannot produce a valid-by-construction value for the
    requested scheme, or the scheme name is unknown.

    Raised at call time in ``_fpe_checksum_permute`` (backstop) and at
    plan-compile time for the same conditions.  Callers should catch
    ``DecoyError`` or ``FpeChecksumError`` specifically.

    Maps to code ``fpe.checksum_unsupported`` by default. Carries the scheme
    name on ``.scheme``.

    Task 5.2 (P5-final) adds a second code this class can carry:
    ``fpe.checksum_invalid_source`` when the SOURCE value (including its
    check digit) fails ``checksums.validate()`` before any permutation is
    attempted. That is a distinct failure from "the scheme name itself is
    unsupported," so callers mapping to handler-surface codes
    (``fpe_checksum_unsupported`` vs ``fpe_checksum_invalid_source``) can
    tell them apart via ``.code`` rather than string-matching the message.

    Task 5.2 (P7, error redaction): this class never carries the offending
    value, only ``.scheme`` (a config-level scheme name such as ``"luhn"``,
    not customer data). Every call site's message text uses only
    non-sensitive metadata (lengths, scheme names): a plaintext,
    ciphertext, key, derived key material, or tweak must never appear in
    this exception's ``str()``, ``repr()``, ``.args``, or any attribute."""

    code: str = reasons.runtime.FPE_CHECKSUM_UNSUPPORTED

    def __init__(self, message: str, *, scheme: str | None = None, code: str | None = None) -> None:
        self.scheme = scheme
        if code is not None:
            self.code = code
        super().__init__(message)


class FpeUnencryptableError(DecoyError):
    """An FPE value cannot be format-preserving-encrypted without either leaking
    cleartext or producing a non-invertible (non-round-trip) output, so the
    engine fails closed rather than emit unsafe masked data (DE-01 cluster-C,
    2026-07-14).

    Closes two previously-silent value classes at the source in
    ``transforms/fpe.py``:

    - a value with ZERO in-charset characters (the all-out-of-charset case).
      The pre-fix covering-hash fallback produced an in-charset value that the
      inverse cipher cannot recover (verified non-round-trip ``'---' -> '092'
      -> '858'``); there is nothing to format-preserving-encrypt, so fail
      closed instead of emitting a value that silently will not reverse.
    - under ``preserve_separators=False``, any out-of-charset character. The
      pre-fix path returned the whole value unchanged (a silent cleartext
      no-op on the executed V2 path); fail closed instead.

    NOTE (scope): the PARTIAL out-of-charset case -- a value with in-charset
    content plus an out-of-charset format prefix, e.g. ``M`` in ``M000001`` --
    is NOT closed here. It keeps the current preserve-the-prefix behavior and
    is surfaced as a structured ``QualityWarning`` (residual partial-plaintext
    disclosure); full coverage needs the structured-FPE/FF1 fast-follow. See
    ``docs/discussions/2026-07-14-de01-vault-token-for-fpe.md``.

    The executed strategy handler re-raises this as ``StrategyError`` at the
    execution boundary (matching the ``fpe_charset_degenerate`` precedent).
    Maps to code ``fpe.unencryptable`` by default.

    Task 5.2 (FF1 profile enforcement) adds two more codes this class can
    carry: ``fpe.unencryptable_domain`` when a value's in-charset domain
    (``radix ** length``) falls below the FF1 minimum admissible domain, and
    ``fpe.unencryptable_length`` for every other profile-boundary rejection
    (key size, radix range, alphabet duplicates, tweak/body length caps).
    Distinct codes let a caller (the strategy handler mapping to
    ``StrategyError`` codes) tell "this value's domain is genuinely too
    small" apart from "this is a config/wiring bug," without string-matching
    the message.

    Task 5.2 (P7, error redaction): fully redacted. The constructor used to
    carry the offending value verbatim on ``.value``; it no longer retains
    the value anywhere on the instance, in the message, or via
    ``__cause__``/``__context__`` chaining a caller controls. ``value=`` is
    still accepted (so existing call sites need no signature change) but is
    reduced immediately to ``.value_length`` (a character count), never
    stored or echoed as content. Every call site's message text is written
    to describe the failure using only non-sensitive metadata (lengths,
    counts, code points, scheme/config names): a plaintext, ciphertext,
    key, derived key material, or tweak must never appear in this
    exception's ``str()``, ``repr()``, ``.args``, or any attribute."""

    code: str = reasons.runtime.FPE_UNENCRYPTABLE

    def __init__(self, message: str, *, value: str | None = None, code: str | None = None) -> None:
        self.value_length: int | None = len(value) if value is not None else None
        if code is not None:
            self.code = code
        super().__init__(message)


class ValidatorFailedError(DecoyError):
    """Raised when a job-level validator fails and quarantine is not active.

    The engine is fail-closed by default (SP-05 / P5.INFRA.4): a validator
    failure kills the job unless quarantine is enabled with the
    ``validation_fail`` trigger. Callers that want warn-only behaviour must
    wait for a later sprint.

    Carries the full ``ValidationReport`` on ``.report`` so error handlers
    can inspect which validators failed and which rows were affected without
    re-running validation. The report type is ``decoy_engine.validators.ValidationReport``;
    it is typed as ``Any`` here to avoid a circular import between
    ``decoy_engine.errors`` and ``decoy_engine.validators``.

    Raises:
        ValidatorFailedError: Always raised with a non-None ``report``
            whose ``passed`` attribute is ``False``.
    """

    def __init__(
        self, report: Any, *, summaries_by_table: Mapping[str, RowErrorSummary] | None = None
    ) -> None:
        self.report = report
        # Row errors observed in the same full-frame run; a validator failure raises first,
        # so without this the row-error evidence would be dropped.
        self.summaries_by_table = summaries_by_table
        findings = getattr(report, "findings", ())
        n = len(findings)
        validators_run = list(getattr(report, "validators_run", ()))
        super().__init__(
            f"validator framework: {n} finding(s) failed the job. "
            f"Validators run: {validators_run}. "
            "Enable quarantine with trigger 'validation_fail' to route "
            "failing rows to a separate output instead of failing the job."
        )


class RowErrorsFailedError(DecoyError):
    """Raised when per-row strategy errors exist and quarantine does not
    cover their trigger (Sprint 2 honesty pack, D8).

    Sibling of ``ValidatorFailedError``: the engine is fail-closed by
    default for row-level format/mask errors exactly as it is for validator
    findings. A bucketize/date_shift cell that fails coercion, or a
    code_set value that cannot be masked, used to silently keep the source
    value in the output; after this sprint the job either fails loud (this
    exception) or the offending rows are quarantined under their trigger
    (``format_error`` / ``mask_error``) -- there is no third silent option.

    Carries the tuple of ``RowErrorRecord`` on ``.records`` so error handlers
    can inspect which rows failed without re-running the mask pass. The
    message summarizes counts by ``(table, column, trigger)`` and never
    embeds a cell value (the records themselves carry no cell values either;
    see ``execution._row_errors.RowError``).
    """

    def __init__(
        self,
        records: tuple[Any, ...],
        *,
        summary: RowErrorSummary | None = None,
        summaries_by_table: Mapping[str, RowErrorSummary] | None = None,
    ) -> None:
        self.records = records
        # `summary`: single-table chunked legs. `summaries_by_table`: full-frame runs. Both
        # describe every observed error; `.records` stays the uncovered subset.
        self.summary = summary
        self.summaries_by_table = summaries_by_table
        counts: dict[tuple[str, str, str], int] = {}
        for r in records:
            key = (r.table, r.column, r.trigger)
            counts[key] = counts.get(key, 0) + 1
        by_key = ", ".join(
            f"{table}.{column} [{trigger}]: {n}"
            for (table, column, trigger), n in sorted(counts.items())
        )
        triggers = sorted({r.trigger for r in records})
        super().__init__(
            f"row-error framework: {len(records)} row(s) failed strategy execution and are "
            f"not covered by an enabled quarantine trigger. Counts by (table, column, "
            f"trigger): {by_key}. Enable quarantine with trigger(s) {triggers} to route "
            "failing rows to a separate output instead of failing the job."
        )


class FixedWidthParseError(DecoyError):
    """Raised when a fixed-width data file cannot be parsed against its
    `FixedWidthLayout` column-spec (S4, engine-finish-open-ended program).

    Covers three fail-closed cases: a record shorter than a column's
    `start + width` extent (row-width mismatch), a sliced value that
    cannot cast to its column's declared type, and a line whose bytes are
    not valid UTF-8. None of them truncates, coerces or replaces the bad
    input -- all raise here instead. Per the row-error framework's convention (see
    `RowErrorsFailedError`), the message never embeds the offending
    cell value -- only the file path, 1-based line number, and column
    name -- since the file being parsed may itself carry the PII this
    is a masking product for. Every raise in
    `profile._fixed_width_reader` happens after its `except` block has
    exited (the handler records only safe position data), so the caught
    `ValueError`, `TypeError` or `UnicodeDecodeError` -- whose own text or
    `.object` embeds the raw value -- is never attached as `__cause__` or
    `__context__`, even if someone later inspects the raised exception.
    """


class FixedWidthWriteError(DecoyError):
    """Raised when a value cannot be written to a fixed-width file such that
    it reads back unchanged against its `FixedWidthLayout` column-spec
    (`profile._fixed_width_writer.write_fixed_width`).

    The writer is the inverse of `read_fixed_width`: it fails closed on any
    value that would not round-trip exactly -- a rendered value wider than its
    column `width` (overflow), a string whose strip-side edge is the pad
    character (the reader would strip part of the value), an integer whose
    trailing digits the reader's pad-strip would remove, a null/NaN (the reader
    never emits one), a value whose Python type does not match the column's
    declared type, a string carrying an embedded newline, or a character that
    cannot encode as UTF-8 -- rather than publishing a file that would decode to
    different data. A mid-frame failure publishes nothing (atomic move on full
    success only), so the canonical path is never left half-written.

    Mirrors `FixedWidthParseError`'s PII-safe contract: the message carries only
    the 0-based row index, column name and rendered value length -- never the
    cell value -- and every raise happens from a frame that holds no cell value,
    with the caught caster exception never attached as `__cause__`/`__context__`,
    because the file being written may itself carry the PII this is a masking
    product for. See docs/security/error-reporting-and-data-exposure.md.
    """


__all__ = [
    "ConfigError",
    "ConnectorAuthError",
    "ConnectorError",
    "DecoyError",
    "EmptyParentPoolError",
    "FKPreservationError",
    "FixedWidthParseError",
    "FixedWidthWriteError",
    "FlagPauseSignal",
    "FpeChecksumError",
    "FpeUnencryptableError",
    "LicenseError",
    "LicenseExpiredError",
    "MaskKeyDerivationError",
    "MaskKeyRequiredError",
    "PKDuplicatesError",
    "PipelineValidationError",
    "RowErrorsFailedError",
    "UnknownFKColumnError",
    "ValidationError",
    "ValidatorFailedError",
]
