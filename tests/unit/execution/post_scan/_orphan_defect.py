"""A masking defect the gate must catch: every child key is rewritten after the mask.

The engine keeps child keys matched to their parent by construction (no config makes it
orphan its own output), so the defect is planted at the gate's input. The gate, the verdict, the
envelope and the commit path under test are all the real ones.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pyarrow as pa

from decoy_engine.execution.pipeline import _post_scan


def install() -> None:
    real = _post_scan.PostScanGate.run_resident

    def break_child_keys(
        self: Any, outputs: Mapping[str, pa.Table], *args: Any, **kwargs: Any
    ) -> None:
        child = outputs["child"]
        at = child.schema.get_field_index("parent_id")
        field = child.schema.field("parent_id")
        broken = child.set_column(at, field, pa.array(["no-parent"] * child.num_rows, field.type))
        real(self, {**outputs, "child": broken}, *args, **kwargs)

    _post_scan.PostScanGate.run_resident = break_child_keys
