"""Catalog of the ``route`` namespace, part 1."""

from __future__ import annotations

from decoy_engine.reasons import _catalog

__all__ = [
    "ADAPTER_REQUESTED",
    "BUCKET_PERTURB_CONFIG_DECLINED",
    "BUCKET_PERTURB_SOURCE_NOT_STRING",
    "BYTE_ESTIMATE_BOUNDED_OUT_OF_CORE",
    "BYTE_ESTIMATE_FULL_FRAME_FITS",
    "CATEGORICAL_SOURCE_NOT_STRING",
    "CHUNKED_ADMITTED",
    "CHUNKED_EXTRA_SOURCE_FRAME",
    "CHUNKED_LAZY_SOURCE_UNSUPPORTED",
    "CHUNKED_SOURCE_BELOW_THRESHOLD",
    "CHUNKED_SOURCE_DTYPE_UNSTABLE",
    "CHUNKED_SOURCE_FRAME_MISSING",
    "CLOCK_DEPENDENT_WITHOUT_CONTROLLED_CLOCK",
    "CROSS_TABLE_CYCLE",
    "CRYPTO_EXTENSION_UNAVAILABLE",
    "CRYPTO_EXT_UNAVAILABLE",
    "CUSTOM_PROVIDER_WITHOUT_DETERMINISTIC_CONTRACT",
    "DEPENDS_ON_EXCLUDED_COLUMN",
    "DETERMINISM_FRESH",
    "DISPATCHER_DISABLED",
    "ELIGIBLE",
    "EMPTY_INPUT",
    "FAKER_METADATA_NOT_ALLOWLISTED",
    "FAKER_PROVIDER_NOT_NATIVE",
    "FAKER_PROVIDER_OUTPUT_NOT_STRING",
    "FAKER_SCHEMA_NOT_GUARANTEED",
    "FAKER_SOURCE_NOT_STRING",
    "FALLBACK_POLICY_NOT_NATIVE",
    "FIDELITY_REPORT_REQUESTED",
    "FK_RELATIONSHIP",
    "FORCED_OUT_OF_CORE_INCOMPATIBLE",
    "FORCED_OUT_OF_CORE_INELIGIBLE",
    "FORCED_OUT_OF_CORE_NO_MASK_TABLE",
    "FORCED_SEQUENTIAL_CYCLIC",
    "FORCED_SEQUENTIAL_INELIGIBLE",
    "FORCED_SEQUENTIAL_NO_MASK_TABLE",
    "FPE_CHECKSUM_NOT_NATIVE",
    "FPE_EXTENSION_UNAVAILABLE",
    "FPE_EXT_UNAVAILABLE",
    "FPE_JOIN_GROUP",
    "GENERATE_PLUS_MASK",
    "GENERATE_TABLES_PRESENT",
    "GROUP_KEY_MASKED_SIBLING",
    "GROUP_KEY_SELF_ANCHOR",
    "GROUP_KEY_SIBLING_MISSING",
    "GROUP_KEY_SIBLING_TYPE_NOT_NATIVE",
    "HASH_CONFIG_REJECTED",
    "HASH_INPUT_TYPE_NOT_NATIVE_ADMITTED",
    "HASH_INPUT_TYPE_TZ_TIMESTAMP_DEFERRED",
    "INDEX_EXTENSION_UNAVAILABLE",
    "INDEX_EXT_UNAVAILABLE",
    "LAZY_SOURCE_NULL_COUNT_UNAVAILABLE",
    "MASKS_ONE_TABLE_PER_RUN",
    "NON_PANDAS_SUBSTRATE_REQUESTED",
    "NON_SCALAR_COMPOSITE",
    "NON_SCALAR_NODE",
    "NO_MASK_NODES",
    "NO_MASK_TABLES",
    "NO_NATIVE_KERNEL_CALLS",
    "NO_NATIVE_KERNEL_OR_POOL",
    "NO_RELATIONSHIPS",
    "NO_RELATIONSHIP_ROUTE",
    "NO_ROWS_HASHED",
    "OTHER",
    "OUT_OF_CORE_BELOW_THRESHOLD",
    "OUT_OF_CORE_CROSS_TABLE_CYCLE",
    "OUT_OF_CORE_FK_KEY_DTYPE_HAZARD",
    "OUT_OF_CORE_INCOMPATIBLE",
    "OUT_OF_CORE_LARGE_FK",
    "OUT_OF_CORE_NO_MASK_TABLE",
    "OUT_OF_CORE_NO_SIZE_SIGNAL",
    "OUT_OF_CORE_READY",
    "OVERRIDE_FULL_FRAME",
    "OVERRIDE_OUT_OF_CORE",
    "PROBE_RECOVERED_FULL_FRAME",
    "PURE_MASK_FK",
    "RAW_HEX_EXT_UNAVAILABLE",
    "RELATIONSHIP_ROUTE_DEFERRED",
    "REMAP_INPUT_NOT_ARROW",
    "REMAP_PARENT_STAYS_PYTHON",
    "RESIDENT_SOURCE",
    "SEQUENTIAL_BUCKET_PERTURB_DATE_OUT_OF_NATIVE_RANGE",
    "SEQUENTIAL_FK_KEY_INJECTIVITY_NOT_GUARANTEED",
    "SLICE_NATIVE_ADMITTED",
    "SOURCE_TYPE_NOT_STRING",
    "STRATEGY_VETOED_CHUNKED",
    "TEXT_MASK_EXTENSION_UNAVAILABLE",
    "TEXT_REDACT_EXTENSION_UNAVAILABLE",
    "UNCONFIGURED_SET_MISMATCH",
    "UNCOVERED_OR_MISSING_COLUMNS",
    "VALIDATORS_PRESENT",
    "VAULT_WRITER_REQUESTED",
    "WHEN_OUTSIDE_SUBSET",
    "WHEN_PREDICATE_NOT_NATIVE",
    "WHEN_READS_MASKED_COLUMN",
]

_d = _catalog.route("RouteDeclineCode")

ADAPTER_REQUESTED = _d("adapter_requested", "Adapter requested.")
CRYPTO_EXT_UNAVAILABLE = _d("crypto_ext_unavailable", "Crypto extension unavailable.")
EMPTY_INPUT = _d("empty_input", "Empty input.")
FAKER_METADATA_NOT_ALLOWLISTED = _d(
    "faker_metadata_not_allowlisted", "Faker metadata not allowlisted."
)
FAKER_PROVIDER_NOT_NATIVE = _d("faker_provider_not_native", "Faker provider not native.")
FAKER_PROVIDER_OUTPUT_NOT_STRING = _d(
    "faker_provider_output_not_string", "Faker provider output not string."
)
FAKER_SCHEMA_NOT_GUARANTEED = _d("faker_schema_not_guaranteed", "Faker schema not guaranteed.")
FAKER_SOURCE_NOT_STRING = _d("faker_source_not_string", "Faker source not string.")
FALLBACK_POLICY_NOT_NATIVE = _d("fallback_policy_not_native", "Fallback policy not native.")
FK_RELATIONSHIP = _d("fk_relationship", "FK relationship.")
FPE_CHECKSUM_NOT_NATIVE = _d("fpe_checksum_not_native", "FPE checksum not native.")
FPE_EXT_UNAVAILABLE = _d("fpe_ext_unavailable", "FPE extension unavailable.")
GROUP_KEY_MASKED_SIBLING = _d("group_key_masked_sibling", "Group key masked sibling.")
GROUP_KEY_SELF_ANCHOR = _d("group_key_self_anchor", "Group key self anchor.")
GROUP_KEY_SIBLING_MISSING = _d("group_key_sibling_missing", "Group key sibling missing.")
GROUP_KEY_SIBLING_TYPE_NOT_NATIVE = _d(
    "group_key_sibling_type_not_native", "Group key sibling type not native."
)
HASH_CONFIG_REJECTED = _d("hash_config_rejected", "Hash config rejected.")
INDEX_EXT_UNAVAILABLE = _d("index_ext_unavailable", "Index extension unavailable.")
NO_MASK_NODES = _d("no_mask_nodes", "No mask nodes.")
NO_NATIVE_KERNEL_OR_POOL = _d("no_native_kernel_or_pool", "No native kernel or pool.")
NON_SCALAR_NODE = _d("non_scalar_node", "Non scalar node.")
OTHER = _d("other", "Other.")
RAW_HEX_EXT_UNAVAILABLE = _d("raw_hex_ext_unavailable", "Raw hex extension unavailable.")
SOURCE_TYPE_NOT_STRING = _d("source_type_not_string", "Source type not string.")
STRATEGY_VETOED_CHUNKED = _d("strategy_vetoed_chunked", "Strategy vetoed chunked.")
UNCONFIGURED_SET_MISMATCH = _d("unconfigured_set_mismatch", "Unconfigured set mismatch.")
UNCOVERED_OR_MISSING_COLUMNS = _d("uncovered_or_missing_columns", "Uncovered or missing columns.")
WHEN_OUTSIDE_SUBSET = _d("when_outside_subset", "When outside subset.")
WHEN_PREDICATE_NOT_NATIVE = _d("when_predicate_not_native", "When predicate not native.")
WHEN_READS_MASKED_COLUMN = _d("when_reads_masked_column", "When reads masked column.")

_d = _catalog.route("decoy_engine.execution._chunked_input")

ELIGIBLE = _d("eligible", "Eligible.")
RESIDENT_SOURCE = _d("resident_source", "Resident source.")

_d = _catalog.route("decoy_engine.execution._pipeline_auto_chunk")

DISPATCHER_DISABLED = _d("dispatcher_disabled", "Dispatcher disabled.")

_d = _catalog.route("decoy_engine.execution._sequential_native")

REMAP_INPUT_NOT_ARROW = _d("remap_input_not_arrow", "Remap input not Arrow.")
REMAP_PARENT_STAYS_PYTHON = _d("remap_parent_stays_python", "Remap parent stays python.")
SEQUENTIAL_BUCKET_PERTURB_DATE_OUT_OF_NATIVE_RANGE = _d(
    "sequential_bucket_perturb_date_out_of_native_range",
    "Sequential bucket perturb date out of native range.",
)
SEQUENTIAL_FK_KEY_INJECTIVITY_NOT_GUARANTEED = _d(
    "sequential_fk_key_injectivity_not_guaranteed", "Sequential FK key injectivity not guaranteed."
)

_d = _catalog.route("decoy_engine.execution._sequential_native_decisions")

NO_ROWS_HASHED = _d("no_rows_hashed", "No rows hashed.")

_d = _catalog.route("decoy_engine.execution.out_of_core._bucket_native")

BUCKET_PERTURB_CONFIG_DECLINED = _d(
    "bucket_perturb_config_declined", "Bucket perturb config declined."
)
BUCKET_PERTURB_SOURCE_NOT_STRING = _d(
    "bucket_perturb_source_not_string", "Bucket perturb source not string."
)
INDEX_EXTENSION_UNAVAILABLE = _d("index_extension_unavailable", "Index extension unavailable.")
NO_NATIVE_KERNEL_CALLS = _d("no_native_kernel_calls", "No native kernel calls.")

_d = _catalog.route("decoy_engine.execution.out_of_core._categorical_native")

CATEGORICAL_SOURCE_NOT_STRING = _d(
    "categorical_source_not_string", "Categorical source not string."
)

_d = _catalog.route("decoy_engine.execution.out_of_core._fpe_native")

FPE_EXTENSION_UNAVAILABLE = _d("fpe_extension_unavailable", "FPE extension unavailable.")

_d = _catalog.route("decoy_engine.execution.out_of_core._hash_native")

CRYPTO_EXTENSION_UNAVAILABLE = _d("crypto_extension_unavailable", "Crypto extension unavailable.")
HASH_INPUT_TYPE_NOT_NATIVE_ADMITTED = _d(
    "hash_input_type_not_native_admitted", "Hash input type not native admitted."
)
HASH_INPUT_TYPE_TZ_TIMESTAMP_DEFERRED = _d(
    "hash_input_type_tz_timestamp_deferred", "Hash input type timezone timestamp deferred."
)

_d = _catalog.route("decoy_engine.execution.out_of_core._text_mask_native")

TEXT_MASK_EXTENSION_UNAVAILABLE = _d(
    "text_mask_extension_unavailable", "Text mask extension unavailable."
)

_d = _catalog.route("decoy_engine.execution.out_of_core._text_redact_native")

TEXT_REDACT_EXTENSION_UNAVAILABLE = _d(
    "text_redact_extension_unavailable", "Text redact extension unavailable."
)

_d = _catalog.route("decoy_engine.execution.physical._reasons")

BYTE_ESTIMATE_BOUNDED_OUT_OF_CORE = _d(
    "byte_estimate_bounded_out_of_core", "Byte estimate bounded out of core."
)
BYTE_ESTIMATE_FULL_FRAME_FITS = _d(
    "byte_estimate_full_frame_fits", "Byte estimate full frame fits."
)
CHUNKED_ADMITTED = _d("chunked_admitted", "Chunked admitted.")
CHUNKED_EXTRA_SOURCE_FRAME = _d("chunked_extra_source_frame", "Chunked extra source frame.")
CHUNKED_LAZY_SOURCE_UNSUPPORTED = _d(
    "chunked_lazy_source_unsupported", "Chunked lazy source unsupported."
)
CHUNKED_SOURCE_BELOW_THRESHOLD = _d(
    "chunked_source_below_threshold", "Chunked source below threshold."
)
CHUNKED_SOURCE_DTYPE_UNSTABLE = _d(
    "chunked_source_dtype_unstable", "Chunked source dtype unstable."
)
CHUNKED_SOURCE_FRAME_MISSING = _d("chunked_source_frame_missing", "Chunked source frame missing.")
CROSS_TABLE_CYCLE = _d("cross_table_cycle", "Cross table cycle.")
FIDELITY_REPORT_REQUESTED = _d("fidelity_report_requested", "Fidelity report requested.")
FORCED_OUT_OF_CORE_INCOMPATIBLE = _d(
    "forced_out_of_core_incompatible", "Forced out of core incompatible."
)
FORCED_OUT_OF_CORE_INELIGIBLE = _d(
    "forced_out_of_core_ineligible", "Forced out of core ineligible."
)
FORCED_OUT_OF_CORE_NO_MASK_TABLE = _d(
    "forced_out_of_core_no_mask_table", "Forced out of core no mask table."
)
FORCED_SEQUENTIAL_CYCLIC = _d("forced_sequential_cyclic", "Forced sequential cyclic.")
FORCED_SEQUENTIAL_INELIGIBLE = _d("forced_sequential_ineligible", "Forced sequential ineligible.")
FORCED_SEQUENTIAL_NO_MASK_TABLE = _d(
    "forced_sequential_no_mask_table", "Forced sequential no mask table."
)
FPE_JOIN_GROUP = _d("fpe_join_group", "FPE join group.")
GENERATE_PLUS_MASK = _d("generate_plus_mask", "Generate plus mask.")
GENERATE_TABLES_PRESENT = _d("generate_tables_present", "Generate tables present.")
LAZY_SOURCE_NULL_COUNT_UNAVAILABLE = _d(
    "lazy_source_null_count_unavailable", "Lazy source null count unavailable."
)
MASKS_ONE_TABLE_PER_RUN = _d("masks_one_table_per_run", "Masks one table per run.")
NO_MASK_TABLES = _d("no_mask_tables", "No mask tables.")
NO_RELATIONSHIP_ROUTE = _d("no_relationship_route", "No relationship route.")
NO_RELATIONSHIPS = _d("no_relationships", "No relationships.")
NON_PANDAS_SUBSTRATE_REQUESTED = _d(
    "non_pandas_substrate_requested", "Non pandas substrate requested."
)
NON_SCALAR_COMPOSITE = _d("non_scalar_composite", "Non scalar composite.")
OUT_OF_CORE_BELOW_THRESHOLD = _d("out_of_core_below_threshold", "Out of core below threshold.")
OUT_OF_CORE_CROSS_TABLE_CYCLE = _d(
    "out_of_core_cross_table_cycle", "Out of core cross table cycle."
)
OUT_OF_CORE_FK_KEY_DTYPE_HAZARD = _d(
    "out_of_core_fk_key_dtype_hazard", "Out of core FK key dtype hazard."
)
OUT_OF_CORE_INCOMPATIBLE = _d("out_of_core_incompatible", "Out of core incompatible.")
OUT_OF_CORE_LARGE_FK = _d("out_of_core_large_fk", "Out of core large FK.")
OUT_OF_CORE_NO_MASK_TABLE = _d("out_of_core_no_mask_table", "Out of core no mask table.")
OUT_OF_CORE_NO_SIZE_SIGNAL = _d("out_of_core_no_size_signal", "Out of core no size signal.")
OUT_OF_CORE_READY = _d("out_of_core_ready", "Out of core ready.")
OVERRIDE_FULL_FRAME = _d("override_full_frame", "Override full frame.")
OVERRIDE_OUT_OF_CORE = _d("override_out_of_core", "Override out of core.")
PROBE_RECOVERED_FULL_FRAME = _d("probe_recovered_full_frame", "Probe recovered full frame.")
PURE_MASK_FK = _d("pure_mask_fk", "Pure mask FK.")
RELATIONSHIP_ROUTE_DEFERRED = _d("relationship_route_deferred", "Relationship route deferred.")
VALIDATORS_PRESENT = _d("validators_present", "Validators present.")
VAULT_WRITER_REQUESTED = _d("vault_writer_requested", "Vault writer requested.")

_d = _catalog.route("decoy_engine.execution.physical._shadow_bindings")

SLICE_NATIVE_ADMITTED = _d("slice_native_admitted", "Slice native admitted.")

_d = _catalog.route("decoy_engine.generation._reproducibility")

CLOCK_DEPENDENT_WITHOUT_CONTROLLED_CLOCK = _d(
    "clock_dependent_without_controlled_clock", "Clock dependent without controlled clock."
)
CUSTOM_PROVIDER_WITHOUT_DETERMINISTIC_CONTRACT = _d(
    "custom_provider_without_deterministic_contract",
    "Custom provider without deterministic contract.",
)
DEPENDS_ON_EXCLUDED_COLUMN = _d("depends_on_excluded_column", "Depends on excluded column.")
DETERMINISM_FRESH = _d("determinism_fresh", "Determinism fresh.")
