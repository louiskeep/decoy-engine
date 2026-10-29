"""Declared parsing, casting and protected quarantine (F06).

One engine-owned boundary applies a column's versioned contract (``decoy_engine.contracts.
declared_casting``) before masking and renders it after. Public names are re-exported here.
"""

from __future__ import annotations

from decoy_engine.declared_casting._catalog import (
    FormatDescriptor,
    capability_manifest,
    compile_formatter,
    compile_parser,
    descriptor_for,
    descriptors,
    format_value,
    match_sentinel,
    max_rendered_width,
    output_width,
    parse_value,
    validate_format_spec,
    validate_sentinel_spec,
)
from decoy_engine.declared_casting._types import CastOutcome

__all__ = [
    "CastOutcome",
    "FormatDescriptor",
    "capability_manifest",
    "compile_formatter",
    "compile_parser",
    "descriptor_for",
    "descriptors",
    "format_value",
    "match_sentinel",
    "max_rendered_width",
    "output_width",
    "parse_value",
    "validate_format_spec",
    "validate_sentinel_spec",
]
