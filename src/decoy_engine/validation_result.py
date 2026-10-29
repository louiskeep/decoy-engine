"""Validation result contract.

A typed, multi-message validation outcome shared by engine, API, web,
and CLI callers. Lets callers:

  - render every problem at once (not "fix one, re-run, find the next"),
  - distinguish errors from warnings,
  - map failures to inspector fields via stable `code` strings (no
    string-matching the human-readable message), and
  - diff the validator's `normalized_config` against the original
    caller-owned input.

The ``ValidationResult`` type is still load-bearing in V2 for the
multi-error config / plan-compile diagnostic surface. The V1 graph-
runner entry points that historically produced these results
(``validate_graph(yaml) -> None`` raise-style and
``validate_graph_full(yaml) -> ValidationResult`` collecting-style) were
removed under the ``decoy_v2_clean_break`` PO directive; see
``__init__.py`` for the current public surface (``PipelineConfig`` +
``compile_plan`` + ``ExecutionAdapter``). V2 callers raise
``PipelineValidationError`` or ``PlanCompileError`` at the
choke-point and surface the multi-message detail via the structures
defined here.

Message codes
-------------
Codes follow ``<subject>.<failure>`` form, kebab/underscore. They are
intentionally stable: once shipped, a renamed code is a breaking change
for any UI mapping to inspector fields. Add a new code rather than
renaming.

The :data:`CODES` namespace below acts as the registry; importing from
it (rather than literal strings) keeps typos out of validators and
makes "find usages" useful for downstream consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from decoy_engine import reasons

Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True)
class ValidationMessage:
    """One issue surfaced by a validator.

    Attributes:
        severity: "error" (blocks run), "warning" (advisory), "info"
            (annotation, never blocks).
        code: Stable identifier for the failure mode. See :data:`CODES`.
        message: Human-readable text safe to surface in a UI toast.
        path: Dotted location of the bad config when known, e.g.
            ``nodes[2].config.path``. ``None`` for top-level failures.
        node_id: Resolved graph node id when the failure is node-scoped
            (e.g. ``"src_1"``). The platform layer typically populates
            this from the YAML using the path index; engine validators
            can leave it ``None`` and let the platform resolve it.
        hint: Optional one-line actionable suggestion ("set has_header
            to true, or provide column_names").
    """

    severity: Severity
    code: str
    message: str
    path: str | None = None
    node_id: str | None = None
    hint: str | None = None


@dataclass
class ValidationResult:
    """Outcome of a non-raising validation pass.

    Errors and warnings are kept in separate lists rather than mixed +
    filtered so callers don't have to worry about ordering. The
    :pyattr:`ok` property is the canonical "can this run?" signal.

    ``normalized_config`` is the validator's view of the input after
    defaults were filled in. It is ``None`` when validation produced
    one or more errors (the validator may not have run to completion).
    On success it is always populated, even when no defaults were
    applied -- callers can diff against the original input for
    audit/explainability.
    """

    errors: list[ValidationMessage] = field(default_factory=list)
    warnings: list[ValidationMessage] = field(default_factory=list)
    normalized_config: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        """True iff there are no errors. Warnings are not blocking."""
        return not self.errors

    def add_error(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        node_id: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.errors.append(
            ValidationMessage(
                severity="error",
                code=code,
                message=message,
                path=path,
                node_id=node_id,
                hint=hint,
            )
        )

    def add_warning(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        node_id: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.warnings.append(
            ValidationMessage(
                severity="warning",
                code=code,
                message=message,
                path=path,
                node_id=node_id,
                hint=hint,
            )
        )


# -- Code registry -----------------------------------------------------------------------
#
# Stable code strings exposed via a namespace so callers can import them
# rather than literal-stringing. Adding a new code is non-breaking;
# renaming is breaking. Codes are grouped by subject prefix.


class CODES:
    """Stable validation code constants.

    Imported as ``from decoy_engine.validation_result import CODES``
    and referenced like ``CODES.NODE_UNKNOWN_KIND``. Each constant is
    the wire-format string a UI or CLI consumer should match against.
    """

    # Top-level pipeline config.
    TOP_LEVEL_BAD_MODE = reasons.config.TOP_LEVEL_BAD_MODE
    TOP_LEVEL_BAD_SCHEMA_VERSION = reasons.config.TOP_LEVEL_BAD_SCHEMA_VERSION
    TOP_LEVEL_BAD_ENGINE = reasons.config.TOP_LEVEL_BAD_ENGINE
    NODES_EMPTY_LIST = reasons.config.NODES_EMPTY_LIST
    EDGES_BAD_TYPE = reasons.config.EDGES_BAD_TYPE

    # Per-node structural checks.
    NODE_BAD_TYPE = reasons.config.NODE_BAD_TYPE
    NODE_BAD_ID = reasons.config.NODE_BAD_ID
    NODE_DUPLICATE_ID = reasons.config.NODE_DUPLICATE_ID
    NODE_UNKNOWN_KIND = reasons.config.NODE_UNKNOWN_KIND
    NODE_BAD_NAME = reasons.config.NODE_BAD_NAME
    NODE_BAD_CONFIG_TYPE = reasons.config.NODE_BAD_CONFIG_TYPE
    # Audit Sprint 1.5: catch misdeclared NATIVE_ENGINE at validation time
    # instead of silently falling back to pandas in the registry.
    NODE_BAD_NATIVE_ENGINE = reasons.config.NODE_BAD_NATIVE_ENGINE

    # Per-edge.
    EDGE_BAD_TYPE = reasons.config.EDGE_BAD_TYPE
    EDGE_UNKNOWN_FROM_NODE = reasons.config.EDGE_UNKNOWN_FROM_NODE
    EDGE_UNKNOWN_TO_NODE = reasons.config.EDGE_UNKNOWN_TO_NODE
    EDGE_UNKNOWN_PORT = reasons.config.EDGE_UNKNOWN_PORT
    EDGE_PORT_ON_NON_SPLIT = reasons.config.EDGE_PORT_ON_NON_SPLIT

    # Graph topology.
    GRAPH_CYCLE = reasons.config.GRAPH_CYCLE
    GRAPH_NODE_INSUFFICIENT_INPUTS = reasons.config.GRAPH_NODE_INSUFFICIENT_INPUTS
    GRAPH_NODE_TOO_MANY_INPUTS = reasons.config.GRAPH_NODE_TOO_MANY_INPUTS
    GRAPH_SINK_HAS_OUTPUTS = reasons.config.GRAPH_SINK_HAS_OUTPUTS
    # R2.4 cross-node: source format and target format disagree with no
    # convert.file_type node in between. Engine logs a logger.warning;
    # platform preflight (api/pipelines/preflight.py) emits a structured
    # advisory with severity=warning so the pipeline builder can show a
    # conversion disclosure on the target node without blocking the run.
    GRAPH_FORMAT_MISMATCH = reasons.config.GRAPH_FORMAT_MISMATCH
    # R2.3 strict mode: target.file had no explicit 'format' and would
    # have been back-filled from the source format. Non-blocking in lenient
    # mode (back-fill applies silently); emitted as an error when strict=True
    # so production pipelines can require explicit format declarations.
    TARGET_FILE_FORMAT_INFERRED = reasons.config.TARGET_FILE_FORMAT_INFERRED

    # Cross-node schema checks (R2.3).
    MASK_UNKNOWN_COLUMN = reasons.config.MASK_UNKNOWN_COLUMN

    # Variable scope checks (R2.3). Platform-only - the engine doesn't
    # own the variable resolution registry. Surfaced through the same
    # preflight wire format so the UI can route the failure to the
    # pipeline + show the missing scope.key clearly.
    VARIABLE_UNRESOLVED = reasons.config.VARIABLE_UNRESOLVED
    VARIABLE_UNKNOWN_SCOPE = reasons.config.VARIABLE_UNKNOWN_SCOPE

    # ${nodes.<id>.<key>} export-reference checks (R2.3). Engine-side
    # because they're pure graph-topology lints.
    NODES_REF_UNKNOWN_ID = reasons.config.NODES_REF_UNKNOWN_ID
    NODES_REF_NOT_UPSTREAM = reasons.config.NODES_REF_NOT_UPSTREAM

    # mask op-level (R2.2).
    MASK_BAD_COLUMNS_TYPE = reasons.config.MASK_BAD_COLUMNS_TYPE
    MASK_BAD_COLUMN_SPEC_TYPE = reasons.config.MASK_BAD_COLUMN_SPEC_TYPE
    MASK_UNKNOWN_STRATEGY = reasons.config.MASK_UNKNOWN_STRATEGY
    MASK_FORMULA_MISSING = reasons.config.MASK_FORMULA_MISSING
    MASK_REFERENCE_MISSING = reasons.config.MASK_REFERENCE_MISSING

    # target.file specific.
    TARGET_FILE_MISSING_OUTPUT_FILENAME = reasons.config.TARGET_FILE_MISSING_OUTPUT_FILENAME
    TARGET_FILE_UNSUPPORTED_FORMAT = reasons.config.TARGET_FILE_UNSUPPORTED_FORMAT
    # Preflight only - the engine doesn't know the platform's
    # settings.output_dir. Platform raises this when a target.file
    # would write outside the configured output sandbox.
    TARGET_FILE_PATH_OUTSIDE_OUTPUT_DIR = reasons.config.TARGET_FILE_PATH_OUTSIDE_OUTPUT_DIR

    # Shared across cloud source/target ops (source.s3/gcs/sftp,
    # target.s3/gcs/sftp). They all route through
    # `_cloud_io.validate_format` so one code identifies the failure
    # across kinds; per-kind missing_field codes are below.
    CLOUD_IO_UNSUPPORTED_FORMAT = reasons.config.CLOUD_IO_UNSUPPORTED_FORMAT

    # source.s3 / target.s3
    SOURCE_S3_MISSING_BUCKET = reasons.config.SOURCE_S3_MISSING_BUCKET
    SOURCE_S3_MISSING_PATH = reasons.config.SOURCE_S3_MISSING_PATH
    TARGET_S3_MISSING_BUCKET = reasons.config.TARGET_S3_MISSING_BUCKET
    TARGET_S3_MISSING_PATH = reasons.config.TARGET_S3_MISSING_PATH
    # R2.4 preflight (platform-only): require explicit credentials. The
    # engine's op would otherwise fall back to the boto3 chain, which
    # in a multi-tenant deployment silently uses the platform's own
    # AWS identity. Variable references like ${var.X} / ${env.X} count
    # as explicit here; R2.3 handles the resolution check separately.
    SOURCE_S3_MISSING_CREDENTIALS = reasons.config.SOURCE_S3_MISSING_CREDENTIALS
    TARGET_S3_MISSING_CREDENTIALS = reasons.config.TARGET_S3_MISSING_CREDENTIALS

    # source.gcs / target.gcs
    SOURCE_GCS_MISSING_BUCKET = reasons.config.SOURCE_GCS_MISSING_BUCKET
    SOURCE_GCS_MISSING_PATH = reasons.config.SOURCE_GCS_MISSING_PATH
    TARGET_GCS_MISSING_BUCKET = reasons.config.TARGET_GCS_MISSING_BUCKET
    TARGET_GCS_MISSING_PATH = reasons.config.TARGET_GCS_MISSING_PATH
    # R2.4 preflight (platform-only): require service_account_json. The
    # engine's op would otherwise fall back to GCP Application Default
    # Credentials, which in a multi-tenant deployment silently uses the
    # platform's own workload identity.
    SOURCE_GCS_MISSING_CREDENTIALS = reasons.config.SOURCE_GCS_MISSING_CREDENTIALS
    TARGET_GCS_MISSING_CREDENTIALS = reasons.config.TARGET_GCS_MISSING_CREDENTIALS

    # source.sftp / target.sftp
    SOURCE_SFTP_MISSING_HOST = reasons.config.SOURCE_SFTP_MISSING_HOST
    SOURCE_SFTP_MISSING_USERNAME = reasons.config.SOURCE_SFTP_MISSING_USERNAME
    SOURCE_SFTP_MISSING_PATH = reasons.config.SOURCE_SFTP_MISSING_PATH
    SOURCE_SFTP_MISSING_AUTH = reasons.config.SOURCE_SFTP_MISSING_AUTH
    TARGET_SFTP_MISSING_HOST = reasons.config.TARGET_SFTP_MISSING_HOST
    TARGET_SFTP_MISSING_USERNAME = reasons.config.TARGET_SFTP_MISSING_USERNAME
    TARGET_SFTP_MISSING_PATH = reasons.config.TARGET_SFTP_MISSING_PATH
    TARGET_SFTP_MISSING_AUTH = reasons.config.TARGET_SFTP_MISSING_AUTH

    # source.file specific.
    SOURCE_FILE_MISSING_PATH = reasons.config.SOURCE_FILE_MISSING_PATH
    # Preflight: the source.file path doesn't exist on disk relative
    # to the platform's upload_dir at job-create time. The engine
    # itself never raises this - it's a platform-only check (the
    # engine doesn't know the upload_dir). Lives on the same code
    # namespace so the UI can route it next to the path field.
    SOURCE_FILE_FILE_NOT_FOUND = reasons.config.SOURCE_FILE_FILE_NOT_FOUND
    SOURCE_FILE_UNSUPPORTED_FORMAT = reasons.config.SOURCE_FILE_UNSUPPORTED_FORMAT
    SOURCE_FILE_BAD_HAS_HEADER_TYPE = reasons.config.SOURCE_FILE_BAD_HAS_HEADER_TYPE
    SOURCE_FILE_NO_HEADER_COLUMNS = reasons.config.SOURCE_FILE_NO_HEADER_COLUMNS
    SOURCE_FILE_COLUMN_NAMES_WITH_HEADER = reasons.config.SOURCE_FILE_COLUMN_NAMES_WITH_HEADER
    SOURCE_FILE_BAD_DELIMITER = reasons.config.SOURCE_FILE_BAD_DELIMITER
    SOURCE_FILE_BAD_ROW_LIMIT = reasons.config.SOURCE_FILE_BAD_ROW_LIMIT
    SOURCE_FILE_MISSING_FW_COLUMNS = reasons.config.SOURCE_FILE_MISSING_FW_COLUMNS
    SOURCE_FILE_CSV_PARAM_ON_NON_CSV = reasons.config.SOURCE_FILE_CSV_PARAM_ON_NON_CSV
    # A headerless delimited file whose per-record field count does not match the
    # declared column_names (a short/ragged row, an over-wide row, or an empty
    # file). Raised at read time by the profiler, never with a cell value.
    SOURCE_FILE_COLUMN_COUNT_MISMATCH = reasons.config.SOURCE_FILE_COLUMN_COUNT_MISMATCH
    # A headerless delimited file containing a NUL byte. NUL is not valid in a
    # text delimited file; the csv width scan and pandas tokenize it differently
    # (pandas keeps it, csv rejects it), so the two cannot be reconciled. Fail
    # closed at read rather than sanitize-and-guess. Never raised with a cell value.
    SOURCE_FILE_NUL_BYTE = reasons.config.SOURCE_FILE_NUL_BYTE
    # A headerless delimited file containing a lone CR (a carriage return not part
    # of a CRLF pair): an old-Mac line ending, or an unquoted/quoted CR in a record.
    # The csv width scan and pandas treat a lone CR as a row boundary differently,
    # which can shift a value into the wrong column without changing the frame
    # width. Only LF and CRLF are reconcilable, so a lone CR fails closed at read.
    # Never raised with a cell value.
    SOURCE_FILE_LONE_CR = reasons.config.SOURCE_FILE_LONE_CR

    # R3.10 generation key policy. Platform-only preflight codes -- the
    # engine doesn't know about admin settings, but the codes live in
    # the shared registry so the wire format stays uniform.
    GENERATION_KEY_REQUIRED = reasons.config.GENERATION_KEY_REQUIRED
    GENERATION_RANDOM_NOT_ALLOWED = reasons.config.GENERATION_RANDOM_NOT_ALLOWED

    # ── FK preservation codes ──
    #
    # Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG;
    # materialize parent pool; child samples with replacement.
    #
    # Codes split into validation-time (validator stage 9) and runtime
    # (pool resolver + generate op). Strict mode promotes advisory codes
    # (parallel_branches, strategy_coerced) from warning to error.
    FK_UNKNOWN_NODE = reasons.config.FK_UNKNOWN_NODE  # validation, error
    FK_UNKNOWN_COLUMN = reasons.config.FK_UNKNOWN_COLUMN  # validation + runtime, error
    FK_PARENT_AFTER_CHILD = reasons.config.FK_PARENT_AFTER_CHILD  # validation, error
    FK_PARENT_UNRESOLVED = (
        reasons.config.FK_PARENT_UNRESOLVED
    )  # runtime, error in strict / warning otherwise
    FK_EMPTY_PARENT_POOL = (
        reasons.config.FK_EMPTY_PARENT_POOL
    )  # runtime, error in strict / warning otherwise
    FK_PARALLEL_BRANCHES = reasons.config.FK_PARALLEL_BRANCHES  # validation, warning (advisory)
    FK_NONDETERMINISTIC_MASK = (
        reasons.config.FK_NONDETERMINISTIC_MASK
    )  # validation, warning (advisory; error under DECOY_FK_STRICT_DETERMINISM=1)
    # Pre-V1 tier-1 audit fixes (2026-05-20):
    # Reject column_relationships entries whose child / junction node
    # can't carry the relationship at run time. source.* nodes are
    # read-only inputs (no engine hook to rewrite their column values);
    # transforms that don't materialize values (drop_column, filter,
    # dedupe) likewise have no place for the engine to inject FK pool
    # lookups. The platform-side panel already warns on these at author
    # time; this code stops them from saving silently.
    FK_INELIGIBLE_CHILD_KIND = reasons.config.FK_INELIGIBLE_CHILD_KIND  # validation, error
    # Distribution-bound composition: sequential ordering + cardinality
    # bounds don't compose because the bounds repair phase shuffles
    # placement. Operator gets a clear warning instead of silently
    # broken ordering at run time.
    FK_SEQUENTIAL_BOUNDS_CONFLICT = (
        reasons.config.FK_SEQUENTIAL_BOUNDS_CONFLICT
    )  # validation, warning
    # Self-reference: parent.node == child.node. V1 supported when
    # child.column != parent.column (engine reads pool from in-flight
    # output instead of pool_resolver). V1 rejects same-column
    # self-edges (FK_SELF_CYCLE) and longer column cycles within one
    # node (a -> b, b -> a). Mask + non-generate kinds are inert (no
    # two-pass mechanism); FK_SELF_REF_INERT rejects those instead of
    # the engine silently ignoring the entry at run time.
    FK_SELF_REFERENCE = reasons.config.FK_SELF_REFERENCE  # legacy: kept for back-compat (now a warning when child.column != parent.column; previously a hard error)
    FK_SELF_CYCLE = (
        reasons.config.FK_SELF_CYCLE  # validation, error (cycle within one node's column relationships)
    )
    FK_SELF_REF_INERT = (
        reasons.config.FK_SELF_REF_INERT  # validation, error (self-ref on a node kind that can't two-pass)
    )

    # Many-to-many junction kind. Engine accepts `kind: m2m` with
    # left_parent + right_parent + junction columns; rejects unknown
    # parent nodes / columns / missing junction columns the same way
    # the kind: fk path does.
    FK_M2M_UNKNOWN_NODE = reasons.config.FK_M2M_UNKNOWN_NODE  # validation, error
    FK_M2M_UNKNOWN_COLUMN = reasons.config.FK_M2M_UNKNOWN_COLUMN  # validation, error
    FK_M2M_BAD_POOL = (
        reasons.config.FK_M2M_BAD_POOL
    )  # validation, error (unsupported pool_strategy)

    # Composite / multi-parent FK: parent: [...] array form for
    # junction-style FKs where a single child column references two
    # parents jointly. Engine accepts; validator checks all parents
    # resolve.
    FK_MULTI_PARENT_BAD_SHAPE = reasons.config.FK_MULTI_PARENT_BAD_SHAPE  # validation, error

    # Custom Faker provider pool: parent.custom_provider names a
    # provider that is not currently registered. Warning at validation
    # time (providers load from filesystem/DB at run time; the provider
    # may register itself before execution). The engine's
    # EmptyParentPoolError is the hard backstop at run time.
    FK_CUSTOM_PROVIDER_UNREGISTERED = (
        reasons.config.FK_CUSTOM_PROVIDER_UNREGISTERED
    )  # validation, warning

    # PK uniqueness: a column marked primary_key has duplicate values
    # in the output. Hard error in strict, warning otherwise (some
    # generation strategies - faker - can produce duplicates by
    # chance; operators may want to tolerate when sample size is small).
    PK_DUPLICATES = reasons.config.PK_DUPLICATES  # runtime, error in strict / warning otherwise

    # Generic catch-all for validators that haven't been migrated yet.
    # New gates should add a specific code rather than reusing this.
    UNTAGGED = reasons.config.UNTAGGED
