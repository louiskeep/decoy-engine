"""Registry of graph node ops.

Maps the YAML `kind:` string to the op module that implements it. Each op
module conforms to the protocol in `_base.py`.

To add a new kind: write the op module, import it here, register it in OPS.
The validator's "unknown kind" rejection is driven directly off OPS.
"""

from decoy_engine.graph.ops import (
    convert_file_type,
    dedupe,
    derive,
    drop_column,
    filter_op,
    flag_gate,
    generate_op,
    if_router,
    iterate_files,
    iterate_fixed,
    iterate_loop,
    join,
    limit,
    mask_op,
    run_storm,
    select_column,
    sort,
    source_db,
    source_file,
    source_gcs,
    source_s3,
    source_sftp,
    sql_run,
    sub_pipeline,
    target_db,
    target_file,
    target_gcs,
    target_s3,
    target_sftp,
)

OPS: dict[str, object] = {
    "source.db": source_db,
    "source.file": source_file,
    "source.s3": source_s3,
    "source.gcs": source_gcs,
    "source.sftp": source_sftp,
    "drop_column": drop_column,
    "select_column": select_column,
    "filter": filter_op,
    "sort": sort,
    "limit": limit,
    "dedupe": dedupe,
    "derive": derive,
    "join": join,
    "run_storm": run_storm,
    "mask": mask_op,
    "generate": generate_op,
    "target.file": target_file,
    "target.db": target_db,
    "target.s3": target_s3,
    "target.gcs": target_gcs,
    "target.sftp": target_sftp,
    "sub_pipeline": sub_pipeline,
    "iterate_fixed": iterate_fixed,
    "iterate_loop": iterate_loop,
    "iterate_files": iterate_files,
    "sql_run": sql_run,
    "if": if_router,
    "flag_gate": flag_gate,
    "convert.file_type": convert_file_type,
}


def known_kinds() -> set[str]:
    return set(OPS.keys())
