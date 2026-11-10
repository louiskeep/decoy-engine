"""Every test-flight manifest still assembles into a valid pipeline config.

The flight suite lives outside ``testpaths`` and runs on demand, so a config-schema change can
break a job without any default-suite test noticing (GEN-STRICT E1 refused a stray ``params``
key on two jobs this way). This runs the flight's own config assembly, the same
``PipelineConfig.model_validate`` choke point, for each manifest in the default suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from testflight._builder import assemble_config
from testflight._spec import load_manifest

JOBS = Path(__file__).resolve().parents[3] / "testflight" / "jobs"
MANIFESTS = sorted(JOBS.glob("*/manifest.yaml"))


def test_manifests_are_found() -> None:
    assert len(MANIFESTS) >= 6, f"expected the test-flight jobs under {JOBS}, found {MANIFESTS}"


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_manifest_config_validates(path: Path, tmp_path: Path) -> None:
    manifest = load_manifest(path)
    sources = {
        t.name: str(tmp_path / f"{t.name}.parquet") for t in manifest.tables if t.kind != "generate"
    }
    assemble_config(manifest, sources, tmp_path, str(tmp_path / "quarantine.jsonl"))
