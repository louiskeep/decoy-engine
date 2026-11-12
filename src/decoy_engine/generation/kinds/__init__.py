"""Generate types that need no source data (`constant`, parametric `distribution`, `lookup`) and `output_type`.

A sub-package rather than modules beside `synthesize.py` (generation/ already holds 25 direct
modules). The name is `kinds`, not `types`, so it never shadows the stdlib module. Callers import
from here, never from the underscore modules.
"""

from __future__ import annotations

from decoy_engine.generation.kinds._constant import (
    ConstantStream,
    constant_arrow_type,
    constant_payload_bound,
    constant_values,
)
from decoy_engine.generation.kinds._lookup import (
    MAX_LOOKUP_ENTRIES,
    LookupList,
    LookupOutcome,
    lookup_column_values,
    lookup_digests,
    lookup_file_format,
    lookup_from_codeset,
    lookup_values,
    parse_lookup_bytes,
)
from decoy_engine.generation.kinds._output_type import (
    OutputType,
    cast_output_columns,
    cast_output_values,
    output_arrow_type,
    output_payload_bound,
    output_type_of,
    output_types,
    to_lexeme,
)
from decoy_engine.generation.kinds._parametric import (
    ParametricStream,
    parametric_arrow_type,
    parametric_payload_bound,
    parametric_values,
)

__all__ = [
    "MAX_LOOKUP_ENTRIES",
    "ConstantStream",
    "LookupList",
    "LookupOutcome",
    "OutputType",
    "ParametricStream",
    "cast_output_columns",
    "cast_output_values",
    "constant_arrow_type",
    "constant_payload_bound",
    "constant_values",
    "lookup_column_values",
    "lookup_digests",
    "lookup_file_format",
    "lookup_from_codeset",
    "lookup_values",
    "output_arrow_type",
    "output_payload_bound",
    "output_type_of",
    "output_types",
    "parametric_arrow_type",
    "parametric_payload_bound",
    "parametric_values",
    "parse_lookup_bytes",
    "to_lexeme",
]
