"""AT-M4a: a regex-only post-scan report is byte-equal to the base output (class row 9).

The golden file was written on the base, before the model passes existed, by running this module
(`python -m tests.unit.detection_verification.test_scanml_m4_regex_baseline`). Each check drops
`generated_at` and the fields the model passes add, after asserting each added field holds its
regex-only value, then compares the JSON text. Two callers are pinned: `run_post_mask` called
with no detectors (the platform hook) and `run_pipeline` with `post_scan: {integrity: true}`.
Mutation M-M4a (run the classifier when `detectors` is None) turns the first red.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "scanml_m4" / "regex_only_reports.json"
_FINDING_DEFAULTS = {
    "source": "regex",
    "model": None,
    "band": None,
    "dismissed": False,
    "promoted": False,
}
_REPORT_ADDED = ("detectors", "tables_checked", "config_sha256")
N = 40


def _frames() -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], dict[str, Any]]:
    emails = [f"user{i}@example.com" for i in range(N)]
    ssns = [f"{100 + i:03d}-{10 + i % 80:02d}-{1000 + i * 7:04d}" for i in range(N)]
    phones = [f"(555) {200 + i:03d}-{1000 + i * 3:04d}" for i in range(N)]
    names = [f"Person{i} Example" for i in range(N)]
    src_customers = pd.DataFrame(
        {
            "email": emails,
            "ssn": ssns,
            "phone": phones,
            "memo": [f"reach me at {e}" for e in emails],
            "full_name": names,
            "card": [f"4111 1111 1111 {1000 + i:04d}" for i in range(N)],
        }
    )
    out_customers = pd.DataFrame(
        {
            "email": emails,  # unconfigured: a regex warning, escalated on identity
            "ssn": [f"h{i:08x}" for i in range(N)],  # hashed
            "phone": phones,  # passthrough
            "memo": [f"reach me at {e}" for e in emails],  # text_redact that did not fire
            "full_name": [f"Fake{i} Name" for i in range(N)],  # faker
            "card": [
                src_customers["card"][i] if i % 5 == 0 else f"4000 0000 0000 {2000 + i:04d}"
                for i in range(N)
            ],  # fpe with partial identity
        }
    )
    src_orders = pd.DataFrame({"order_id": [f"O{i}" for i in range(N)], "qty": list(range(N))})
    out_orders = src_orders.copy()
    config = {
        "version": 2,
        "tables": [
            {
                "name": "customers",
                "columns": [
                    {"name": "ssn", "strategy": "hash", "namespace": "ssn"},
                    {"name": "phone", "strategy": "passthrough"},
                    {"name": "memo", "strategy": "text_redact"},
                    {"name": "full_name", "strategy": "faker", "provider": "person_full_name"},
                    {"name": "card", "strategy": "fpe"},
                ],
            },
            {"name": "orders", "columns": []},
        ],
        "post_scan": {"integrity": True},
    }
    return (
        {"customers": src_customers, "orders": src_orders},
        {"customers": out_customers, "orders": out_orders},
        config,
    )


def _platform_report() -> dict[str, Any]:
    from decoy_engine.verification.postmask import run_post_mask

    sources, outputs, config = _frames()
    return run_post_mask(sources, outputs, config=config)


def _pipeline_report(tmp_path: Path) -> dict[str, Any]:
    from decoy_engine.config import PipelineConfig
    from decoy_engine.execution import run_pipeline

    sources, _, _ = _frames()
    df = sources["customers"]
    src = tmp_path / "c.csv"
    df.to_csv(src, index=False)
    raw = {
        "version": 2,
        "global_settings": {"seed": 7},
        "post_scan": {"integrity": True},
        "sources": {"c": {"type": "file", "format": "csv", "has_header": True, "path": str(src)}},
        "tables": [
            {
                "name": "c",
                "columns": [
                    {"name": "email", "strategy": "passthrough"},
                    {"name": "ssn", "strategy": "redact"},
                    {"name": "phone", "strategy": "passthrough"},
                    {"name": "memo", "strategy": "redact"},
                    {"name": "full_name", "strategy": "faker", "provider": "person_full_name"},
                    {"name": "card", "strategy": "redact"},
                ],
            }
        ],
        "targets": {"c": {"type": "file", "format": "csv", "path": str(tmp_path / "o.csv")}},
    }
    cfg = PipelineConfig.model_validate(raw).model_dump()
    table = pa.Table.from_pandas(df, preserve_index=False)
    result = run_pipeline(cfg, sources={"c": table}, engine_version="scanml-m4-golden")
    return result.quality_metrics["post_scan_integrity"]


def _base_shape(report: dict[str, Any]) -> str:
    """The report as the base wrote it: new regex-only fields checked, then dropped."""
    report = dict(report)
    report.pop("generated_at")
    for key in _REPORT_ADDED:
        report.pop(key, None)
    findings = []
    for finding in report["residual_pii"]:
        finding = dict(finding)
        for key, default in _FINDING_DEFAULTS.items():
            if key in finding:
                assert finding.pop(key) == default, (key, finding)
        findings.append(finding)
    report["residual_pii"] = findings
    return json.dumps(report, indent=1, sort_keys=False)


def _golden() -> dict[str, str]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_platform_call_with_no_detectors_is_byte_equal_to_the_base() -> None:
    report = _platform_report()
    assert report["residual_pii"], "the fixture set must produce findings to compare"
    assert _base_shape(report) == _golden()["run_post_mask"]


def test_regex_only_pipeline_report_is_byte_equal_to_the_base(tmp_path: Path) -> None:
    assert _base_shape(_pipeline_report(tmp_path)) == _golden()["run_pipeline"]


if __name__ == "__main__":  # writes the golden on the base; never run on a branch that changed it
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        golden = {
            "run_post_mask": _base_shape(_platform_report()),
            "run_pipeline": _base_shape(_pipeline_report(Path(tmp))),
        }
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(golden, indent=1) + "\n", encoding="utf-8")
