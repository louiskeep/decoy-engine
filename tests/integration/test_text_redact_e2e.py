"""MG-2 integration (2026-05-31): text_redact end-to-end through the
PandasExecutionAdapter on the clinical_notes golden fixture.

Asserts byte-for-byte parity against the hand-curated expected output.
A diff here means either:
  - the detector regexes changed (review per-detector),
  - the splice logic changed (review _splice in _text_redact.py),
  - a new detector was added to _SPAN_DETECTORS (re-baseline the golden
    after reviewing the new spans by hand).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

GOLDEN_DIR = Path(__file__).parent.parent / "snapshots" / "golden" / "mask_text_redact"
INPUT_PATH = GOLDEN_DIR / "clinical_notes_input.txt"
OUTPUT_PATH = GOLDEN_DIR / "clinical_notes_output.txt"


def _col(provider_config: tuple = ()) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="text_redact",
        provider="x_nobackend",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=provider_config,
        coherent_with=(),
    )


def _plan(col_seed: ColumnSeed) -> SimpleNamespace:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x07" * 8,
            per_table=(("t", TableSeed(per_column=(("notes", col_seed),), per_group=())),),
        )
    )


def test_text_redact_full_pipeline_clinical_notes_matches_golden():
    input_text = INPUT_PATH.read_text(encoding="utf-8")
    expected = OUTPUT_PATH.read_text(encoding="utf-8")

    adapter = PandasExecutionAdapter()
    result = adapter.run(
        _plan(_col()),
        {"t": pa.table({"notes": [input_text]})},
        registry=get_default_registry(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
    )
    actual = result.outputs["t"].column("notes").to_pylist()[0]
    assert actual == expected, (
        "text_redact e2e golden mismatch.\n"
        f"--- expected ---\n{expected}\n"
        f"--- actual ---\n{actual}"
    )


def test_text_redact_manifest_carries_detectors_token_label_token():
    """The strategy provider_config round-trips through the plan
    envelope so the manifest can serialize all three config keys."""
    cfg = (
        ("detectors", ("email", "ssn")),
        ("label_token", True),
        ("token", "<PHI>"),
    )
    col_seed = _col(provider_config=cfg)
    # plan->envelope->col round-trip
    plan = _plan(col_seed)
    table_seed = plan.seed_envelope.per_table[0][1]
    round_tripped = table_seed.per_column[0][1]
    assert round_tripped.provider_config == cfg
