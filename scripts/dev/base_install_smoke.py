"""Base-install smoke: the classifier runs from the engine's base dependencies alone.

Run it in a venv synced with no extras (the `base-install` job in ci.yml). No pytest, no
fixtures: it loads the bundled lgbm-v1 pack strictly and classifies a small frame. On macOS
without libomp the lightgbm import fails; `classifier_status()` must then report the `import`
cause and nothing may escape.
"""

from __future__ import annotations

import sys

import pandas as pd

import decoy_engine


def main() -> int:
    status = decoy_engine.classifier_status()
    if not status.available:
        print(f"classifier unavailable: reason={status.reason} cause={status.cause}")
        return 1
    found = decoy_engine.load_detectors(("classifier",), ner_model="en_core_web_sm")
    pack = found.classifier_pack
    if pack is None:
        print("classifier pack did not load")
        return 1
    from decoy_engine.profiling.model_pack import classify_frame

    frame = pd.DataFrame({"email": ["a@example.com", "b@example.org"], "n": ["1", "2"]})
    result = classify_frame(pack, frame)
    if set(result) != set(frame.columns):
        print("classify_frame did not return every column")
        return 1
    print(f"classifier ok: {status.model_id} {status.version} lightgbm {status.runtime_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
