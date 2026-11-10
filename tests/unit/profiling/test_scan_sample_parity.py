"""The same dataset as CSV, TSV, fixed width and Parquet gives the same head-sample facts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from decoy_engine import write_fixed_width
from decoy_engine.profiling import scan_sources

ROWS = 1_500
HEAD = 1_000
FRAME = pd.DataFrame(
    {
        "id": range(1, ROWS + 1),
        "grp": [f"g{i % 7}" for i in range(ROWS)],
        "val": [i % 13 for i in range(ROWS)],
    }
)
LAYOUT = {
    "columns": [
        {"name": "id", "start": 0, "width": 6, "type": "int", "align": "right"},
        {"name": "grp", "start": 6, "width": 4},
        {"name": "val", "start": 10, "width": 4, "type": "int", "align": "right"},
    ]
}


def _sources(tmp_path: Path) -> dict[str, dict[str, Any]]:
    FRAME.to_csv(tmp_path / "d.csv", index=False)
    FRAME.to_csv(tmp_path / "d.tsv", index=False, sep="\t")
    FRAME.to_parquet(tmp_path / "d.parquet")
    write_fixed_width(FRAME, LAYOUT, tmp_path / "d.fwf")
    file = {"type": "file"}
    return {
        "csv": {**file, "format": "csv", "path": str(tmp_path / "d.csv"), "has_header": True},
        "tsv": {
            **file,
            "format": "csv",
            "path": str(tmp_path / "d.tsv"),
            "delimiter": "\t",
            "has_header": True,
        },
        "parquet": {**file, "format": "parquet", "path": str(tmp_path / "d.parquet")},
        "fwf": {**file, "format": "fixed_width", "path": str(tmp_path / "d.fwf"), "layout": LAYOUT},
    }


def test_head_sample_facts_match_across_formats(tmp_path: Path) -> None:
    scan = scan_sources(_sources(tmp_path), sample_rows=HEAD)
    facts = {
        t.name: (
            t.profile.row_count,
            {f.name: (f.null_count, f.distinct_count) for f in t.profile.fields},
        )
        for t in scan.tables
    }
    assert facts["csv"][0] == HEAD
    assert facts["csv"][1]["id"] == (0, HEAD)
    assert len({repr(v) for v in facts.values()}) == 1
