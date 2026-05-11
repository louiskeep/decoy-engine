"""Registry of graph node ops.

Maps the YAML `kind:` string to the op module that implements it. Each op
module conforms to the protocol in `_base.py`.

To add a new kind: write the op module, import it here, register it in OPS.
The validator's "unknown kind" rejection is driven directly off OPS.
"""

from decoy_engine.graph.ops import (
    drop_column,
    select_column,
    filter_op,
    dedupe,
    derive,
    limit,
    sort,
    run_storm,
    source_file,
    source_db,
    target_file,
    target_db,
    mask_op,
    generate_op,
    sub_pipeline,
    iterate_fixed,
    iterate_loop,
    iterate_files,
    sql_run,
)

OPS: dict[str, object] = {
    "source.db": source_db,
    "source.file": source_file,
    "drop_column": drop_column,
    "select_column": select_column,
    "filter": filter_op,
    "sort": sort,
    "limit": limit,
    "dedupe": dedupe,
    "derive": derive,
    "run_storm": run_storm,
    "mask": mask_op,
    "generate": generate_op,
    "target.file": target_file,
    "target.db": target_db,
    # Sprint G Week 4: sub-pipelines + iterators (FILE, FIXED, LOOP).
    "sub_pipeline": sub_pipeline,
    "iterate_fixed": iterate_fixed,
    "iterate_loop": iterate_loop,
    "iterate_files": iterate_files,
    # Sprint G Week 5: DuckDB-on-DataFrame SQL escape hatch.
    "sql_run": sql_run,
}


def known_kinds() -> set[str]:
    return set(OPS.keys())
