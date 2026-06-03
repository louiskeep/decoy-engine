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

import pandas as pd

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.plan import compile_plan
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.profile import ColumnProfile, Profile, TableProfile
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import (
    NamespaceRegistry,
    build_namespace_registry,
)

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


# ── S5c W-1 / F1: ColumnConfig -> compile_plan -> ColumnSeed -> handler ──────
#
# The cells above build a ColumnSeed directly, bypassing plan-compile. S5c W-1
# (the platform emit fix) depends on the engine accepting a provider-less
# text_redact ColumnConfig through plan-compile -- the one link that had no
# committed test. These cells drive the real compile_plan path in-memory
# (mirrors tests/integration/test_composite_mg4_e2e.py's harness).

_PLAN_VERSION = "0.1.0"


def _profile_one(df: pd.DataFrame, table_name: str) -> Profile:
    from datetime import datetime

    return Profile(
        schema_version=1,
        tables=(
            TableProfile(
                name=table_name,
                row_count=len(df),
                columns=tuple(
                    ColumnProfile(
                        name=c,
                        dtype="object",
                        row_count=len(df),
                        null_count=int(df[c].isna().sum()),
                        distinct_count=int(df[c].nunique()),
                        sampled=False,
                        is_candidate_key_sampled=False,
                        declared_pk=False,
                        is_fk=False,
                        fk_target=None,
                        pii_class=None,
                    )
                    for c in df.columns
                ),
            ),
        ),
        relationships=(),
        profiled_at=datetime(2026, 6, 3),
        decoy_engine_version=_PLAN_VERSION,
    )


def _run_text_redact(provider_config: dict | None, notes: list[str]) -> list:
    """Build a single-table text_redact config exactly as graphToV2 emits it,
    compile it, run it through the pandas adapter, and return the output cells."""
    col: dict = {
        "name": "clinical_notes",
        "strategy": "text_redact",
        "namespace": "notes_clinical_notes",
        "deterministic": False,
    }
    if provider_config is not None:
        col["provider_config"] = provider_config
    config = {
        "global_settings": {"seed": 7},
        "tables": [{"name": "notes", "columns": [col]}],
    }
    df = pd.DataFrame({"clinical_notes": notes})
    profile = _profile_one(df, "notes")
    plan = compile_plan(config, profile, decoy_engine_version=_PLAN_VERSION)
    ns_registry = build_namespace_registry(config, profile)
    result = PandasExecutionAdapter().run(
        plan,
        {"notes": pa.Table.from_pandas(df, preserve_index=False)},
        registry=get_default_registry(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=ns_registry,
    )
    return result.outputs["notes"].column("clinical_notes").to_pylist()


def test_text_redact_through_compile_plan_redacts_pii():
    out = _run_text_redact(
        {"detectors": ["email", "ssn"], "token": "<PHI>"},
        ["Contact john@x.com or SSN 123-45-6789 today", "No PII here at all"],
    )
    assert out[0] == "Contact <PHI> or SSN <PHI> today"
    assert out[1] == "No PII here at all"  # non-PII row byte-identical


def test_text_redact_empty_detector_list_redacts_all_not_nothing():
    # S5c F2 fail-safe: detectors: [] must mean "all detectors", never "redact
    # nothing". iter_spans treats [] as zero detectors; the handler coerces
    # [] -> None so no authoring path can silently leave PHI unredacted.
    out = _run_text_redact(
        {"detectors": [], "token": "<PHI>"},
        ["Email a@b.com and SSN 123-45-6789"],
    )
    assert "a@b.com" not in out[0]
    assert "123-45-6789" not in out[0]
    assert "<PHI>" in out[0]


def test_text_redact_defaults_run_all_detectors():
    # No provider_config at all: the engine runs every detector with the
    # default "[REDACTED]" token. Proves the minimal emit (scalar branch with
    # no provider_config) is runnable.
    out = _run_text_redact(None, ["Reach me at jane@y.org"])
    assert "jane@y.org" not in out[0]
    assert "[REDACTED]" in out[0]
