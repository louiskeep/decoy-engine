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
    source_file,
    source_db,
    target_file,
    target_db,
    mask_op,
    generate_op,
)

OPS: dict[str, object] = {
    "source.db": source_db,
    "source.file": source_file,
    "drop_column": drop_column,
    "select_column": select_column,
    "filter": filter_op,
    "dedupe": dedupe,
    "mask": mask_op,
    "generate": generate_op,
    "target.file": target_file,
    "target.db": target_db,
}


def known_kinds() -> set[str]:
    return set(OPS.keys())
