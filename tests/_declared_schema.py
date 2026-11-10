"""The Arrow schema a config declares for one table, for `run_mask_pipeline_chunked` calls.

Tests whose chunks carry columns the config does not list build the schema by hand instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pyarrow as pa

from decoy_engine import declared_columns


def declared_schema(config: Mapping[str, Any], table: str) -> pa.Schema:
    return pa.schema([pa.field(name, pa.string()) for name in declared_columns(config, table)])
