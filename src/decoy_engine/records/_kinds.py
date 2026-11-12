"""Every record kind V1 knows, registered once.

A model that belongs to another package (``RunResult``, ``ScanSetCore``) is pointed at, never
copied, so the library, the platform importer and the producer cannot drift apart.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import TypeAdapter, ValidationError

from decoy_engine.config import SOURCE_LOCATOR_FIELDS, SOURCE_PATH_FIELDS, SourceDescriptor
from decoy_engine.profiling import SCAN_SET_SCHEMA, ScanSetCore, ScanSetCoreRead
from decoy_engine.records._export_rules import pointer_rule
from decoy_engine.records._payloads import EvidencePayload, ExportLayout, SampleRunPayload
from decoy_engine.records._read_model import read_model_of
from decoy_engine.records._registry import KindSpec, register_kind
from decoy_engine.run_result import RUN_RESULT_SCHEMA, RunResult, RunResultRead

_DESCRIPTOR: TypeAdapter[Any] = TypeAdapter(SourceDescriptor)
_LAYOUT: TypeAdapter[Any] = TypeAdapter(ExportLayout)


_EVIDENCE_PATHS = (
    "/payload/pipeline_path",
    "/payload/project_manifest",
    "/payload/input_fingerprints/*/path",
    "/payload/output_fingerprints/*/path",
)
_RUN_RESULT_PATHS = ("/payload/tables/*/delivery/output", "/payload/links/log/path")
_SAMPLE_PATHS = ("/payload/scratch_dir", "/payload/outputs/*/path")


def _strict_dump(adapter: TypeAdapter[Any], value: Any) -> dict[str, Any] | None:
    """``value`` as the strict model's JSON dump, or ``None``. Strict means no coercion (a width
    given as text, a bool given as "true" fail), and the dump is what leaves, never the input."""
    try:
        model = adapter.validate_json(json.dumps(value), strict=True)
    except (ValidationError, TypeError, ValueError):
        return None
    dumped: dict[str, Any] = model.model_dump(mode="json")
    return dumped


def _evidence_scrub(payload: dict[str, Any]) -> bool:
    """Free text leaves the evidence payload in a default export. A bare warning is a message
    and a warning's ``detail`` carries engine text, so neither is kept (the code, provider and
    column stay). A layout must fit the structural model, or the record is withheld."""
    payload["warnings"] = [
        {**w, "detail": {}} for w in payload.get("warnings", []) if isinstance(w, dict)
    ]
    for output in payload.get("output_fingerprints", {}).values():
        layout = output.get("layout")
        if layout is None:
            continue
        projected = _strict_dump(_LAYOUT, layout)
        if projected is None:
            return False
        output["layout"] = projected
    return True


def _scan_set_scrub(payload: dict[str, Any]) -> bool:
    """Each table descriptor becomes the dump of a strict public ``SourceDescriptor``, so the
    marker registry names all of its path and locator fields. A descriptor with an unknown key or
    a value that needed coercion (a width given as text) is not one, and the record is withheld."""
    tables = payload.get("tables")
    if not isinstance(tables, list):
        return False
    for table in tables:
        descriptor = table.get("descriptor") if isinstance(table, dict) else None
        if not isinstance(descriptor, dict):
            return False
        projected = _strict_dump(_DESCRIPTOR, descriptor)
        if projected is None:
            return False
        table["descriptor"] = projected
    return True


# A scan record holds counts, rates, type labels and column names, plus each table's source
# descriptor. The descriptor names a file or object, so its path fields come from the config
# marker registry (a new marker joins here without an edit); a bucket or endpoint cannot be
# reduced, so they are locators and export lists the record as unstripped.
_SCAN_PATHS = tuple(f"/payload/tables/*/descriptor/{f}" for f in SOURCE_PATH_FIELDS)
_SCAN_LOCATORS = tuple(f"/payload/tables/*/descriptor/{f}" for f in SOURCE_LOCATOR_FIELDS)
SCAN_SET = register_kind(
    KindSpec(
        kind="scan-set",
        payload_schema=SCAN_SET_SCHEMA,
        write_model=ScanSetCore,
        read_model=ScanSetCoreRead,
        path_fields=_SCAN_PATHS,
        locator_fields=_SCAN_LOCATORS,
        export_rule=pointer_rule(_SCAN_PATHS, _SCAN_LOCATORS, scrub=_scan_set_scrub),
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
        path_fields=_EVIDENCE_PATHS,
        export_rule=pointer_rule(_EVIDENCE_PATHS, scrub=_evidence_scrub),
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
        path_fields=_RUN_RESULT_PATHS,
        export_rule=pointer_rule(_RUN_RESULT_PATHS),
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
        path_fields=_SAMPLE_PATHS,
        export_rule=pointer_rule(_SAMPLE_PATHS),
        diff_rule="sample-run",
        canary_fixture="sample-run",
    )
)

# Reserved: the names and link types are stable now; the owning plans add the models.
PUBLICATION = register_kind(KindSpec(kind="publication", payload_schema="publication/v1"))
CHECKPOINT = register_kind(KindSpec(kind="checkpoint", payload_schema="checkpoint-index/v1"))
