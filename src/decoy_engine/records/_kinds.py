"""Every record kind V1 knows, registered once.

A model that belongs to another package (``RunResult``, ``ScanSetCore``) is pointed at, never
copied, so the library, the platform importer and the producer cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import TypeAdapter, ValidationError

from decoy_engine.config import SOURCE_LOCATOR_FIELDS, SOURCE_PATH_FIELDS, SourceDescriptor
from decoy_engine.profiling.scan_set import SCAN_SET_SCHEMA, ScanSetCore, ScanSetCoreRead
from decoy_engine.records._payloads import EvidencePayload, SampleRunPayload
from decoy_engine.records._read_model import read_model_of
from decoy_engine.records._registry import KindSpec, register_kind
from decoy_engine.run_result import RUN_RESULT_SCHEMA, RunResult, RunResultRead

_DESCRIPTOR: TypeAdapter[Any] = TypeAdapter(SourceDescriptor)


def _scan_set_strippable(payload: Mapping[str, Any]) -> bool:
    """Whether every table descriptor is a public source descriptor, so the marker registry
    names all of its path and locator fields. A descriptor with an unknown key is not."""
    tables = payload.get("tables")
    if not isinstance(tables, list):
        return False
    for table in tables:
        descriptor = table.get("descriptor") if isinstance(table, dict) else None
        if not isinstance(descriptor, dict):
            return False
        try:
            _DESCRIPTOR.validate_python(descriptor)
        except ValidationError:
            return False
    return True


# A scan record holds counts, rates, type labels and column names, plus each table's source
# descriptor. The descriptor names a file or object, so its path fields come from the config
# marker registry (a new marker joins here without an edit); a bucket or endpoint cannot be
# reduced, so they are locators and export lists the record as unstripped.
SCAN_SET = register_kind(
    KindSpec(
        kind="scan-set",
        payload_schema=SCAN_SET_SCHEMA,
        write_model=ScanSetCore,
        read_model=ScanSetCoreRead,
        path_fields=tuple(f"/payload/tables/*/descriptor/{f}" for f in SOURCE_PATH_FIELDS),
        locator_fields=tuple(f"/payload/tables/*/descriptor/{f}" for f in SOURCE_LOCATOR_FIELDS),
        strip_check=_scan_set_strippable,
        diff_rule="scan-set",
        canary_fixture="scan-set",
    )
)

EVIDENCE = register_kind(
    KindSpec(
        kind="evidence",
        payload_schema="cli-local-1",
        write_model=EvidencePayload,
        read_model=read_model_of(EvidencePayload),
        path_fields=(
            "/payload/pipeline_path",
            "/payload/input_fingerprints/*/path",
            "/payload/output_fingerprints/*/path",
        ),
        diff_rule="evidence",
        canary_fixture="evidence",
    )
)

RUN_RESULT = register_kind(
    KindSpec(
        kind="run-result",
        payload_schema=RUN_RESULT_SCHEMA,
        write_model=RunResult,
        read_model=RunResultRead,
        path_fields=(
            "/payload/tables/*/delivery/output",
            "/payload/links/log/path",
        ),
        diff_rule="run-result",
        canary_fixture="run-result",
    )
)

SAMPLE_RUN = register_kind(
    KindSpec(
        kind="sample-run",
        payload_schema="sample-run/v1",
        write_model=SampleRunPayload,
        read_model=read_model_of(SampleRunPayload),
        path_fields=("/payload/scratch_dir", "/payload/outputs/*/path"),
        diff_rule="sample-run",
        canary_fixture="sample-run",
    )
)

# Reserved: the names and link types are stable now; the owning plans add the models.
PUBLICATION = register_kind(KindSpec(kind="publication", payload_schema="publication/v1"))
CHECKPOINT = register_kind(KindSpec(kind="checkpoint", payload_schema="checkpoint-index/v1"))
