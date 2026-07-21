"""Typed, fail-closed errors for the codeset ETL pipeline (HC-1 slice 2).

Distinct from ``decoy_engine.plan._errors.PlanCompileError``: that type is
the engine's execution-time contract (raised inside a masking job). This ETL
runs OUTSIDE any job -- it is a dev/ops tool (``scripts/``, never packaged;
see the package docstring) -- so it gets its own small hierarchy instead of
importing an execution-layer type into a build script. Every stage (fetch,
parse, validate, write) raises one of these on any defect rather than
returning a partial/best-effort result: per the slice-2 spec, a malformed or
truncated source must abort with a typed error, never write a partial corpus.
"""

from __future__ import annotations


class CodesetEtlError(Exception):
    """Base class for every fail-closed error this pipeline raises."""


class CodesetFetchError(CodesetEtlError):
    """The network fetch failed, timed out, or returned too few bytes."""


class CodesetParseError(CodesetEtlError):
    """The downloaded archive is not the expected format (bad zip, missing
    member file, unparseable row layout)."""


class CodesetValidationError(CodesetEtlError):
    """Parsed rows failed a sanity check (row-count floor, duplicate/empty
    codes) before anything was written to disk."""
