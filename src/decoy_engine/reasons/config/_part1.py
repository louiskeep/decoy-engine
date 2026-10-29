"""Catalog of the ``config`` namespace, part 1."""

from __future__ import annotations

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import Kind, Outcome, RetryClass, Severity

__all__ = [
    "CLOUD_IO_UNSUPPORTED_FORMAT",
    "EDGES_BAD_TYPE",
    "EDGE_BAD_TYPE",
    "EDGE_PORT_ON_NON_SPLIT",
    "EDGE_UNKNOWN_FROM_NODE",
    "EDGE_UNKNOWN_PORT",
    "EDGE_UNKNOWN_TO_NODE",
    "FK_CUSTOM_PROVIDER_UNREGISTERED",
    "FK_EMPTY_PARENT_POOL",
    "FK_INELIGIBLE_CHILD_KIND",
    "FK_M2M_BAD_POOL",
    "FK_M2M_UNKNOWN_COLUMN",
    "FK_M2M_UNKNOWN_NODE",
    "FK_MULTI_PARENT_BAD_SHAPE",
    "FK_NONDETERMINISTIC_MASK",
    "FK_PARALLEL_BRANCHES",
    "FK_PARENT_AFTER_CHILD",
    "FK_PARENT_UNRESOLVED",
    "FK_SELF_CYCLE",
    "FK_SELF_REFERENCE",
    "FK_SELF_REF_INERT",
    "FK_SEQUENTIAL_BOUNDS_CONFLICT",
    "FK_UNKNOWN_COLUMN",
    "FK_UNKNOWN_NODE",
    "GENERATION_KEY_REQUIRED",
    "GENERATION_RANDOM_NOT_ALLOWED",
    "GRAPH_CYCLE",
    "GRAPH_FORMAT_MISMATCH",
    "GRAPH_NODE_INSUFFICIENT_INPUTS",
    "GRAPH_NODE_TOO_MANY_INPUTS",
    "GRAPH_SINK_HAS_OUTPUTS",
    "MASK_BAD_COLUMNS_TYPE",
    "MASK_BAD_COLUMN_SPEC_TYPE",
    "MASK_FORMULA_MISSING",
    "MASK_REFERENCE_MISSING",
    "MASK_UNKNOWN_COLUMN",
    "MASK_UNKNOWN_STRATEGY",
    "NAMESPACE_AMBIGUITY",
    "NAMESPACE_MISSING",
    "NODES_EMPTY_LIST",
    "NODES_REF_NOT_UPSTREAM",
    "NODES_REF_UNKNOWN_ID",
    "NODE_BAD_CONFIG_TYPE",
    "NODE_BAD_ID",
    "NODE_BAD_NAME",
    "NODE_BAD_NATIVE_ENGINE",
    "NODE_BAD_TYPE",
    "NODE_DUPLICATE_ID",
    "NODE_UNKNOWN_KIND",
    "PK_DUPLICATES",
    "SOURCE_FILE_BAD_DELIMITER",
    "SOURCE_FILE_BAD_HAS_HEADER_TYPE",
    "SOURCE_FILE_BAD_ROW_LIMIT",
    "SOURCE_FILE_COLUMN_COUNT_MISMATCH",
    "SOURCE_FILE_COLUMN_NAMES_WITH_HEADER",
    "SOURCE_FILE_CSV_PARAM_ON_NON_CSV",
    "SOURCE_FILE_FILE_NOT_FOUND",
    "SOURCE_FILE_LONE_CR",
    "SOURCE_FILE_MISSING_FW_COLUMNS",
    "SOURCE_FILE_MISSING_PATH",
    "SOURCE_FILE_NO_HEADER_COLUMNS",
    "SOURCE_FILE_NUL_BYTE",
    "SOURCE_FILE_UNSUPPORTED_FORMAT",
    "SOURCE_GCS_MISSING_BUCKET",
    "SOURCE_GCS_MISSING_CREDENTIALS",
    "SOURCE_GCS_MISSING_PATH",
    "SOURCE_S3_MISSING_BUCKET",
    "SOURCE_S3_MISSING_CREDENTIALS",
    "SOURCE_S3_MISSING_PATH",
    "SOURCE_SFTP_MISSING_AUTH",
    "SOURCE_SFTP_MISSING_HOST",
    "SOURCE_SFTP_MISSING_PATH",
    "SOURCE_SFTP_MISSING_USERNAME",
    "SUBSET_CONFIG_MISSING",
    "SUBSET_DANGLING_NOT_ACKNOWLEDGED",
    "SUBSET_DUPLICATE_SEED_TABLE",
    "SUBSET_OUTPUT_DIR_EXISTS",
    "SUBSET_SEED_KEY_TYPE",
    "SUBSET_SEED_KEY_UNCANONICALIZABLE",
    "SUBSET_SOURCE_TYPE_UNSUPPORTED",
    "SUBSET_UNKNOWN_EDGE",
    "TARGET_FILE_FORMAT_INFERRED",
    "TARGET_FILE_MISSING_OUTPUT_FILENAME",
    "TARGET_FILE_PATH_OUTSIDE_OUTPUT_DIR",
    "TARGET_FILE_UNSUPPORTED_FORMAT",
    "TARGET_GCS_MISSING_BUCKET",
    "TARGET_GCS_MISSING_CREDENTIALS",
    "TARGET_GCS_MISSING_PATH",
    "TARGET_S3_MISSING_BUCKET",
    "TARGET_S3_MISSING_CREDENTIALS",
    "TARGET_S3_MISSING_PATH",
    "TARGET_SFTP_MISSING_AUTH",
    "TARGET_SFTP_MISSING_HOST",
    "TARGET_SFTP_MISSING_PATH",
    "TARGET_SFTP_MISSING_USERNAME",
    "TOP_LEVEL_BAD_ENGINE",
    "TOP_LEVEL_BAD_MODE",
    "TOP_LEVEL_BAD_SCHEMA_VERSION",
    "UNTAGGED",
    "WHEN_OUTSIDE_CLOSED_GRAMMAR",
]

_d = _catalog.config("NamespaceConfigError")

NAMESPACE_AMBIGUITY = _d("namespace_ambiguity", "Namespace ambiguity.")
NAMESPACE_MISSING = _d("namespace_missing", "Namespace missing.")

_d = _catalog.config("SubsetConfigError")

SUBSET_CONFIG_MISSING = _d("subset_config_missing", "Subset config missing.")
SUBSET_DANGLING_NOT_ACKNOWLEDGED = _d(
    "subset_dangling_not_acknowledged", "Subset dangling not acknowledged."
)
SUBSET_DUPLICATE_SEED_TABLE = _d("subset_duplicate_seed_table", "Subset duplicate seed table.")
SUBSET_OUTPUT_DIR_EXISTS = _d("subset_output_dir_exists", "Subset output dir exists.")
SUBSET_SEED_KEY_TYPE = _d("subset_seed_key_type", "Subset seed key type.")
SUBSET_SEED_KEY_UNCANONICALIZABLE = _d(
    "subset_seed_key_uncanonicalizable", "Subset seed key uncanonicalizable."
)
SUBSET_SOURCE_TYPE_UNSUPPORTED = _d(
    "subset_source_type_unsupported", "Subset source type unsupported."
)
SUBSET_UNKNOWN_EDGE = _d("subset_unknown_edge", "Subset unknown edge.")

_d = _catalog.config("ValidationError")

WHEN_OUTSIDE_CLOSED_GRAMMAR = _d("when_outside_closed_grammar", "When outside closed grammar.")

_d = _catalog.config("decoy_engine.validation_result")

CLOUD_IO_UNSUPPORTED_FORMAT = _d("cloud_io.unsupported_format", "Cloud I/O unsupported format.")
EDGE_BAD_TYPE = _d("edge.bad_type", "Edge bad type.")
EDGE_PORT_ON_NON_SPLIT = _d("edge.port_on_non_split", "Edge port on non split.")
EDGE_UNKNOWN_FROM_NODE = _d("edge.unknown_from_node", "Edge unknown from node.")
EDGE_UNKNOWN_PORT = _d("edge.unknown_port", "Edge unknown port.")
EDGE_UNKNOWN_TO_NODE = _d("edge.unknown_to_node", "Edge unknown to node.")
EDGES_BAD_TYPE = _d("edges.bad_type", "Edges bad type.")
FK_CUSTOM_PROVIDER_UNREGISTERED = _d(
    "fk.custom_provider_unregistered", "FK custom provider unregistered."
)
FK_EMPTY_PARENT_POOL = _d("fk.empty_parent_pool", "FK empty parent pool.")
FK_INELIGIBLE_CHILD_KIND = _d("fk.ineligible_child_kind", "FK ineligible child kind.")
FK_M2M_BAD_POOL = _d("fk.m2m_bad_pool", "FK m2m bad pool.")
FK_M2M_UNKNOWN_COLUMN = _d("fk.m2m_unknown_column", "FK m2m unknown column.")
FK_M2M_UNKNOWN_NODE = _d("fk.m2m_unknown_node", "FK m2m unknown node.")
FK_MULTI_PARENT_BAD_SHAPE = _d("fk.multi_parent_bad_shape", "FK multi parent bad shape.")
FK_NONDETERMINISTIC_MASK = _d("fk.nondeterministic_mask", "FK nondeterministic mask.")
FK_PARALLEL_BRANCHES = _d("fk.parallel_branches", "FK parallel branches.")
FK_PARENT_AFTER_CHILD = _d("fk.parent_after_child", "FK parent after child.")
FK_PARENT_UNRESOLVED = _d("fk.parent_unresolved", "FK parent unresolved.")
FK_SELF_CYCLE = _d("fk.self_cycle", "FK self cycle.")
FK_SELF_REF_INERT = _d(
    "fk.self_ref_inert",
    "FK self ref inert.",
    kind=Kind.WARNING,
    severity=Severity.WARNING,
    outcome=Outcome.FINDINGS,
    retry=RetryClass.NEVER,
)
FK_SELF_REFERENCE = _d("fk.self_reference", "FK self reference.")
FK_SEQUENTIAL_BOUNDS_CONFLICT = _d(
    "fk.sequential_bounds_conflict", "FK sequential bounds conflict."
)
FK_UNKNOWN_COLUMN = _d("fk.unknown_column", "FK unknown column.")
FK_UNKNOWN_NODE = _d("fk.unknown_node", "FK unknown node.")
GENERATION_KEY_REQUIRED = _d("generation.key_required", "Generation key required.")
GENERATION_RANDOM_NOT_ALLOWED = _d(
    "generation.random_not_allowed", "Generation random not allowed."
)
GRAPH_CYCLE = _d("graph.cycle", "Graph cycle.")
GRAPH_FORMAT_MISMATCH = _d(
    "graph.format_mismatch",
    "Graph format mismatch.",
    kind=Kind.WARNING,
    severity=Severity.WARNING,
    outcome=Outcome.FINDINGS,
    retry=RetryClass.NEVER,
)
GRAPH_NODE_INSUFFICIENT_INPUTS = _d(
    "graph.node_insufficient_inputs", "Graph node insufficient inputs."
)
GRAPH_NODE_TOO_MANY_INPUTS = _d("graph.node_too_many_inputs", "Graph node too many inputs.")
GRAPH_SINK_HAS_OUTPUTS = _d("graph.sink_has_outputs", "Graph sink has outputs.")
MASK_BAD_COLUMN_SPEC_TYPE = _d("mask.bad_column_spec_type", "Mask bad column spec type.")
MASK_BAD_COLUMNS_TYPE = _d("mask.bad_columns_type", "Mask bad columns type.")
MASK_FORMULA_MISSING = _d("mask.formula_missing", "Mask formula missing.")
MASK_REFERENCE_MISSING = _d("mask.reference_missing", "Mask reference missing.")
MASK_UNKNOWN_COLUMN = _d("mask.unknown_column", "Mask unknown column.")
MASK_UNKNOWN_STRATEGY = _d("mask.unknown_strategy", "Mask unknown strategy.")
NODE_BAD_CONFIG_TYPE = _d("node.bad_config_type", "Node bad config type.")
NODE_BAD_ID = _d("node.bad_id", "Node bad ID.")
NODE_BAD_NAME = _d("node.bad_name", "Node bad name.")
NODE_BAD_NATIVE_ENGINE = _d("node.bad_native_engine", "Node bad native engine.")
NODE_BAD_TYPE = _d("node.bad_type", "Node bad type.")
NODE_DUPLICATE_ID = _d("node.duplicate_id", "Node duplicate ID.")
NODE_UNKNOWN_KIND = _d("node.unknown_kind", "Node unknown kind.")
NODES_EMPTY_LIST = _d("nodes.empty_list", "Nodes empty list.")
NODES_REF_NOT_UPSTREAM = _d("nodes_ref.not_upstream", "Nodes ref not upstream.")
NODES_REF_UNKNOWN_ID = _d("nodes_ref.unknown_id", "Nodes ref unknown ID.")
PK_DUPLICATES = _d("pk.duplicates", "A primary-key column contains duplicate values.")
SOURCE_FILE_BAD_DELIMITER = _d("source_file.bad_delimiter", "Source file bad delimiter.")
SOURCE_FILE_BAD_HAS_HEADER_TYPE = _d(
    "source_file.bad_has_header_type", "Source file bad has header type."
)
SOURCE_FILE_BAD_ROW_LIMIT = _d("source_file.bad_row_limit", "Source file bad row limit.")
SOURCE_FILE_COLUMN_COUNT_MISMATCH = _d(
    "source_file.column_count_mismatch", "Source file column count mismatch."
)
SOURCE_FILE_COLUMN_NAMES_WITH_HEADER = _d(
    "source_file.column_names_with_header", "Source file column names with header."
)
SOURCE_FILE_CSV_PARAM_ON_NON_CSV = _d(
    "source_file.csv_param_on_non_csv", "Source file CSV param on non CSV."
)
SOURCE_FILE_FILE_NOT_FOUND = _d("source_file.file_not_found", "Source file file not found.")
SOURCE_FILE_LONE_CR = _d("source_file.lone_cr", "Source file lone CR.")
SOURCE_FILE_MISSING_FW_COLUMNS = _d(
    "source_file.missing_fw_columns", "Source file missing fw columns."
)
SOURCE_FILE_MISSING_PATH = _d("source_file.missing_path", "Source file missing path.")
SOURCE_FILE_NO_HEADER_COLUMNS = _d(
    "source_file.no_header_columns", "Source file no header columns."
)
SOURCE_FILE_NUL_BYTE = _d("source_file.nul_byte", "Source file NUL byte.")
SOURCE_FILE_UNSUPPORTED_FORMAT = _d(
    "source_file.unsupported_format", "Source file unsupported format."
)
SOURCE_GCS_MISSING_BUCKET = _d("source_gcs.missing_bucket", "Source GCS missing bucket.")
SOURCE_GCS_MISSING_CREDENTIALS = _d(
    "source_gcs.missing_credentials", "Source GCS missing credentials."
)
SOURCE_GCS_MISSING_PATH = _d("source_gcs.missing_path", "Source GCS missing path.")
SOURCE_S3_MISSING_BUCKET = _d("source_s3.missing_bucket", "Source S3 missing bucket.")
SOURCE_S3_MISSING_CREDENTIALS = _d(
    "source_s3.missing_credentials", "Source S3 missing credentials."
)
SOURCE_S3_MISSING_PATH = _d("source_s3.missing_path", "Source S3 missing path.")
SOURCE_SFTP_MISSING_AUTH = _d("source_sftp.missing_auth", "Source SFTP missing auth.")
SOURCE_SFTP_MISSING_HOST = _d("source_sftp.missing_host", "Source SFTP missing host.")
SOURCE_SFTP_MISSING_PATH = _d("source_sftp.missing_path", "Source SFTP missing path.")
SOURCE_SFTP_MISSING_USERNAME = _d("source_sftp.missing_username", "Source SFTP missing username.")
TARGET_FILE_FORMAT_INFERRED = _d("target_file.format_inferred", "Target file format inferred.")
TARGET_FILE_MISSING_OUTPUT_FILENAME = _d(
    "target_file.missing_output_filename", "Target file missing output filename."
)
TARGET_FILE_PATH_OUTSIDE_OUTPUT_DIR = _d(
    "target_file.path_outside_output_dir", "Target file path outside output dir."
)
TARGET_FILE_UNSUPPORTED_FORMAT = _d(
    "target_file.unsupported_format", "Target file unsupported format."
)
TARGET_GCS_MISSING_BUCKET = _d("target_gcs.missing_bucket", "Target GCS missing bucket.")
TARGET_GCS_MISSING_CREDENTIALS = _d(
    "target_gcs.missing_credentials", "Target GCS missing credentials."
)
TARGET_GCS_MISSING_PATH = _d("target_gcs.missing_path", "Target GCS missing path.")
TARGET_S3_MISSING_BUCKET = _d("target_s3.missing_bucket", "Target S3 missing bucket.")
TARGET_S3_MISSING_CREDENTIALS = _d(
    "target_s3.missing_credentials", "Target S3 missing credentials."
)
TARGET_S3_MISSING_PATH = _d("target_s3.missing_path", "Target S3 missing path.")
TARGET_SFTP_MISSING_AUTH = _d("target_sftp.missing_auth", "Target SFTP missing auth.")
TARGET_SFTP_MISSING_HOST = _d("target_sftp.missing_host", "Target SFTP missing host.")
TARGET_SFTP_MISSING_PATH = _d("target_sftp.missing_path", "Target SFTP missing path.")
TARGET_SFTP_MISSING_USERNAME = _d("target_sftp.missing_username", "Target SFTP missing username.")
TOP_LEVEL_BAD_ENGINE = _d("top_level.bad_engine", "Top level bad engine.")
TOP_LEVEL_BAD_MODE = _d("top_level.bad_mode", "Top level bad mode.")
TOP_LEVEL_BAD_SCHEMA_VERSION = _d("top_level.bad_schema_version", "Top level bad schema version.")
UNTAGGED = _d("untagged", "A validation failure with no registered code.")
