"""The engine `FileAccess` policy: every file the engine opens goes through one object.

A caller passes `file_access=` to an entry point (or installs nothing and gets today's
ambient CLI behavior). The policy turns a `FileRequest` (role, bare locator, config position)
into an already-authorized capability: a `FileHandle`, `DirHandle` or `OutputDir`. Libraries
see only a handle's descriptor path, and only through the adapter modules. A process that must
never fall back to ambient access calls `forbid_ambient()` once at startup.

Docs: docs/security/file-access-policy.md. Pattern: object capabilities (see `_types`).
"""

from __future__ import annotations

from decoy_engine.fileaccess._entry import (
    ambient_forbidden,
    at_position,
    bind_iterator,
    capture,
    current,
    entry_point,
    forbid_ambient,
    installed,
    is_ambient,
    map_in_context,
    position_key,
    require_ambient,
    run_in_context,
    scope,
)
from decoy_engine.fileaccess._errors import encode_field_key, redact_locators, scrub
from decoy_engine.fileaccess._local import LocalFileAccess, engine_dir_handle, engine_handle
from decoy_engine.fileaccess._preopened import PreopenedFileAccess
from decoy_engine.fileaccess._read import keeps_class, read_all, read_text, shown
from decoy_engine.fileaccess._rooted import Root, RootedFileAccess
from decoy_engine.fileaccess._types import (
    CONTRACT_VERSION,
    DirHandle,
    FileAccess,
    FileAccessDenied,
    FileAccessReadError,
    FileAccessUnavailable,
    FileHandle,
    FileRequest,
    FileRole,
    OutputDir,
    OutputTxn,
)

# Public callables that install a policy for their call. A sentry fails on any public
# callable that reaches `current()` without being listed and accepting `file_access=`.
ENTRY_POINTS: tuple[str, ...] = (
    "decoy_engine.data_discovery.run_discovery_sql",
    "decoy_engine.execution._chunked.run_mask_pipeline_chunked",
    "decoy_engine.execution._governor.run_job_with_governor",
    "decoy_engine.execution._isolated_run.run_pipeline_isolated",
    "decoy_engine.execution._job_estimate.estimate_job",
    "decoy_engine.execution._pipeline.run_pipeline",
    "decoy_engine.execution._sequential.run_sequential",
    "decoy_engine.execution._transforms_table.apply_table_transforms",
    "decoy_engine.execution.capacity.estimate_job_capacity",
    "decoy_engine.execution.native._chunked_entry.run_mask_chunked",
    "decoy_engine.execution.out_of_core._runner.run_fk_out_of_core",
    "decoy_engine.generation._pinned_compile.compile_pinned_plan",
    "decoy_engine.generation._pinned_compile.compile_pinned_plan_with_generation",
    "decoy_engine.generation._pinned_execute.execute_pinned",
    "decoy_engine.generation._plan_entry.generate_tables",
    "decoy_engine.generation.window.generate_rows",
    "decoy_engine.generators.derivation.strategy_config_fingerprint",
    "decoy_engine.internal.faker_setup.load_custom_providers",
    "decoy_engine.io.bounded_reader.iter_csv_chunks",
    "decoy_engine.io.bounded_reader.iter_parquet_chunks",
    "decoy_engine.keyprovider.key_provider_from_ref",
    "decoy_engine.keyprovider.resolve_mask_secret_ref",
    "decoy_engine.plan._compile.compile_plan",
    "decoy_engine.plan._compile.run_config_only_checks",
    "decoy_engine.plan._serialize.plan_from_yaml",
    "decoy_engine.plan.validate._consolidator.validate_plan",
    "decoy_engine.profile._fixed_width_reader.read_fixed_width",
    "decoy_engine.profile._fixed_width_writer.write_fixed_width",
    "decoy_engine.profile._source.profile_source",
    "decoy_engine.profiling.profiler.profile_columns",
    "decoy_engine.quarantine.apply_quarantine",
    "decoy_engine.reference_tables._loader.load_table",
    "decoy_engine.subset._api.plan_subset",
    "decoy_engine.subset._api.run_subset",
    "decoy_engine.subset._preflight.run_subset_preflight",
    "decoy_engine.transforms._codeset_loader.load_corpus",
    "decoy_engine.transforms._codeset_loader.load_corpus_provenance",
    "decoy_engine.transforms._codeset_loader.verify_corpus",
    "decoy_engine.transforms.joint_mask.validate_joint_mask_config",
    "decoy_engine.unmask.unmask_pipeline",
    "decoy_engine.validation._config.validate_config",
    "decoy_engine.vault.load_vault",
    "decoy_engine.vault.vault_writer_for_config",
)

__all__ = [
    "CONTRACT_VERSION",
    "ENTRY_POINTS",
    "DirHandle",
    "FileAccess",
    "FileAccessDenied",
    "FileAccessReadError",
    "FileAccessUnavailable",
    "FileHandle",
    "FileRequest",
    "FileRole",
    "LocalFileAccess",
    "OutputDir",
    "OutputTxn",
    "PreopenedFileAccess",
    "Root",
    "RootedFileAccess",
    "ambient_forbidden",
    "at_position",
    "bind_iterator",
    "capture",
    "current",
    "encode_field_key",
    "engine_dir_handle",
    "engine_handle",
    "entry_point",
    "forbid_ambient",
    "installed",
    "is_ambient",
    "keeps_class",
    "map_in_context",
    "position_key",
    "read_all",
    "read_text",
    "redact_locators",
    "require_ambient",
    "run_in_context",
    "scope",
    "scrub",
    "shown",
]
