"""Child process for the shuffle cross-process determinism test: run the QA config once, print digests."""

from __future__ import annotations

import hashlib
import json
import sys

import pyarrow as pa
import pyarrow.csv as pa_csv

from decoy_engine.config import PipelineConfig
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.keyprovider import SecretKeyProvider

_SECRET = SecretKeyProvider(b"decoy-test-throwaway-secret-0123456789", key_version="v1")


def _sources(cfg: dict) -> dict[str, pa.Table]:
    return {name: pa_csv.read_csv(src["path"]) for name, src in sorted(cfg["sources"].items())}


def main() -> None:
    with open(sys.argv[1], encoding="utf-8") as fh:
        cfg = json.load(fh)
    result = run_pipeline(
        PipelineConfig.model_validate(cfg).model_dump(),
        _sources(cfg),
        engine_version="0.1.0",
        key_provider=_SECRET,
    )
    digests = {}
    for name in sorted(result.outputs):
        table = result.outputs[name]
        h = hashlib.sha256()
        for col in table.column_names:
            h.update(col.encode())
            h.update(repr(table.column(col).to_pylist()).encode())
        digests[name] = h.hexdigest()
    print(json.dumps(digests, sort_keys=True))


if __name__ == "__main__":
    main()
