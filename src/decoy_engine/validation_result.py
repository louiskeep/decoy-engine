"""Validation result contract (R2.1).

A typed, multi-message validation outcome shared by engine, API, web,
and CLI callers. Replaces the older single-`ValidationError`-raises
pattern at the public boundary so callers can:

  - render every problem at once (not "fix one, re-run, find the next"),
  - distinguish errors from warnings,
  - map failures to inspector fields via stable `code` strings (no
    string-matching the human-readable message), and
  - diff the validator's `normalized_config` against the original
    caller-owned input.

The legacy ``validate_graph(yaml) -> None`` raise-style entry point
stays in place for backward compatibility; new code should call
``validate_graph_full(yaml) -> ValidationResult``.

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
        self.errors.append(ValidationMessage(
            severity="error", code=code, message=message,
            path=path, node_id=node_id, hint=hint,
        ))

    def add_warning(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        node_id: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.warnings.append(ValidationMessage(
            severity="warning", code=code, message=message,
            path=path, node_id=node_id, hint=hint,
        ))


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
    TOP_LEVEL_BAD_MODE = "top_level.bad_mode"
    TOP_LEVEL_BAD_SCHEMA_VERSION = "top_level.bad_schema_version"
    TOP_LEVEL_BAD_ENGINE = "top_level.bad_engine"
    NODES_EMPTY_LIST = "nodes.empty_list"
    EDGES_BAD_TYPE = "edges.bad_type"

    # Per-node structural checks.
    NODE_BAD_TYPE = "node.bad_type"
    NODE_BAD_ID = "node.bad_id"
    NODE_DUPLICATE_ID = "node.duplicate_id"
    NODE_UNKNOWN_KIND = "node.unknown_kind"
    NODE_BAD_NAME = "node.bad_name"
    NODE_BAD_CONFIG_TYPE = "node.bad_config_type"
    # Audit Sprint 1.5: catch misdeclared NATIVE_ENGINE at validation time
    # instead of silently falling back to pandas in the registry.
    NODE_BAD_NATIVE_ENGINE = "node.bad_native_engine"

    # Per-edge.
    EDGE_BAD_TYPE = "edge.bad_type"
    EDGE_UNKNOWN_FROM_NODE = "edge.unknown_from_node"
    EDGE_UNKNOWN_TO_NODE = "edge.unknown_to_node"
    EDGE_UNKNOWN_PORT = "edge.unknown_port"
    EDGE_PORT_ON_NON_SPLIT = "edge.port_on_non_split"

    # Graph topology.
    GRAPH_CYCLE = "graph.cycle"
    GRAPH_NODE_INSUFFICIENT_INPUTS = "graph.node_insufficient_inputs"
    GRAPH_NODE_TOO_MANY_INPUTS = "graph.node_too_many_inputs"
    GRAPH_SINK_HAS_OUTPUTS = "graph.sink_has_outputs"
    # R2.4 cross-node: source format and target format disagree with no
    # convert.file_type node in between. Engine logs a logger.warning;
    # platform preflight (api/pipelines/preflight.py) emits a structured
    # advisory with severity=warning so the pipeline builder can show a
    # conversion disclosure on the target node without blocking the run.
    GRAPH_FORMAT_MISMATCH = "graph.format_mismatch"
    # R2.3 strict mode: target.file had no explicit 'format' and would
    # have been back-filled from the source format. Non-blocking in lenient
    # mode (back-fill applies silently); emitted as an error when strict=True
    # so production pipelines can require explicit format declarations.
    TARGET_FILE_FORMAT_INFERRED = "target_file.format_inferred"

    # Cross-node schema checks (R2.3).
    MASK_UNKNOWN_COLUMN = "mask.unknown_column"

    # Variable scope checks (R2.3). Platform-only - the engine doesn't
    # own the variable resolution registry. Surfaced through the same
    # preflight wire format so the UI can route the failure to the
    # pipeline + show the missing scope.key clearly.
    VARIABLE_UNRESOLVED = "variable.unresolved"
    VARIABLE_UNKNOWN_SCOPE = "variable.unknown_scope"

    # ${nodes.<id>.<key>} export-reference checks (R2.3). Engine-side
    # because they're pure graph-topology lints.
    NODES_REF_UNKNOWN_ID = "nodes_ref.unknown_id"
    NODES_REF_NOT_UPSTREAM = "nodes_ref.not_upstream"

    # mask op-level (R2.2).
    MASK_BAD_COLUMNS_TYPE = "mask.bad_columns_type"
    MASK_BAD_COLUMN_SPEC_TYPE = "mask.bad_column_spec_type"
    MASK_UNKNOWN_STRATEGY = "mask.unknown_strategy"
    MASK_FORMULA_MISSING = "mask.formula_missing"
    MASK_REFERENCE_MISSING = "mask.reference_missing"

    # target.file specific.
    TARGET_FILE_MISSING_OUTPUT_FILENAME = "target_file.missing_output_filename"
    TARGET_FILE_UNSUPPORTED_FORMAT = "target_file.unsupported_format"
    # Preflight only - the engine doesn't know the platform's
    # settings.output_dir. Platform raises this when a target.file
    # would write outside the configured output sandbox.
    TARGET_FILE_PATH_OUTSIDE_OUTPUT_DIR = "target_file.path_outside_output_dir"

    # Shared across cloud source/target ops (source.s3/gcs/sftp,
    # target.s3/gcs/sftp). They all route through
    # `_cloud_io.validate_format` so one code identifies the failure
    # across kinds; per-kind missing_field codes are below.
    CLOUD_IO_UNSUPPORTED_FORMAT = "cloud_io.unsupported_format"

    # source.s3 / target.s3
    SOURCE_S3_MISSING_BUCKET = "source_s3.missing_bucket"
    SOURCE_S3_MISSING_PATH = "source_s3.missing_path"
    TARGET_S3_MISSING_BUCKET = "target_s3.missing_bucket"
    TARGET_S3_MISSING_PATH = "target_s3.missing_path"
    # R2.4 preflight (platform-only): require explicit credentials. The
    # engine's op would otherwise fall back to the boto3 chain, which
    # in a multi-tenant deployment silently uses the platform's own
    # AWS identity. Variable references like ${var.X} / ${env.X} count
    # as explicit here; R2.3 handles the resolution check separately.
    SOURCE_S3_MISSING_CREDENTIALS = "source_s3.missing_credentials"
    TARGET_S3_MISSING_CREDENTIALS = "target_s3.missing_credentials"

    # source.gcs / target.gcs
    SOURCE_GCS_MISSING_BUCKET = "source_gcs.missing_bucket"
    SOURCE_GCS_MISSING_PATH = "source_gcs.missing_path"
    TARGET_GCS_MISSING_BUCKET = "target_gcs.missing_bucket"
    TARGET_GCS_MISSING_PATH = "target_gcs.missing_path"
    # R2.4 preflight (platform-only): require service_account_json. The
    # engine's op would otherwise fall back to GCP Application Default
    # Credentials, which in a multi-tenant deployment silently uses the
    # platform's own workload identity.
    SOURCE_GCS_MISSING_CREDENTIALS = "source_gcs.missing_credentials"
    TARGET_GCS_MISSING_CREDENTIALS = "target_gcs.missing_credentials"

    # source.sftp / target.sftp
    SOURCE_SFTP_MISSING_HOST = "source_sftp.missing_host"
    SOURCE_SFTP_MISSING_USERNAME = "source_sftp.missing_username"
    SOURCE_SFTP_MISSING_PATH = "source_sftp.missing_path"
    SOURCE_SFTP_MISSING_AUTH = "source_sftp.missing_auth"
    TARGET_SFTP_MISSING_HOST = "target_sftp.missing_host"
    TARGET_SFTP_MISSING_USERNAME = "target_sftp.missing_username"
    TARGET_SFTP_MISSING_PATH = "target_sftp.missing_path"
    TARGET_SFTP_MISSING_AUTH = "target_sftp.missing_auth"

    # source.file specific.
    SOURCE_FILE_MISSING_PATH = "source_file.missing_path"
    # Preflight: the source.file path doesn't exist on disk relative
    # to the platform's upload_dir at job-create time. The engine
    # itself never raises this - it's a platform-only check (the
    # engine doesn't know the upload_dir). Lives on the same code
    # namespace so the UI can route it next to the path field.
    SOURCE_FILE_FILE_NOT_FOUND = "source_file.file_not_found"
    SOURCE_FILE_UNSUPPORTED_FORMAT = "source_file.unsupported_format"
    SOURCE_FILE_BAD_HAS_HEADER_TYPE = "source_file.bad_has_header_type"
    SOURCE_FILE_NO_HEADER_COLUMNS = "source_file.no_header_columns"
    SOURCE_FILE_COLUMN_NAMES_WITH_HEADER = "source_file.column_names_with_header"
    SOURCE_FILE_BAD_DELIMITER = "source_file.bad_delimiter"
    SOURCE_FILE_BAD_ROW_LIMIT = "source_file.bad_row_limit"
    SOURCE_FILE_MISSING_FW_COLUMNS = "source_file.missing_fw_columns"
    SOURCE_FILE_CSV_PARAM_ON_NON_CSV = "source_file.csv_param_on_non_csv"

    # R3.10 generation key policy. Platform-only preflight codes -- the
    # engine doesn't know about admin settings, but the codes live in
    # the shared registry so the wire format stays uniform.
    GENERATION_KEY_REQUIRED = "generation.key_required"
    GENERATION_RANDOM_NOT_ALLOWED = "generation.random_not_allowed"

    # Generic catch-all for validators that haven't been migrated yet.
    # New gates should add a specific code rather than reusing this.
    UNTAGGED = "untagged"
