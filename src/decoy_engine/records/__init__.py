"""The record library's engine side: envelope, kinds, canonical form, export codec, job-log header.

Records are immutable, content-addressed JSON documents shared by the CLI's local library and the
platform importer, so both compute the same ids. This package touches no filesystem; where bytes
live is the caller's concern.
"""

from decoy_engine.records._canonical import canonical_bytes, payload_digest, record_id
from decoy_engine.records._codec import build, decode, encode
from decoy_engine.records._envelope import (
    ENVELOPE_SCHEMA,
    DecodedRecord,
    Labels,
    Link,
    OpaqueRecord,
    Producer,
    RecordEnvelope,
    RecordNeedsNewer,
)
from decoy_engine.records._errors import RecordError
from decoy_engine.records._export import (
    EXPORT_SCHEMA,
    BadLine,
    ExportedLog,
    ExportedRecord,
    ExportHeader,
    UnstrippedRef,
    export_lines,
    read_export,
)
from decoy_engine.records._joblog import (
    JOB_LOG_SCHEMA,
    JobLog,
    JobLogHeader,
    header_line,
    read_job_log,
)
from decoy_engine.records._registry import KindSpec, kind_spec, registered_kinds

__all__ = [
    "ENVELOPE_SCHEMA",
    "EXPORT_SCHEMA",
    "JOB_LOG_SCHEMA",
    "BadLine",
    "DecodedRecord",
    "ExportHeader",
    "ExportedLog",
    "ExportedRecord",
    "JobLog",
    "JobLogHeader",
    "KindSpec",
    "Labels",
    "Link",
    "OpaqueRecord",
    "Producer",
    "RecordEnvelope",
    "RecordError",
    "RecordNeedsNewer",
    "UnstrippedRef",
    "build",
    "canonical_bytes",
    "decode",
    "encode",
    "export_lines",
    "header_line",
    "kind_spec",
    "payload_digest",
    "read_export",
    "read_job_log",
    "record_id",
    "registered_kinds",
]
