"""Runnable demo: text_redact with `ner: true` on free-text notes.

Companion to `runnable_demo.py`'s bigger multi-table walkthrough; this one
is deliberately small and focused on ONE thing -- spaCy NER activation
(TX-1, see docs/guides/free-text-ner.md). Requires the `ner` extra +
`en_core_web_sm`:

    pip install 'decoy-engine[ner]'
    python -m spacy download en_core_web_sm
    python examples/ner_redaction_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd
import pyarrow as pa

from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.storm.ner import DEFAULT_NER_MODEL, model_installed, spacy_installed

if not spacy_installed():
    print(
        "spaCy is not installed. Run: pip install 'decoy-engine[ner]'",
        file=sys.stderr,
    )
    sys.exit(1)
if not model_installed(DEFAULT_NER_MODEL):
    print(
        f"The {DEFAULT_NER_MODEL!r} model is not installed. Run: "
        f"python -m spacy download {DEFAULT_NER_MODEL}",
        file=sys.stderr,
    )
    sys.exit(1)

notes = pd.DataFrame(
    {
        "note_id": [1, 2, 3],
        "clinical_notes": [
            "Patient Jane Doe, seen in Boston on referral from Dr. Alan Turing.",
            "Contact John Smith at john.smith@example.com regarding the claim.",
            "No PII in this row, just a routine follow-up note.",
        ],
    }
)

# profile_source reads the declared source path for the pre-mask row-count
# profile even though the actual data below comes from the in-memory
# `sources` dict passed to run_pipeline -- write it to disk too, same as
# runnable_demo.py.
HERE = Path(__file__).resolve().parent
IN = HERE / "in"
IN.mkdir(exist_ok=True)
notes.to_csv(IN / "notes.csv", index=False)

config = {
    "version": 1,
    "global_settings": {"seed": 1},
    "sources": {"notes": {"type": "file", "format": "csv", "path": "examples/in/notes.csv"}},
    "targets": {"notes": {"type": "file", "format": "csv", "path": "examples/out/notes.csv"}},
    "tables": [
        {
            "name": "notes",
            "columns": [
                {"name": "note_id", "strategy": "passthrough"},
                {
                    "name": "clinical_notes",
                    "strategy": "text_redact",
                    "provider_config": {
                        # `ner: true` turns on the default en_core_web_sm
                        # model for person_name/location spans, on top of
                        # the regex catalog (which already catches the
                        # email in row 2). label_token makes it visible
                        # WHICH detector fired each redaction.
                        "ner": True,
                        "label_token": True,
                    },
                },
            ],
        }
    ],
}

# Validate against the real strict model, then dump to the plain dict the
# engine consumes -- same pattern as runnable_demo.py.
cfg = PipelineConfig.model_validate(config).model_dump()

sources = {"notes": pa.Table.from_pandas(notes)}
result = run_pipeline(cfg, sources, engine_version="demo-0")

masked = result.outputs["notes"].to_pandas()

OUT = HERE / "out"
OUT.mkdir(exist_ok=True)
masked.to_csv(OUT / "notes.csv", index=False)

print("--- SOURCE ---")
print(notes.to_string(index=False))
print("\n--- REDACTED (ner: true) ---")
print(masked.to_string(index=False))
print("\nDone. Output written to examples/out/notes.csv")
